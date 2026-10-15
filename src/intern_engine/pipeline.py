"""The watcher + spotter.

One async pass per run: quarantine-check every tracked company (circuit breaker),
fetch the healthy ones concurrently (global + per-host concurrency caps),
normalize into one shape, keep the roles that match the configured scope,
de-duplicate across sources, enrich the keepers with the posting's full text
(skills, pay, degree), merge into the store, and record run metrics + a history
line.

The publish layer is deliberately NOT in here. run_update returns
(stats, store, new_ids) and run.py owns every rendering step, so a broken
renderer can never take down a scrape.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import httpx

from . import (
    compensation,
    config,
    enrich,
    filters,
    health,
    models,
    paths,
    quality,
    store,
)
from .connectors import (
    amazon,
    ashby,
    bloomberg,
    breezy,
    bytedance,
    custom_careers,
    eightfold,
    google,
    greenhouse,
    icims,
    indeed,
    instahyre,
    internshala,
    lever,
    linkedin,
    naukri,
    oracle,
    recruitee,
    rippling,
    smartrecruiters,
    successfactors,
    uber,
    unstop,
    wellfound,
    workable,
    workday,
    yc_waas,
)
from .net import HostLimiter, Net

# Sources that block datacentre/cloud IPs far more aggressively than the rest.
# Routed through WORKDAY_PROXY when one is configured.
_PROXIED_ATS = ("workday", "oracle")

CONNECTORS = {
    # --- mainstream ATS ---
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "workable": workable.fetch,
    "workday": workday.fetch,
    "oracle": oracle.fetch,
    "rippling": rippling.fetch,
    "breezy": breezy.fetch,
    "recruitee": recruitee.fetch,
    "eightfold": eightfold.fetch,
    # --- enterprise and custom careers sites ---
    "amazon": amazon.fetch,
    "custom": custom_careers.fetch,
    "google": google.fetch,
    "icims": icims.fetch,
    "successfactors": successfactors.fetch,
    "bloomberg": bloomberg.fetch,
    "bytedance": bytedance.fetch,
    "uber": uber.fetch,
    # --- indian aggregators ---
    "internshala": internshala.fetch,
    "naukri": naukri.fetch,
    "instahyre": instahyre.fetch,
    "unstop": unstop.fetch,
    "wellfound": wellfound.fetch,
    "yc_waas": yc_waas.fetch,
    # --- keyword aggregators (their `slug` is a search term, not a board) ---
    # These rate-limit hard from datacentre IPs. The health circuit breaker is
    # what keeps them cheap: repeated failure backs each keyword off
    # exponentially up to 72h, so a blocked run costs three requests, not 200.
    "linkedin": linkedin.fetch,
    "indeed": indeed.fetch,
}

GLOBAL_CONCURRENCY = 32
PER_HOST_CONCURRENCY = 8
USER_AGENT = f"intern-engine-india/2.0 (+https://github.com/{config.repo_slug()})"


def _load_companies() -> list[dict]:
    with open(paths.COMPANIES_PATH, encoding="utf-8") as f:
        return json.load(f)


async def _fetch_one(company: dict, net: Net):
    """Return (company, jobs, error); never raises (failures are isolated)."""
    fetch = CONNECTORS.get(company.get("ats"))
    if fetch is None:
        return company, [], f"no connector for {company.get('ats')}"
    try:
        return company, await fetch(company, net), None
    except Exception as exc:  # noqa: BLE001 — one bad endpoint must not stop a run
        return company, [], f"{type(exc).__name__}: {exc}"


async def _fetch_all(companies: list[dict], enrich_after):
    """Fetch every company, then run `enrich_after(results, net, workday_net)` on
    the same client session so enrichment reuses open connections."""
    limiter = HostLimiter(PER_HOST_CONCURRENCY)
    gate = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    proxy = os.environ.get("WORKDAY_PROXY") or None

    common = dict(
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )

    async with httpx.AsyncClient(**common) as client:
        default_net = Net(client, limiter)
        workday_client = (
            httpx.AsyncClient(proxy=proxy, **common) if proxy else None
        )
        workday_net = Net(workday_client, limiter) if workday_client else default_net

        async def worker(company: dict):
            net = (
                workday_net if company.get("ats") in _PROXIED_ATS else default_net
            )
            async with gate:
                return await _fetch_one(company, net)

        try:
            results = await asyncio.gather(*(worker(c) for c in companies))
            enrich_result = await enrich_after(results, default_net, workday_net)
            return results, enrich_result
        finally:
            if workday_client is not None:
                await workday_client.aclose()


def _dedup(jobs: list) -> list:
    """Collapse the same role seen more than once (e.g. via two ATS).

    Keyed by company + normalized title; a posting carrying a real date wins
    over one without.
    """
    jobs = sorted(jobs, key=lambda j: (j.posted_at is None,))
    seen: set[tuple[str, str]] = set()
    unique = []
    for job in jobs:
        key = (
            job.company.lower().strip(),
            re.sub(r"[^a-z0-9]+", "", job.title.lower()),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def _apply_company_cap(jobs: list, cap: int) -> list:
    """Keep at most `cap` roles per company, newest first.

    Without this a handful of huge tenants (and the aggregators, which carry
    hundreds of companies at once) crowd the rest of the list off the page.
    """
    if cap <= 0:
        return jobs
    kept: list = []
    counts: Counter = Counter()
    for job in sorted(jobs, key=lambda j: (j.posted_at or ""), reverse=True):
        key = job.company.lower().strip()
        if counts[key] >= cap:
            continue
        counts[key] += 1
        kept.append(job)
    return kept


def _keep_matching(results, cfg, blocklist, existing=None):
    """Classify every fetched role. Returns the kept roles plus run counters."""
    cycles = config.cycles(cfg)
    tech_only = cfg.get("role_scope", "tech") == "tech"
    restrict = config.restrict_region(cfg)
    wants_us = config.want_us(cfg)
    wants_canada = config.want_canada(cfg)
    wants_india = config.want_india(cfg)
    wants_remote = config.want_remote(cfg)
    international = config.include_international(cfg)
    allowlist = config.allowlist_only(cfg)
    infer = config.infer_undated(cfg)
    infer_age = config.infer_max_age_days(cfg)
    max_age = config.max_age_days(cfg)
    cutoff = (
        (datetime.now(UTC) - timedelta(days=max_age)).strftime("%Y-%m-%d")
        if max_age
        else None
    )

    existing = existing or {}
    kept: list = []
    succeeded: set[str] = set()
    errors = 0
    errors_by_ats: Counter = Counter()
    dropped_no_year = 0
    dropped_offcycle = 0

    for company, jobs, error in results:
        if error is not None:
            errors += 1
            errors_by_ats[company.get("ats", "?")] += 1
            continue
        succeeded.add(f"{company['ats']}:{company['slug']}")
        if quality.is_blocked(company["name"], blocklist):
            continue
        if allowlist and not quality.is_recognized(company["name"]):
            continue
        for job in jobs:
            if not filters.is_internship(job.title):
                continue
            if tech_only and not filters.is_tech(job.title):
                continue

            season = filters.detect_season(job.title, cycles)
            inferred = False
            if season is None:
                if filters.states_explicit_year(job.title):
                    # Names a year that is not a cycle we track (a 2026 role
                    # while we follow Summer 2027). Let it go permanently
                    # rather than re-deciding on every run.
                    dropped_offcycle += 1
                    continue
                prior = existing.get(job.id) or {}
                prior_season = prior.get("season")
                if prior_season in cycles:
                    season = prior_season
                    inferred = bool(prior.get("season_inferred"))
                elif infer:
                    season = filters.infer_season(
                        job.title, job.posted_at, cycles, infer_age
                    )
                    inferred = season is not None
            if season is None:
                season = "Term Unconfirmed"
                inferred = True

            if restrict:
                in_region = filters.region_ok(
                    job.location,
                    wants_us,
                    wants_canada,
                    wants_india,
                    wants_remote,
                    source=job.source,
                )
                if not in_region:
                    # A role with a real location outside the configured
                    # regions is only worth keeping when the user asked for
                    # them explicitly.
                    if not international or not job.location:
                        continue

            posted_day = (job.posted_at or "")[:10]
            if cutoff and posted_day and posted_day < cutoff:
                continue

            job.season = season
            job.season_inferred = inferred
            job.category = filters.categorize(job.title)
            kept.append(job)

    return kept, succeeded, errors, errors_by_ats, dropped_no_year, dropped_offcycle


async def _enrich_stage(results, net: Net, workday_net: Net):
    """Keep, de-duplicate, then pull full posting text for the survivors."""
    cfg = config.load_config()
    blocklist = quality.load_blocklist()
    existing = store.load(paths.JOBS_PATH)

    kept, succeeded, errors, errors_by_ats, no_year, offcycle = _keep_matching(
        results, cfg, blocklist, existing
    )
    kept = _dedup(kept)
    kept = _apply_company_cap(kept, config.max_per_company(cfg))

    enriched_ids: set[str] = set()
    detail_fetches = 0
    if config.enrich_enabled(cfg):
        # Enrich the non-proxied sources on one client and Workday/Oracle on
        # the other, so the proxy keeps doing its job during enrichment too.
        plain = [j for j in kept if j.source not in _PROXIED_ATS]
        proxied = [j for j in kept if j.source in _PROXIED_ATS]
        limit = config.max_enrich_per_run(cfg)
        ids_a, fetches_a = await enrich.enrich_jobs(plain[:limit], existing, net)
        ids_b, fetches_b = await enrich.enrich_jobs(proxied[:limit], existing, workday_net)
        enriched_ids = ids_a | ids_b
        detail_fetches = fetches_a + fetches_b

        # A full posting can name the cycle even when the title does not.
        # Correct the inferred bucket and clear the flag when it does.
        cycles = config.cycles(cfg)
        for job in kept:
            if not job.season_inferred:
                continue
            from_text = filters.detect_season_from_description(
                job.description or "", cycles
            )
            if from_text:
                job.season = from_text
                job.season_inferred = False

    compensation.save_cache()
    return (
        kept,
        succeeded,
        errors,
        errors_by_ats,
        enriched_ids,
        detail_fetches,
        no_year,
        offcycle,
    )


def _detection_latency(existing: dict, window_days: int = 7) -> dict:
    """How long a role sat live before this engine first saw it.

    The gap between the company's published date and our first_seen_at. Only
    meaningful for roles whose posted_at is a real date.
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    deltas: list[float] = []
    for record in existing.values():
        posted = (record.get("posted_at") or "")[:10]
        first_seen = record.get("first_seen_at") or ""
        if not posted or not first_seen:
            continue
        try:
            p = datetime.fromisoformat(posted + "T00:00:00+00:00")
            f = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        except ValueError:
            continue
        if p < cutoff or f < p:
            continue  # outside the window, or first seen before it was posted
        deltas.append((f - p).total_seconds() / 60)
    if not deltas:
        return {"median_minutes": None, "sample_size": 0, "window_days": window_days}
    return {
        "median_minutes": round(statistics.median(deltas), 1),
        "sample_size": len(deltas),
        "window_days": window_days,
    }


