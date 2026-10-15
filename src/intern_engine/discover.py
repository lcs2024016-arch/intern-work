"""Discover companies at scale from public internship datasets + companies.txt.

Mines public datasets for ATS tokens AND brute-forces our companies.txt seed list
using multi-candidate slug probing.
Results are merged into data/companies.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime

import httpx

from . import config, interndoor, paths
from .slug_variants import generate_candidates

# Public datasets to mine for ATS tokens (company names + apply URLs).
JSON_SOURCES = [
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2025-Internships/dev/.github/scripts/listings.json",
]

# ATS URL patterns to extract tokens from apply URLs.
_PATTERNS = {
    "greenhouse": [
        re.compile(
            r"(?:job-boards|boards)\.greenhouse\.io/([a-z0-9][a-z0-9_\-]*)", re.I
        ),
    ],
    "lever": [re.compile(r"jobs\.lever\.co/([a-z0-9][a-z0-9_\-]*)", re.I)],
    "ashby": [re.compile(r"jobs\.ashbyhq\.com/([a-z0-9][a-z0-9_\-]*)", re.I)],
    "smartrecruiters": [re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9][\w\-]*)")],
    "workable": [re.compile(r"apply\.workable\.com/([a-z0-9][\w\-]*)/j/", re.I)],
}

_BLOCKLIST = {"jobs", "www", "careers", "job", "embed", "search", "api", "boards"}

# Workday and Oracle tenants cannot be slug-guessed: the tenant is not derived
# from the company name ("Larsen & Toubro" -> "larsentoubro" is not a tenant),
# and the careers URL carries the only copy of it. So these are mined from
# apply URLs in the datasets instead of probed.
#
# This is the single biggest coverage lever for India: TCS, Infosys, Wipro,
# Cummins, Standard Chartered and most large enterprises run Workday or Oracle
# Recruiting, and slug-probing five small ATS platforms never finds any of them.
_WD_SUB_RE = re.compile(
    r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w%-]+)",
    re.I,
)
_WD_SITE_RE = re.compile(
    r"https://(wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([\w-]+)/([\w%-]+)",
    re.I,
)
_WD_BAD_SITES = {"job", "jobs", "en", "en-us", "recruiting", "login"}

# Oracle Recruiting Cloud: per-tenant host plus a site number (CX_1, CX_2001...).
_ORC_RE = re.compile(
    r"https://([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z\-]+/sites/(CX_[\w]+)",
    re.I,
)

# ATS endpoints for brute-force probing (from companies.txt).
_PROBE_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
    "workable": "https://apply.workable.com/api/v3/accounts/{slug}/jobs",
}

HEADERS = {"User-Agent": "intern-engine-india/1.0"}


def _prettify(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def _count(ats: str, payload) -> int:
    if ats == "lever":
        return len(payload) if isinstance(payload, list) else 0
    if ats == "smartrecruiters":
        if isinstance(payload, dict):
            return payload.get("totalFound", len(payload.get("content", [])))
        return 0
    if ats == "workable":
        return len(payload.get("results", [])) if isinstance(payload, dict) else 0
    return len(payload.get("jobs", [])) if isinstance(payload, dict) else 0


def _extract_from_datasets(
    session: httpx.Client,
) -> dict[tuple[str, str], dict]:
    """Mine public JSON datasets for ATS tokens.

    Returns {(ats, slug): registry_record}. Every value is a full record in
    the shape companies.json stores, so the caller can merge without caring
    whether the token came from slug-guessable ATS or from a Workday/Oracle
    URL.
    """
    found: dict[tuple[str, str], dict] = {}

    for url in JSON_SOURCES:
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                data = data.get("listings") or list(data.values())
            text = json.dumps(data)

            # Learn names from listings
            names: dict[str, str] = {}
            for item in (data if isinstance(data, list) else []):
                if not isinstance(item, dict):
                    continue
                name = (item.get("company_name") or "").strip()
                if not name:
                    continue
                blob = " ".join(str(item.get(k, "")) for k in ("url", "company_url"))
                for ats, patterns in _PATTERNS.items():
                    for pattern in patterns:
                        for slug in pattern.findall(blob):
                            if ats != "smartrecruiters":
                                slug = slug.lower()
                            names.setdefault(f"{ats}:{slug.lower()}", name)

            for ats, patterns in _PATTERNS.items():
                for pattern in patterns:
                    for slug in pattern.findall(text):
                        if ats != "smartrecruiters":
                            slug = slug.lower()
                        if slug.lower() in _BLOCKLIST:
                            continue
                        name = names.get(f"{ats}:{slug.lower()}") or _prettify(slug)
                        found.setdefault(
                            (ats, slug),
                            {"name": name, "slug": slug, "ats": ats},
                        )

            # Workday / Oracle tenants, which slug-guessing can never find. Both
            # are keyed on the tenant/host alone: the site is part of the
            # record, not the identity, and one tenant can serve several.
            for (tenant, _site), record in _extract_workday(text, names).items():
                found.setdefault(("workday", tenant), record)
            for (host, _site), record in _extract_oracle(text, names).items():
                found.setdefault(("oracle", host), record)
        except Exception as exc:
            print(f"  dataset failed: {url} ({exc})")

    return found


def _extract_workday(text: str, names: dict[str, str]) -> dict[tuple[str, str], dict]:
    """{(tenant, site): record} for every Workday tenant in a text blob."""
    found: dict[tuple[str, str], dict] = {}
    for m in _WD_SUB_RE.finditer(text):
        tenant, wd, site = m.group(1).lower(), m.group(2).lower(), m.group(3)
        if site.lower() in _WD_BAD_SITES:
            continue
        name = names.get(f"workday:{tenant}") or _prettify(tenant)
        found.setdefault(
            (tenant, site),
            {
                "name": name,
                "slug": tenant,
                "ats": "workday",
                "wd": wd,
                "site": site,
            },
        )
    for m in _WD_SITE_RE.finditer(text):
        host, tenant, site = m.group(1).lower(), m.group(2).lower(), m.group(3)
        if site.lower() in _WD_BAD_SITES:
            continue
        name = names.get(f"workday:{tenant}") or _prettify(tenant)
        found.setdefault(
            (tenant, site),
            {
                "name": name,
                "slug": tenant,
                "ats": "workday",
                "wd": host.split(".")[0],
                "site": site,
                "host": host,
            },
        )
    return found


def _extract_oracle(text: str, names: dict[str, str]) -> dict[tuple[str, str], dict]:
    """{(host, site): record} for every Oracle Recruiting Cloud tenant."""
    found: dict[tuple[str, str], dict] = {}
    for m in _ORC_RE.finditer(text):
        host, site = m.group(1).lower(), m.group(2)
        name = names.get(f"oracle:{host}") or _prettify(host.split(".")[0])
        found.setdefault(
            (host, site),
            {
                "name": name,
                "slug": host,
                "ats": "oracle",
                "host": host,
                "site": site,
            },
        )
    return found


def _norm_key(name: str) -> str:
    """Fold a company name to letters and digits only.

    "Larsen & Toubro", "larsen and toubro" and "LARSEN-TOUBRO" all collapse
    to the same key, which is what lets a Workday tenant slug be matched back
    to a human-typed company name.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _load_seed_names() -> list[str]:
    """companies.txt plus the cached InternDoor seeds."""
    names: list[str] = []
    if os.path.exists(paths.COMPANIES_TXT_PATH):
        with open(paths.COMPANIES_TXT_PATH, encoding="utf-8") as f:
            names += [line.strip() for line in f if line.strip()]
    names += interndoor.load_seeds()
    return list(dict.fromkeys(names))


