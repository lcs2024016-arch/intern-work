"""Turn candidate slugs into a validated registry.

Probes each candidate slug against the ATS APIs and merges confirmed hits into
data/companies.json.

Candidates come from data/candidates.json when that file exists (a
hand-curated list of slugs worth checking), and otherwise are generated from
the seed name lists via slug_variants. The previous version required
candidates.json to exist and did nothing at all without it, which made
`python run.py harvest` a no-op on every scheduled run.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import discover, paths
from .slug_variants import generate_candidates

# Only the slug-guessable platforms. Workday and Oracle tenants are not
# derived from company names, so they cannot be probed — discover mines them
# out of apply URLs instead.
PROBES = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
    "workable": "https://apply.workable.com/api/v3/accounts/{slug}/jobs",
}

HEADERS = {"User-Agent": "intern-engine-india/2.0"}

_MAX_WORKERS = 12
_TIMEOUT = 12


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


def detect(candidate: dict, client: httpx.Client) -> dict | None:
    """Return a registry record for the first platform that confirms this slug."""
    slug = candidate["slug"]
    for ats, template in PROBES.items():
        try:
            if ats == "workable":
                resp = client.post(
                    template.format(slug=slug),
                    json={"query": "", "location": [], "department": []},
                    timeout=_TIMEOUT,
                )
            else:
                resp = client.get(template.format(slug=slug), timeout=_TIMEOUT)
            if resp.status_code == 200 and _count(ats, resp.json()) > 0:
                return {"name": candidate["name"], "slug": slug, "ats": ats}
        except Exception:  # noqa: BLE001 — a dead platform must not stop the sweep
            continue
    return None


def _load_candidates() -> list[dict]:
    """Explicit candidates if present, else slugs generated from the seeds."""
    if os.path.exists(paths.CANDIDATES_PATH):
        try:
            with open(paths.CANDIDATES_PATH, encoding="utf-8") as f:
                rows = json.load(f)
            if rows:
                return [{"name": r.get("name", r.get("slug", "")), "slug": r["slug"]} for r in rows if r.get("slug")]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass

    print("  No candidates.json — generating slugs from the seed name lists.")
    out: list[dict] = []
    seen: set[str] = set()
    for name in discover._load_seed_names():
        for slug in generate_candidates(name):
            if slug not in seen:
                seen.add(slug)
                out.append({"name": name, "slug": slug})
    return out


def harvest() -> tuple[list[dict], list[dict]]:
    candidates = _load_candidates()
    if not candidates:
        print("No candidates to probe — skipping harvest.")
        return [], []

    client = httpx.Client(headers=HEADERS, follow_redirects=True)
    found: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            for result in pool.map(lambda c: detect(c, client), candidates):
                if result:
                    found.append(result)
    finally:
        client.close()

    merged: dict[tuple[str, str], dict] = {}
    try:
        with open(paths.COMPANIES_PATH, encoding="utf-8") as f:
            for c in json.load(f):
                merged[(c["ats"], c["slug"])] = c
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    for c in found:
        merged.setdefault((c["ats"], c["slug"]), c)

    companies = sorted(merged.values(), key=lambda c: c["name"].lower())
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    with open(paths.COMPANIES_PATH, "w", encoding="utf-8") as f:
        json.dump(companies, f, indent=2, ensure_ascii=False)

    return found, candidates