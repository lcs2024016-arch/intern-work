"""The single normalized job record every connector produces.

Keeping one shape means the pipeline, store, and renderer never care which ATS a
role came from — adding a source touches only its connector.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Fields that exist only during a run and must never be written to the store.
TRANSIENT_FIELDS = ("description",)


@dataclass
class Job:
    id: str  # stable: "<source>:<company_slug>:<external_id>"
    source: str
    company: str
    company_slug: str
    title: str
    location: str
    url: str
    # "india" (default) or "global". Global roles come from boards that are
    # outside the India scope but are remote and worth showing; the renderer
    # buckets them into their own labelled section instead of the India ones.
    scope: str = "india"
    posted_at: str | None = None
    season: str = "Unspecified"
    season_inferred: bool = False
    category: str = "Other"
    salary: str | None = None  # pay the ATS itself states
    stipend: str | None = None  # pay stated in the posting body
    estimated_stipend: str | None = None  # looked up, not stated (see compensation.py)
    estimated_ctc: str | None = None
    pay_source: str | None = None  # where the estimate came from
    experience: str | None = None
    degree: str | None = None
    batch: str | None = None  # graduating class the role targets
    skills: list[str] | None = None
    description: str | None = None  # transient: raw posting text