def _improve_names(merged: dict[tuple[str, str], dict], seeds: list[str]) -> int:
    """Rename tenants whose display name is just a prettified slug.

    Workday and Oracle tokens are mined from URLs, so most arrive with no name
    attached and get "_prettify(slug)" applied — which turns tenant
    "fa-epmd-saasfaprod1" into "Fa Epmd Saasfaprod1".

    The replacement has to be an improvement, not merely different. A first cut
    of this rewrote 495 perfectly good names ("Accenture", "Adobe", "Airbnb")
    into the all-lowercase spelling used in companies.txt, because those names
    also normalize to their own slug. So the guard is monotonic: only adopt a
    seed name when it carries strictly MORE capitals than the one on file, and
    when it normalizes to the same key. A name can only ever gain readability
    here.
    """
    index: dict[str, str] = {}
    for name in seeds:
        key = _norm_key(name)
        if not key:
            continue
        current = index.get(key)
        if current is None or _capitals(name) > _capitals(current):
            index[key] = name

    renamed = 0
    for record in merged.values():
        slug = record.get("slug") or ""
        current = record.get("name") or ""
        if not slug or _norm_key(current) != _norm_key(slug):
            continue
        better = index.get(_norm_key(slug))
        if better and better != current and _capitals(better) > _capitals(current):
            record["name"] = better
            renamed += 1
    return renamed


