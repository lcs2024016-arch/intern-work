"""InternDoor's public India feed, read as a job source.

InternDoor aggregates India engineering openings and republishes them at
interndoor.com/data/jobs.json. `interndoor.py` (one level up) reads the same feed
as a *company list* for discovery; this module reads it as *roles*.

Two things make a republished listing safe to publish:

  - `applyUrl` is the employer's own ATS posting whenever InternDoor has
    resolved one, so the Apply link lands on the company's own page rather than
    on a third party. `url` (the LinkedIn mirror) is only the fallback.
  - `postedAt` is epoch milliseconds, i.e. a real published date rather than
    "3 days ago", so age filtering and the newest-first sort both work.

Scope: `category == "software"` only. The feed tags every listing, so the
filter is applied by us rather than trusting the `techCount` in their metadata
(that count is not filtered by anything we control).

Note on the 1,053 software listings: only ~15% have an intern/co-op/trainee
title. The rest are permanent roles that InternDoor lists alongside internships.
Those are dropped by the pipeline's own `filters.is_internship`, not here — the
feed is read once and classified once.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..models import Job
from ..net import Net

URL = "https://interndoor.com/data/jobs.json"

SOURCE = "interndoor"

SOFTWARE_CATEGORY = "software"

# Titles arrive decorated: "WoW Software Engineer B7 - AI (CCX Platform)" and
# bare "Intern" both appear. A bare title carries no company or role detail, so
# it is kept (the company column still identifies the posting) but never used as
# the dedup key on its own — see _external_id.
_BARE_TITLE_RE = re.compile(r"^(intern|internship|trainee|apprentice)\.?$", re.I)


def _is_software(job: dict) -> bool:
    category = str(job.get("category") or "").strip().lower()
    return category == SOFTWARE_CATEGORY or "software" in category


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "unknown"


def _title(job: dict) -> str:
    """Prefer the human title; slugTitle only when title is unusable.

    `title` is what the company wrote and is what the applicant recognises.
    `slugTitle` prefixes the company ("WoW Software Engineer B7 - ..."), which
    would corrupt both the title column and the cross-source dedup key.
    """
    title = " ".join(str(job.get("title") or "").split()).strip()
    if title and not _BARE_TITLE_RE.match(title):
        return title
    slug_title = " ".join(str(job.get("slugTitle") or "").split()).strip()
    # Only borrow the slug when it adds something the bare title lacks.
    if slug_title and not slug_title.lower().startswith(title.lower()):
        return slug_title if not title else title
    return title or slug_title


def _posted_at(job: dict) -> str | None:
    """Their postedAt is epoch milliseconds; we keep the day only."""
    raw = job.get("postedAt")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _location(job: dict) -> str:
    """Their location plus workplace type, so Remote/Hybrid survives filtering.

    `filters.region_ok` reads "Remote" out of the location string, and InternDoor
    keeps workplace type in its own field. Without this a fully-remote listing
    reads as a plain city and lands in the India sections.
    """
    location = " ".join(str(job.get("location") or "").split()).strip()
    workplace = str(job.get("workplaceType") or "").strip()
    if workplace and workplace.lower() not in location.lower():
        location = f"{location}, {workplace}" if location else workplace
    return location or "India"


def _stipend(job: dict) -> str | None:
    """Stated pay only. An estimate is a different field and we do not invent one."""
    for key in ("stipend", "stipendText"):
        value = job.get(key)
        if value:
            return str(value).strip()
    return None


def _external_id(job: dict, title: str) -> str:
    """Stable per listing.

    Their `id` is the LinkedIn job id, which is the right identity. Fall back to
    the company's apply URL, then to company+title, so a listing that arrives
    without an id still gets a key that does not change between runs.
    """
    raw = job.get("id")
    if raw:
        return str(raw)
    apply_url = (job.get("applyUrl") or job.get("url") or "").strip()
    if apply_url:
        return apply_url.rstrip("/").rsplit("/", 1)[-1]
    return f"{_slug(job.get('company') or '')}-{re.sub(r'[^a-z0-9]+', '-', title.lower())}"


async def fetch(company: dict, net: Net) -> list[Job]:
    data = await net.get_json(URL)
    listings = data.get("jobs") or []

    jobs: list[Job] = []
    seen: set[str] = set()
    software = 0
    for listing in listings:
        if not isinstance(listing, dict) or not _is_software(listing):
            continue
        software += 1

        name = " ".join(str(listing.get("company") or "").split()).strip()
        title = _title(listing)
        if not name or not title:
            continue

        # Prefer the employer's own posting. The LinkedIn mirror is only a
        # fallback: an Apply link should never dead-end on a third party when
        # the company's real board is known.
        url = (listing.get("applyUrl") or listing.get("url") or "").strip()
        if not url:
            continue

        external = _external_id(listing, title)
        if external in seen:
            continue
        seen.add(external)

        skills = listing.get("keySkills") or listing.get("skills") or []
        if isinstance(skills, str):
            skills = [skills]

        jobs.append(
            Job(
                id=f"{SOURCE}:{external}",
                source=SOURCE,
                company=name,
                company_slug=_slug(name),
                title=title,
                location=_location(listing),
                url=url,
                posted_at=_posted_at(listing),
                salary=_stipend(listing),
                degree=listing.get("degreeText") or None,
                skills=[str(s) for s in skills][:12] or None,
                description=listing.get("summary") or None,
            )
        )

    # Raises rather than returning []. An empty return is a *successful* fetch,
    # and store.upsert would then close every InternDoor role we have published.
    # A feed outage must never read as "they all stopped hiring".
    if not jobs:
        raise RuntimeError(
            f"interndoor: no software listings in {len(listings)} postings "
            "(feed shape may have changed)"
        )
    return jobs