"""InternDoor as a discovery source.

InternDoor publishes a public JSON feed of India engineering internships and
entry-level roles. This module reads that feed ONCE to learn WHICH COMPANIES
are hiring in India, and writes them out as seed names for discover.py to
resolve to real ATS boards.

What it deliberately does NOT do is mirror their job data. We use their feed
as a company list and then poll each company's own careers API directly,
which means:

  - the roles we publish come from the employer's own ATS, not a third party,
    so a stale or wrong listing is their problem to fix, not ours to guess at;
  - freshness is bounded by how often WE poll, not by how often they publish;
  - nothing on the site depends on another site staying up.

Their feed is a directory, not our source of truth.

Only `category == "software"` is read, per scope. The feed tags every listing,
so the filter is applied server-side by us rather than trusting the count in
their metadata.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from . import paths

FEED_URL = "https://interndoor.com/data/jobs.json"

# We only care about software roles. InternDoor uses this exact slug, but fall
# back to a substring match so a rename ("software-engineering") keeps working
# instead of silently emptying the seed list.
SOFTWARE_CATEGORY = "software"

HEADERS = {
    "User-Agent": (
        "intern-engine-india/2.0 (+https://github.com/intern-engine) "
        "discovery-seed-reader"
    ),
    "Accept": "application/json",
}


def _is_software(job: dict[str, Any]) -> bool:
    category = str(job.get("category") or "").strip().lower()
    return category == SOFTWARE_CATEGORY or "software" in category


def _clean(name: str) -> str:
    """Trim the decoration companies carry in listing titles."""
    return " ".join(str(name or "").split()).strip(" -–—|")


def _looks_like_employer(name: str) -> bool:
    """Reject obvious noise before we spend probe requests on it."""
    n = name.strip()
    if len(n) < 2 or len(n) > 80:
        return False
    # Aggregator and programme names, not companies we could resolve a board for.
    junk = {
        "academy",
        "all",
        "apprentice",
        "apprenticeship",
        "intern",
        "internship",
        "unknown",
    }
    if n.lower() in junk:
        return False
    return True


def extract_companies(payload: dict[str, Any]) -> list[str]:
    """Distinct employer names carrying at least one software listing.

    Counted by listing rather than taken from the feed's own `companies` array:
    that array is not filtered by category, so it would seed us with every
    non-software employer on the site.
    """
    counts: dict[str, int] = {}
    for job in payload.get("jobs") or []:
        if not isinstance(job, dict) or not _is_software(job):
            continue
        name = _clean(job.get("company") or "")
        if not name or not _looks_like_employer(name):
            continue
        # Same employer can appear under several legal suffixes. Fold on a
        # punctuation-free key so "Wipro Ltd" and "Wipro Limited" are one seed.
        key = "".join(ch for ch in name.lower() if ch.isalnum())
        if not key:
            continue
        slot = counts.setdefault(key, [name, 0])  # type: ignore[assignment]
        slot[1] += 1
        # Prefer the shortest spelling: it is usually the recognisable one.
        if len(name) < len(slot[0]):
            slot[0] = name

    ranked = sorted(counts.values(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _n in ranked]


def fetch_feed(client: httpx.Client | None = None) -> dict[str, Any]:
    """Fetch the public feed. Raises on failure so the caller can decide."""
    owned = client is None
    client = client or httpx.Client(headers=HEADERS, follow_redirects=True)
    try:
        resp = client.get(FEED_URL, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("feed was not a JSON object")
        return payload
    finally:
        if owned:
            client.close()


def load_seeds() -> list[str]:
    """Previously captured InternDoor company names, if any."""
    if not os.path.exists(paths.INTERNDOOR_SEEDS_PATH):
        return []
    try:
        with open(paths.INTERNDOOR_SEEDS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        names = data.get("companies") if isinstance(data, dict) else data
        return [_clean(n) for n in (names or []) if _clean(n)]
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def save_seeds(payload: dict[str, Any], names: list[str]) -> None:
    os.makedirs(os.path.dirname(paths.INTERNDOOR_SEEDS_PATH), exist_ok=True)
    with open(paths.INTERNDOOR_SEEDS_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source": FEED_URL,
                "fetched_at": payload.get("generatedAt"),
                "region": payload.get("region"),
                "software_listings_scanned": sum(
                    1 for j in payload.get("jobs") or [] if isinstance(j, dict)
                ),
                "companies": names,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )


def refresh() -> list[str]:
    """Pull the feed, extract software employers, cache them. Returns the names.

    A network failure is not fatal to discovery: whatever was cached last time
    is still good, so the error is reported and the cached list returned.
    """
    try:
        payload = fetch_feed()
    except Exception as exc:  # noqa: BLE001 — never let a third party break discover
        cached = load_seeds()
        print(f"  InternDoor feed unavailable ({exc}); using {len(cached)} cached seeds.")
        return cached

    names = extract_companies(payload)
    save_seeds(payload, names)
    print(
        f"  InternDoor: {payload.get('techCount', len(payload.get('jobs') or []))} "
        f"software listings -> {len(names)} distinct employers"
    )
    return names