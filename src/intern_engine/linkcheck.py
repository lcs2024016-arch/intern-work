"""Dead-link check: drops roles whose posting page is a hard 404.

The engine already closes a role when its company stops listing it — but only
when that company's fetch SUCCEEDED. A board that is quarantined, rate-limited
or simply down keeps its old roles marked open forever, and those are exactly
the rows a reader clicks and finds a 404 on.

So this is a second, independent signal: re-request each open role's Apply URL
and remove the role when the page is definitively gone.

Three rules keep this from lying, because a false "closed" is worse than a
stale row:

  * **Only 404/410 counts.** 403 (WAF/bot block), 429, 5xx, timeouts and
    transport errors are all "we could not tell" and never remove anything.
  * **Two strikes, on the same URL.** Datacentre IPs get a 404 from some
    bot-blocking CDNs on a good day. A role has to come back 404 twice,
    checked hours apart, before it is removed.
  * **Never a role we just saw.** A role returned by this run's fetch is
    excluded outright: the board still lists it, whatever the HTTP answer.

Results are cached in data/link_status.json (committed, like health.json) so an
hourly run re-checks a slice of the list instead of the whole thing, and a
budget keeps the extra traffic bounded.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

from . import config, paths
from .net import Net

# Statuses that mean "this posting is gone", as opposed to "we were not
# allowed to look" or "the server is having a bad day".
DEAD_STATUSES = (404, 410)

# Statuses that a bot-blocking CDN returns instead of answering a HEAD. The
# request is retried as a GET before anything is concluded from it.
_HEAD_UNSUPPORTED = (403, 405, 501)

_CHECK_CONCURRENCY = 8


def load_cache() -> dict:
    try:
        with open(paths.LINK_STATUS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(paths.LINK_STATUS_PATH), exist_ok=True)
    with open(paths.LINK_STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False, sort_keys=True)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _due(records: list[dict], cache: dict, cfg: dict) -> list[dict]:
    """Which roles are worth spending a request on this run.

    Ordered so the most likely dead links are checked first: roles nobody has
    re-checked yet, then the ones checked longest ago, then the oldest
    postings (a posting that has been open for months is a posting that has
    been taken down).
    """
    now = datetime.now(UTC)
    recheck_hours = config.link_check_recheck_hours(cfg)
    confirm_hours = config.link_check_confirm_hours(cfg)

    # (staleness, posted date, record). An empty checked_at sorts first, so
    # never-checked links are requested before ones whose verdict is merely
    # old, and old postings before recent ones at equal staleness.
    due = []
    for record in records:
        url = record.get("url")
        if not url:
            continue
        entry = cache.get(record.get("id") or "")
        if entry and entry.get("url") == url:
            # How long this link's verdict holds. A link that already answered
            # 404 once is due again much sooner: the confirming request should
            # land on the next hourly run, not the next day, or a genuinely
            # dead posting would sit in the list for 24h after we found it.
            window = confirm_hours if (entry.get("strikes") or 0) else recheck_hours
            checked = _parse(entry.get("checked_at", ""))
            if checked is not None and now - checked < timedelta(hours=window):
                continue
            due.append((entry.get("checked_at", ""), record))
        else:
            # Never checked, or checked against a URL the posting has since
            # moved to — start over rather than inherit the old link's verdict.
            due.append(("", record))

    due.sort(key=lambda item: (item[0], (item[1].get("posted_at") or "")[:10]))
    return [record for _, record in due[: config.link_check_budget(cfg)]]


async def _probe(url: str, net: Net) -> int | None:
    """Status code for one URL, or None when the answer was inconclusive."""
    try:
        return await net.probe(url)
    except Exception:  # noqa: BLE001 — a bad URL must not stop the run
        return None


async def check_dead_links(
    records: list[dict],
    net: Net,
    workday_net: Net,
) -> tuple[set[str], int, int]:
    """Re-request open roles' Apply URLs. Returns (dead ids, checked, strikes).

    `records` must already exclude anything this run's fetch returned, so a
    role cannot be removed on the strength of a request while its own board is
    actively listing it.
    """
    cfg = config.load_config()
    cache = load_cache()

    if not config.link_check_enabled(cfg):
        return set(), 0, 0
    if not records:
        # Nothing open left to watch, so nothing in the cache is worth
        # keeping either. Prune even on the way out, or a role removed by the
        # last run leaves its entry behind until some other link comes due.
        if cache:
            cache.clear()
            save_cache(cache)
        return set(), 0, 0

    targets = _due(records, cache, cfg)
    if not targets:
        # Nothing due, but still prune entries for roles that are gone.
        _prune(cache, records)
        save_cache(cache)
        return set(), 0, 0

    gate = asyncio.Semaphore(_CHECK_CONCURRENCY)

    async def worker(record: dict) -> tuple[dict, str | None]:
        async with gate:
            status = await _probe(
                record["url"],
                workday_net if record.get("source") in ("workday", "oracle") else net,
            )
        return record, status

    results = await asyncio.gather(*(worker(r) for r in targets))

    now = datetime.now(UTC)
    strikes_needed = config.link_check_strikes(cfg)
    dead: set[str] = set()
    strikes_now = 0

    for record, status in results:
        jid = record["id"]
        entry = cache.get(jid) or {}
        # A changed URL must not inherit the old link's strike count.
        strikes = (entry.get("strikes") or 0) if entry.get("url") == record["url"] else 0

        if status in DEAD_STATUSES:
            strikes += 1
            strikes_now += 1
        elif status is not None and 200 <= status < 400:
            strikes = 0  # the posting is alive again

        cache[jid] = {
            "url": record["url"],
            "status": status,
            "strikes": strikes,
            "checked_at": _iso(now),
        }
        if strikes >= strikes_needed:
            dead.add(jid)

    _prune(cache, records)
    save_cache(cache)
    return dead, len(results), strikes_now


def _prune(cache: dict, records: list[dict]) -> None:
    """Drop cache entries for roles that no longer need watching.

    Anything not open in the store is gone: either already removed, or closed
    by the fetch logic. Keeping its entry would let the file grow without
    bound and would let a revived link under a reused id skip its two strikes.
    """
    live = {r.get("id") for r in records if r.get("is_open") and r.get("url")}
    for jid in list(cache):
        if jid not in live:
            del cache[jid]