def _posted_date_coverage(records: list[dict]) -> float:
    if not records:
        return 0.0
    dated = sum(1 for r in records if r.get("posted_at"))
    return round(dated / len(records), 3)


def run_update() -> tuple[dict, dict, list[str]]:
    cfg = config.load_config()
    blocklist = quality.load_blocklist()
    companies = _load_companies()
    existing = store.load(paths.JOBS_PATH)

    health_data = health.load()

    # The YC WaaS aggregator is not a company board, so it is injected as a
    # virtual one rather than living in companies.json.
    companies.append(
        {
            "name": "Y Combinator (Work at a Startup)",
            "slug": "yc",
            "ats": "yc_waas",
        }
    )

    active, benched = health.partition(companies, health_data)

    started = time.monotonic()
    print(f"Fetching {len(active)} companies ({len(benched)} quarantined)...")

    results, enrich_result = asyncio.run(_fetch_all(active, _enrich_stage))
    (
        kept,
        succeeded,
        errors,
        errors_by_ats,
        enriched_ids,
        detail_fetches,
        dropped_no_year,
        dropped_offcycle,
    ) = enrich_result

    for company, _jobs, error in results:
        health.record(health_data, company, error)
    health.save(health_data)

    rows = []
    for job in kept:
        row = asdict(job)
        for field in models.TRANSIENT_FIELDS:
            row.pop(field, None)
        rows.append(row)

    new_ids = store.upsert(existing, rows, succeeded, enriched_ids)
    purged = store.purge(existing)
    store.save(paths.JOBS_PATH, existing)

    duration = round(time.monotonic() - started, 1)
    open_records = [r for r in existing.values() if r.get("is_open")]
    attempted = len(companies) - len(benched)

    stats = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": duration,
        "companies_total": len(companies),
        "companies_by_source": dict(Counter(c["ats"] for c in companies)),
        "quarantined": len(benched),
        "fetched_ok": len(succeeded),
        "fetch_errors": errors,
        "errors_by_source": dict(errors_by_ats),
        "fetch_success_rate": round(len(succeeded) / max(attempted, 1), 3),
        "roles_matched": len(kept),
        "roles_cycle_inferred": sum(1 for j in kept if j.season_inferred),
        "dropped_no_year_in_title": dropped_no_year,
        "dropped_offcycle_by_text": dropped_offcycle,
        "roles_by_source": dict(Counter(j.source for j in kept)),
        "roles_by_cycle": dict(Counter(j.season for j in kept)),
        "roles_by_category": dict(Counter(j.category for j in kept)),
        "roles_by_region": dict(
            Counter(
                (
                    "India"
                    if filters.is_india(j.location)
                    else (
                        "Remote" if filters.is_remote_or_hybrid(j.location) else "Other"
                    )
                )
                for j in kept
            )
        ),
        "posted_date_coverage": _posted_date_coverage(open_records),
        "enriched_this_run": len(enriched_ids),
        "enrichment_detail_fetches": detail_fetches,
        "purged_this_run": purged,
        "new_this_run": len(new_ids),
        "open_total": len(open_records),
        "detection_latency": _detection_latency(existing),
    }

    with open(paths.STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    _append_history(stats)
    return stats, existing, new_ids


_HISTORY_KEEP = 2000


def _append_history(stats: dict) -> None:
    line = json.dumps(
        {
            "ts": stats["generated_at"],
            "open": stats["open_total"],
            "new": stats["new_this_run"],
            "companies": stats["companies_total"],
            "ok_rate": stats["fetch_success_rate"],
            "quarantined": stats["quarantined"],
            "secs": stats["duration_seconds"],
        },
        ensure_ascii=False,
    )
    lines = []
    try:
        with open(paths.HISTORY_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    lines.append(line)
    os.makedirs(os.path.dirname(paths.HISTORY_PATH), exist_ok=True)
    with open(paths.HISTORY_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines[-_HISTORY_KEEP:]) + "\n")