def _capitals(text: str) -> int:
    return sum(1 for ch in text if ch.isupper())


def _log_attempt(
    log_entries: list[dict],
    company: str,
    candidate: str,
    ats: str,
    status: int,
    jobs: int,
) -> None:
    """Buffer a probe attempt for later flush to JSONL."""
    log_entries.append(
        {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "company": company,
            "candidate": candidate,
            "ats": ats,
            "status": status,
            "jobs": jobs,
        }
    )


def _flush_log(log_entries: list[dict]) -> None:
    """Append buffered log entries to the discover attempts JSONL file."""
    if not log_entries:
        return
    os.makedirs(os.path.dirname(paths.DISCOVER_LOG_PATH), exist_ok=True)
    with open(paths.DISCOVER_LOG_PATH, "a", encoding="utf-8") as f:
        for entry in log_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def _probe_company(
    client: httpx.AsyncClient,
    name: str,
    sem: asyncio.Semaphore,
    log_entries: list[dict],
) -> dict | None:
    """Try multiple slug candidates × all ATS platforms; short-circuit on first hit."""
    candidates = generate_candidates(name)
    for candidate in candidates:
        for ats, template in _PROBE_URLS.items():
            url = template.format(slug=candidate)
            status = 0
            jobs = 0
            try:
                async with sem:
                    if ats == "workable":
                        resp = await client.post(
                            url,
                            json={
                                "query": "",
                                "location": [],
                                "department": [],
                                "worktype": [],
                                "remote": [],
                            },
                            timeout=8,
                        )
                    else:
                        resp = await client.get(url, timeout=8)
                status = resp.status_code
                if status == 200:
                    payload = resp.json()
                    jobs = _count(ats, payload)
                    if jobs > 0:
                        _log_attempt(log_entries, name, candidate, ats, status, jobs)
                        return {"slug": candidate, "ats": ats}
            except Exception:
                pass
            _log_attempt(log_entries, name, candidate, ats, status, jobs)
    return None


