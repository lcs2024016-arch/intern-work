"""Workday (enterprise tier) — the biggest ATS in intern hiring.

Two public URL shapes exist and both are supported:
  subdomain:  https://{tenant}.{wd}.myworkdayjobs.com/{site}
  path-style: https://{wd}.myworkdaysite.com/recruiting/{tenant}/{site}
The CXS JSON API lives under /wday/cxs/{tenant}/{site} on either host.

Three things this connector gets right that a naive one does not:

  * It asks Workday for "intern" in the search box. The endpoint returns the
    whole board otherwise, which for a large tenant is thousands of postings
    across dozens of pages, all of which the pipeline then throws away.

  * It resolves Workday's relative dates. `postedOn` is free text like
    "Posted 6 Days Ago", never an ISO date. Left alone, every Workday role
    looks undated: the age cutoff silently never fires and the newest-first
    sort degenerates into insertion order. Here it becomes a real date.
    "30+ Days Ago" resolves to None rather than to a guess.

  * It reads the data centre from `wd`, which is what the registry actually
    stores. The old connector read `dc`, a key that does not exist in
    companies.json, so every tenant fell through to probing all five data
    centres in sequence — five times the requests, mostly to 404s.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from ..models import Job
from ..net import Net

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
}

_DAYS_AGO = re.compile(r"(\d+)\s*\+?\s*days?\s+ago", re.IGNORECASE)

_PAGE_SIZE = 20  # hard server-side cap per request
_MAX_JOBS = 100  # intern-search results beyond this are noise

# Only used for the handful of registry rows that never recorded a data centre.
_FALLBACK_DCS = ("wd5", "wd1", "wd3")


def _resolve_posted(text: str | None) -> str | None:
    """Workday's free-text 'postedOn' into an ISO date, or None if unknowable."""
    if not text:
        return None
    lowered = text.lower()
    if "today" in lowered:
        days = 0
    elif "yesterday" in lowered:
        days = 1
    elif "30+" in lowered:
        return None  # too vague to be a real date
    else:
        match = _DAYS_AGO.search(lowered)
        if not match or int(match.group(1)) >= 30:
            return None
        days = int(match.group(1))
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")


def _urls(company: dict, dc: str | None) -> tuple[str, str]:
    """(cxs jobs endpoint, public base URL) for either Workday host shape."""
    tenant, site = company["slug"], company.get("site") or ""
    host = company.get("host")  # set for path-style tenants by discovery
    if host:
        return (
            f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
            f"https://{host}/recruiting/{tenant}/{site}",
        )
    sub_host = f"{tenant}.{dc}.myworkdayjobs.com"
    return (
        f"https://{sub_host}/wday/cxs/{tenant}/{site}/jobs",
        f"https://{sub_host}/{site}",
    )


def _location(posting: dict) -> str:
    locs = posting.get("locationsText")
    if isinstance(locs, list):
        locs = ", ".join(locs)
    if not locs:
        bullets = posting.get("bulletFields") or []
        locs = ", ".join(b for b in bullets if isinstance(b, str))
    return str(locs).strip() or "—"


def _body(offset: int) -> dict:
    return {
        "appliedFacets": {},
        "limit": _PAGE_SIZE,
        "offset": offset,
        # Ask the board for interns. Without this the endpoint returns the
        # entire tenant and we download thousands of roles to discard them.
        "searchText": "intern",
    }


async def fetch(company: dict, net: Net) -> list[Job]:
    tenant = company.get("slug")
    if not tenant or not company.get("site"):
        raise ValueError(
            f"Workday connector needs 'slug' (tenant) and 'site' — got {company}"
        )

    # Registry rows store the data centre as "wd"; older rows may use "dc";
    # and a few have neither, in which case probe the common ones once.
    dc = company.get("wd") or company.get("dc")
    candidates = [dc] if dc else list(_FALLBACK_DCS)

    async def _first_page(api_url: str) -> dict | None:
        """Return page 1, or None when this endpoint is not a live board."""
        try:
            data = await net.post_json(api_url, json=_body(0), headers=HEADERS)
        except Exception:
            return None
        return data if isinstance(data, dict) and "jobPostings" in data else None

    api_url = base = ""
    first: dict | None = None
    for candidate in candidates:
        api_url, base = _urls(company, candidate)
        first = await _first_page(api_url)
        if first is not None:
            break
    if first is None:
        return []

    jobs: list[Job] = []

    def _parse(data: dict) -> None:
        for posting in data.get("jobPostings", []):
            path = posting.get("externalPath") or ""
            jobs.append(
                Job(
                    id=f"workday:{tenant}:{path or posting.get('title')}",
                    source="workday",
                    company=company["name"],
                    company_slug=tenant,
                    title=(posting.get("title") or "").strip(),
                    location=_location(posting),
                    url=(base + path) if path else base,
                    posted_at=_resolve_posted(posting.get("postedOn")),
                )
            )

    _parse(first)
    if len(jobs) >= _MAX_JOBS:
        return jobs[:_MAX_JOBS]

    for offset in range(_PAGE_SIZE, _MAX_JOBS, _PAGE_SIZE):
        try:
            data = await net.post_json(api_url, json=_body(offset), headers=HEADERS)
        except Exception:
            break
        postings = data.get("jobPostings", [])
        _parse(data)
        if len(postings) < _PAGE_SIZE:
            break

    return jobs[:_MAX_JOBS]