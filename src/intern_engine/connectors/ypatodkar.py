"""ypatodkar's VC job directory, read for remote internships only.

The site itself is client-rendered; the page's own script pulls
`/api/all-jobs.json` and filters in the browser. That single JSON file is the
whole dataset, so we read it directly rather than scraping markup.

What we take: `seniority == "intern"` AND `remote == true`. Nothing else. This
board is US-only (19 US metros), so its roles would be vetoed by the India region
filter anyway — the ones worth publishing are the remote internships, and those
go into their own "Global remote" section rather than pretending to be Indian.

Every apply link is the employer's own ATS posting (lever/ashby/greenhouse URLs
are carried through verbatim), so nothing here is a dead aggregator page.

Scope note that matters for honesty: `remote: true` on this board means remote
*within the employer's country*. Nearly all of it is US-only and gated on US
work authorisation. `scope="global"` marks these so the renderer can label the
section rather than quietly implying they are open in India.
"""

from __future__ import annotations

import re

from ..models import Job
from ..net import Net

URL = "https://jobs.ypatodkar.com/api/all-jobs.json"

SOURCE = "ypatodkar"

# `companies` is a list and each job points at it by index. Older shapes used a
# dict keyed by index-as-string, so both are handled.
def _company_name(job: dict, companies) -> str:
    idx = job.get("c")
    entry = None
    if isinstance(companies, list) and isinstance(idx, int) and 0 <= idx < len(companies):
        entry = companies[idx]
    elif isinstance(companies, dict) and idx is not None:
        entry = companies.get(str(idx))
    if isinstance(entry, dict):
        return (entry.get("n") or entry.get("name") or "").strip()
    if isinstance(entry, str):
        return entry.strip()
    return ""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "unknown"


def _posted(value) -> str | None:
    """Their timestamps are ISO with milliseconds; we only ever keep the day."""
    if not value:
        return None
    return str(value)[:10] or None


async def fetch(company: dict, net: Net) -> list[Job]:
    data = await net.get_json(URL)
    companies = data.get("companies")
    postings = data.get("jobs") or []

    jobs: list[Job] = []
    seen: set[str] = set()
    for posting in postings:
        if not isinstance(posting, dict):
            continue
        if posting.get("seniority") != "intern" or not posting.get("remote"):
            continue

        name = _company_name(posting, companies)
        title = (posting.get("title") or "").strip()
        url = (posting.get("url") or "").strip()
        if not name or not title or not url:
            continue

        job_id = posting.get("job_id") or url
        if job_id in seen:
            continue
        seen.add(job_id)

        jobs.append(
            Job(
                id=f"{SOURCE}:{_slug(name)}:{job_id}",
                source=SOURCE,
                company=name,
                company_slug=_slug(name),
                title=title,
                # Deliberately the bare word. Any US city or state token in here
                # trips filters.is_united_states and the role is dropped before
                # it ever reaches the renderer; the section heading and its note
                # carry the "US-based" caveat instead of the location cell.
                location="Remote",
                url=url,
                posted_at=_posted(posting.get("posted")),
                salary=(posting.get("salary") or None),
                scope="global",
            )
        )

    # Raises rather than returning []: an empty return counts as a successful
    # fetch, and store.upsert would then close every role we published from
    # this board. A dead third party must not read as "they hired nobody".
    if not jobs:
        raise RuntimeError(
            f"ypatodkar: no remote internships in {len(postings)} postings "
            "(feed shape may have changed)"
        )
    return jobs