async def _probe_all(slugs_to_names: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Probe all companies concurrently with multi-candidate slug variants."""
    sem = asyncio.Semaphore(16)
    log_entries: list[dict] = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        tasks = [
            _probe_company(client, name, sem, log_entries) for name in slugs_to_names
        ]
        results = await asyncio.gather(*tasks)
    hits = [r for r in results if r]
    return hits, log_entries


def _update_tracked_untracked(merged: dict[tuple[str, str], dict]) -> None:
    """Auto-generate tracked_companies.txt and untracked_companies.txt."""
    if not os.path.exists(paths.COMPANIES_TXT_PATH):
        return

    with open(paths.COMPANIES_TXT_PATH, encoding="utf-8") as f:
        all_names = sorted(set(line.strip() for line in f if line.strip()))

    # Build a set of all known slugs across all ATS entries
    known_slugs: set[str] = set()
    for c in merged.values():
        known_slugs.add(c["slug"])
        # Also match by normalized name
        normalized = re.sub(r"[^a-z0-9]+", "-", c["name"].lower()).strip("-")
        known_slugs.add(normalized)

    tracked: list[str] = []
    untracked: list[str] = []

    for name in all_names:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        # Check if any candidate from this name exists in the merged registry
        candidates = generate_candidates(name)
        found = (
            any((ats, cand) in merged for cand in candidates for ats in _PROBE_URLS)
            or slug in known_slugs
            or any(c in known_slugs for c in candidates)
        )

        if found:
            tracked.append(name)
        else:
            untracked.append(name)

    with open(paths.TRACKED_TXT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(tracked)) + "\n" if tracked else "")

    with open(paths.UNTRACKED_TXT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(untracked)) + "\n" if untracked else "")

    print(f"  Tracked: {len(tracked)} | Untracked: {len(untracked)}")


def discover() -> tuple[list[dict], int]:
    """Mine datasets + brute-force companies.txt -> data/companies.json."""
    # Load existing
    merged: dict[tuple[str, str], dict] = {}
    try:
        with open(paths.COMPANIES_PATH, encoding="utf-8") as f:
            for c in json.load(f):
                merged[(c["ats"], c["slug"])] = c
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    existing_slugs = {c["slug"] for c in merged.values()}
    n_before = len(merged)

    # Step 1: Mine public datasets
    print("Mining public datasets for ATS tokens...")
    with httpx.Client(headers=HEADERS, follow_redirects=True) as session:
        dataset_found = _extract_from_datasets(session)
    for (ats, slug), record in dataset_found.items():
        merged.setdefault((ats, slug), record)
    print(f"  Found {len(dataset_found)} tokens from datasets.")

    # Step 1b: Give the newly mined tenants a real display name.
    renamed = _improve_names(merged, seed_names := _load_seed_names())
    if renamed:
        print(f"  Renamed {renamed} tenants from their slug to a known company name.")

    # Step 2: Multi-candidate brute-force from the seed lists
    # Two seed sources, both plain company names:
    #   companies.txt          - hand-curated
    #   data/interndoor_seeds  - employers InternDoor lists a software role for
    seed_names: list[str] = []
    if os.path.exists(paths.COMPANIES_TXT_PATH):
        with open(paths.COMPANIES_TXT_PATH, encoding="utf-8") as f:
            seed_names += [line.strip() for line in f if line.strip()]

    # Cached InternDoor seeds are always used; only the network refresh is
    # opt-in, so a one-off refresh keeps feeding the list without `discover`
    # depending on a third party being up on every run.
    cached_seeds = interndoor.load_seeds()
    if config.include_interndoor(config.load_config()):
        cached_seeds = interndoor.refresh() or cached_seeds
    if cached_seeds:
        seed_names += cached_seeds
        print(f"  InternDoor seeds: {len(cached_seeds)} cached employers")

    if seed_names:
        all_names = list(dict.fromkeys(seed_names))  # de-dup, keep first seen

        # Filter out companies already tracked (any candidate slug already known)
        to_probe: dict[str, str] = {}  # name -> display_name
        for name in all_names:
            candidates = generate_candidates(name)
            already_known = any(
                cand in existing_slugs
                or any((ats, cand) in merged for ats in _PROBE_URLS)
                for cand in candidates
            )
            if not already_known:
                to_probe[name] = _prettify(name)

        if to_probe:
            print(
                f"Probing {len(to_probe)} companies ({sum(len(generate_candidates(n)) for n in to_probe)} total slug candidates)..."
            )
            probed, log_entries = asyncio.run(_probe_all(to_probe))

            # Flush attempt log
            _flush_log(log_entries)
            print(
                f"  Logged {len(log_entries)} probe attempts to {paths.DISCOVER_LOG_PATH}"
            )

            for hit in probed:
                slug = hit["slug"]
                ats = hit["ats"]
                # Find which original name this hit corresponds to
                display_name = None
                for name in to_probe:
                    if slug in generate_candidates(name):
                        display_name = to_probe[name]
                        break
                display_name = display_name or _prettify(slug)
                merged.setdefault(
                    (ats, slug), {"name": display_name, "slug": slug, "ats": ats}
                )
                print(f"  [+] {display_name} -> {ats} (slug: {slug})")
            print(f"  Probed: {len(probed)} hits out of {len(to_probe)} companies.")
        else:
            print("  No new companies to probe from the seed lists.")
    else:
        print("  No seed names found — skipping brute-force.")

    # Save
    companies = sorted(merged.values(), key=lambda c: c["name"].lower())
    os.makedirs(os.path.dirname(paths.COMPANIES_PATH), exist_ok=True)
    with open(paths.COMPANIES_PATH, "w", encoding="utf-8") as f:
        json.dump(companies, f, indent=2, ensure_ascii=False)

    n_found = len(merged) - n_before

    # Auto-generate tracked/untracked files
    _update_tracked_untracked(merged)

    return companies, n_found
