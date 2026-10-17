"""Renders README.md + data/internships.csv from the job store."""

from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import UTC, datetime, timedelta
from html import escape

from . import config, filters, paths, priority

# "Infosys Ltd." / "Infosys Limited" / "Infosys" should all match one line in
# companies.txt. Everything that is not a letter or digit is noise for that
# comparison, so it is stripped on both sides.
_NOISE_RE = re.compile(r"[^a-z0-9]+")

# Roles from boards outside the India scope (currently the ypatodkar VC
# directory). They are remote internships an India-scoped reader cannot
# necessarily take, so they get their own section and an explicit caveat rather
# than being mixed in with the India/Remote sections.
GLOBAL_SECTION = "Global remote (US-based companies)"


def _norm(name: str) -> str:
    return _NOISE_RE.sub("", (name or "").lower())


def _cell(value) -> str:
    """Escape one value for a Markdown table cell.

    A raw `|` ends the cell, and ATS location strings carry them freely
    ("United States | Remote"). Unescaped, one such role silently reflows the
    rest of its row into the wrong columns. Newlines break the row outright.
    """
    text = escape(str(value if value is not None else ""))
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _seeded_company_names() -> set[str]:
    """companies.txt, lowercased, plus its punctuation-free form.

    Used only when config `company_filter` is on. Matching is by NAME and by
    normalized name — never by ATS board slug. A slug like "1kosmos-in" does
    not appear in a hand-written list of company names, which is why an
    earlier version of this file matched on it and silently dropped 110 of
    134 open roles from the README while the dashboard showed all of them.
    """
    names: set[str] = set()
    try:
        with open(paths.COMPANIES_TXT_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip().lower()
                if not line or line.startswith("#"):
                    continue
                names.add(line)
                names.add(_norm(line))
    except OSError:
        pass
    return names


def _is_seeded(company: str, seeds: set[str]) -> bool:
    low = (company or "").lower()
    return low in seeds or _norm(company) in seeds


def _flag(job: dict) -> str:
    """The "new in the last 48h" marker."""
    first_seen = job.get("first_seen_at") or ""
    try:
        dt = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        if (datetime.now(UTC) - dt).total_seconds() < 48 * 3600:
            return " \U0001f195"
    except (ValueError, TypeError):
        pass
    return ""


def _pretty_date(job: dict) -> str:
    """Render a real posted date as 'Oct 14, 2026', or '—' when unknown."""
    posted = (job.get("posted_at") or "")[:10]
    if not posted:
        return "—"
    try:
        return datetime.strptime(posted, "%Y-%m-%d").strftime("%b %d, %Y")
    except ValueError:
        return posted


def _short_location(location: str, limit: int = 38) -> str:
    location = (location or "").strip() or "—"
    location = location.replace("\n", " ")
    if len(location) > limit:
        return location[: limit - 1].rstrip() + "…"
    return location


def _pay(job: dict) -> str:
    """Best available pay figure, marking which kind it is.

    Order matters: a figure the posting itself states (salary, stipend) beats
    one the engine inferred, and an inference is prefixed with ~ so nobody
    mistakes a benchmark for a promise.
    """
    for field, prefix in (
        ("salary", ""),
        ("stipend", ""),
        ("estimated_stipend", "~"),
        ("estimated_ctc", "~"),
    ):
        value = job.get(field)
        if value:
            return f"{prefix}{value}"
    return ""


def _specs(job: dict) -> str:
    """Pay, degree and experience as one compact cell."""
    parts = []
    if pay := _pay(job):
        parts.append(f"**{pay}**")
    if job.get("degree"):
        parts.append(str(job["degree"]))
    if job.get("experience"):
        parts.append(str(job["experience"]))
    return "<br>".join(_cell(p) for p in parts)


def _skill_tags(job: dict) -> str:
    """Up to four extracted skills as inline code tags."""
    raw = job.get("skills") or []
    if isinstance(raw, str):
        raw = [raw]
    tags = [_cell(s) for s in list(raw)[:4]]
    return " ".join(f"`{t}`" for t in tags)


def _sort_key(record: dict):
    """Newest real posted date first; undated rows sink below dated ones."""
    return (
        (record.get("posted_at") or "")[:10] or "0000-00-00",
        (record.get("first_seen_at") or ""),
    )


def _select(records: list[dict], cfg: dict, label: str) -> list[dict]:
    """Cap what a section shows, newest posted date first.

    Both caps live here rather than in the pipeline on purpose. The store is a
    record of what is actually open; trimming it at collection time would make
    upsert close the roles that were trimmed away, reporting live openings as
    taken down.
    """
    # Sections are labelled "<cycle> (India)" / "<cycle> (Remote)", but
    # section_limits in config.json is keyed by the bare cycle. Try the full
    # label first so an exact per-section override still wins, then the cycle,
    # then a global "default".
    cycle = label.split(" (")[0]
    per_company = config.max_per_company(cfg)

    if per_company > 0:
        # Newest first inside each company, then keep the cap, so the roles
        # that survive for a big tenant are its freshest.
        ranked = sorted(records, key=_sort_key, reverse=True)
        seen: dict[str, int] = {}
        kept: list[dict] = []
        for job in ranked:
            key = (job.get("company") or "").lower().strip()
            if seen.get(key, 0) >= per_company:
                continue
            seen[key] = seen.get(key, 0) + 1
            kept.append(job)
        records = kept

    limit = (
        config.section_limit(cfg, label)
        or config.section_limit(cfg, cycle)
        or config.section_limit(cfg, "default")
    )
    if not limit:
        return records

    # Strictly newest posted date first, undated rows below dated ones.
    # This used to re-sort by company prestige on top of recency, which meant
    # a two-week-old Google role could sit above today's — a live list read
    # that way is not sorted by date at all. The curated order is still
    # available via `list_sort: "priority"` for anyone who wants it.
    scored = sorted(records, key=_sort_key, reverse=True)
    if config.list_sort(cfg) == "priority":
        # Stable sort, so recency still orders the rows inside each band.
        scored.sort(key=lambda r: priority.rank(r.get("company", "")))
    return scored[:limit]


def _group(records: list[dict], cycles: list[str]) -> dict[str, list[dict]]:
    """Bucket roles by cycle and whether they are remote.

    `scope == "global"` roles are collected into one trailing section instead of
    being interleaved by cycle. They come from boards outside the India scope,
    so sorting them into "Summer 2027 (Remote)" alongside roles an Indian
    applicant can actually take would overstate what is open to the reader.
    """
    sections: dict[str, list[dict]] = {}
    for cycle in cycles:
        sections[f"{cycle} (India)"] = []
        sections[f"{cycle} (Remote)"] = []
    sections[GLOBAL_SECTION] = []

    for job in records:
        if job.get("scope") == "global":
            sections[GLOBAL_SECTION].append(job)
            continue
        season = job.get("season") or "Term Unconfirmed"
        label = (
            f"{season} (Remote)"
            if filters.is_remote_or_hybrid(job.get("location", ""))
            else f"{season} (India)"
        )
        sections.setdefault(label, []).append(job)

    for label in sections:
        sections[label].sort(key=_sort_key, reverse=True)
    return sections


def generate(store_data: dict, stats: dict | None = None) -> dict:
    """Render README.md and data/internships.csv.

    `stats` is the last run's metrics. Optional so this stays callable on its
    own (offline regeneration, tests); when absent the engine-summary line is
    omitted rather than rendered with invented numbers.
    """
    cfg = config.load_config()
    cycle_list = config.cycles(cfg)
    repo = config.repo_slug()
    owner, _, name = repo.partition("/")
    if stats is None:
        try:
            with open(paths.STATS_PATH, encoding="utf-8") as f:
                stats = json.load(f)
        except (OSError, json.JSONDecodeError):
            stats = {}

    open_jobs = [r for r in store_data.values() if r.get("is_open")]
    open_jobs.sort(key=_sort_key, reverse=True)

    seeds: set[str] = set()
    if config.company_filter_enabled(cfg):
        seeds = _seeded_company_names()
        before = len(open_jobs)
        open_jobs = [j for j in open_jobs if _is_seeded(j.get("company", ""), seeds)]
        print(
            f"company_filter: {len(open_jobs)}/{before} roles match companies.txt"
        )

    sections = _group(open_jobs, cycle_list)
    # The global bucket is created up-front (so it exists even when empty, and
    # can be capped like any other section) but must render LAST: a US-based
    # role appearing before the India sections reads as if it outranked them.
    sections[GLOBAL_SECTION] = sections.pop(GLOBAL_SECTION)

    lines = [
        '<div align="center">',
        "  <h1>\U0001f1ee\U0001f1f3 India Tech Internships</h1>",
        "  <p><strong>A self-updating engine tracking tech internships in India "
        "so you don't have to.</strong></p>",
        "  <p>",
        f'    <a href="https://{owner.lower()}.github.io/{name}/">',
        '      <img src="https://img.shields.io/badge/Live_Dashboard-000000?style=for-the-badge&logo=github&logoColor=white" alt="Live Dashboard" />',
        "    </a>",
        f'    <a href="https://{owner.lower()}.github.io/{name}/api/jobs.json">',
        '      <img src="https://img.shields.io/badge/JSON_API-007ACC?style=for-the-badge&logo=json&logoColor=white" alt="JSON API" />',
        "    </a>",
        f'    <a href="https://{owner.lower()}.github.io/{name}/feed.xml">',
        '      <img src="https://img.shields.io/badge/RSS-subscribe-e67e22?style=for-the-badge" alt="RSS" />',
        "    </a>",
        "  </p>",
        "  <p>",
        f'    <img src="https://img.shields.io/badge/Open%20Roles-{len(open_jobs)}-6366f1?style=for-the-badge" alt="Open Roles" />',
        f'    <img src="https://img.shields.io/badge/Companies-{_company_count()}-22c55e?style=for-the-badge" alt="Companies" />',
        "    <img src=\"https://img.shields.io/badge/Updates-Hourly-3fb950?style=for-the-badge\" alt=\"Updates\" />",
        "  </p>",
        f"  <p><em>Last updated: {datetime.now(UTC).strftime('%b %d, %Y at %H:%M UTC')}</em></p>",
        "</div>",
        "",
        "---",
        "",
    ]

    inferred_count = 0
    for label, jobs in sections.items():
        if not jobs:
            continue
        jobs = _select(jobs, cfg, label)
        if not jobs:
            continue
        lines.append(f"## {_cell(label)} <kbd>{len(jobs)} open</kbd>")
        lines.append("")
        if label == GLOBAL_SECTION:
            # Said in the page, not left for the reader to infer from a location
            # column: on that board "remote" means remote within the employer's
            # own country, so eligibility is not implied by appearing here.
            lines.append(
                "> Remote internships at US venture-backed companies. **Remote "
                "means remote within the employer's country** — nearly all of "
                "these are US-only and need US work authorisation. Listed "
                "separately so they are never mistaken for India-open roles."
            )
            lines.append("")
        lines.append(
            "| \U0001f3e2 Company | \U0001f4bc Role | \U0001f3f7\ufe0f Category "
            "| \U0001f4b0 Pay & Specs | \U0001f4cd Location | \U0001f4c5 Posted | \U0001f517 Apply |"
        )
        lines.append("|---|---|---|---|---|---|:---:|")
        for job in jobs:
            url = job.get("url") or ""
            apply_link = f"[Apply ↗]({url})" if url else "—"
            tilde = " <sup>~</sup>" if job.get("season_inferred") else ""
            flag = _flag(job)
            if job.get("season_inferred"):
                inferred_count += 1
            specs = _specs(job) or "—"
            skills = _skill_tags(job)
            lines.append(
                f"| **{_cell(job.get('company', ''))}** "
                f"| {_cell(job.get('title', ''))}{tilde}{flag} "
                f"| `{_cell(job.get('category', ''))}` "
                f"| {specs} | {_cell(_short_location(job.get('location', '')))} "
                f"| {_pretty_date(job)} | {apply_link} |"
            )
            # Skills ride under the role rather than in their own column: a
            # separate one pushes the apply link off the right edge, and the
            # role title is what the eye tracks down the column anyway.
            if skills:
                lines.append(f"| | <sub>{skills}</sub> ||||||")
        lines.append("")

    if inferred_count:
        lines.append(
            "_~ = the title doesn't state a year; bucketed here from its posting "
            f"date ({inferred_count} of {len(open_jobs)})._"
        )
        lines.append("")

    # Recently closed.
    #
    # Restricted to roles that were ever in scope. The store also holds roles
    # that failed the region filter on their way in — Coinbase in San
    # Francisco, ACLU Kentucky — and listing those as "taken down" would claim
    # the engine once tracked them, which it did not. Someone chasing an
    # application should only see closures for roles that were real.
    window = (datetime.now(UTC) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    closed = [
        r
        for r in store_data.values()
        if not r.get("is_open")
        and (r.get("closed_at") or "") >= window
        and filters.region_ok(
            r.get("location", ""),
            False,
            False,
            True,
            True,
            source=r.get("source"),
        )
    ]
    closed.sort(key=lambda r: r.get("closed_at", ""), reverse=True)
    if closed:
        lines.append("<details>")
        lines.append(
            f"<summary><strong>Recently closed</strong> — "
            f"{len(closed)} roles taken down in the last 14 days</summary>"
        )
        lines.append("")
        lines.append("| Company | Role | Cycle | Closed |")
        lines.append("|---|---|---|---|")
        for r in closed[:25]:
            lines.append(
                f"| {_cell(r.get('company', ''))} | {_cell(r.get('title', ''))} "
                f"| {_cell(r.get('season', ''))} "
                f"| {(r.get('closed_at') or '')[:10]} |"
            )
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "## Hiring timeline",
            "",
            "Internships posted per week, from each role's real published date. "
            "Redrawn automatically on every run — when this line takes off, "
            "recruiting season is open:",
            "",
            "<picture>",
            '  <source media="(prefers-color-scheme: dark)" srcset="docs/trends-dark.svg">',
            '  <img alt="Internships posted per week, from real published dates" '
            'src="docs/trends-light.svg">',
            "</picture>",
            "",
            "## How this stays current",
            "",
            _engine_line(stats, open_jobs) if stats else "",
            "",
            "## How it works",
            "",
            "A Python engine reads public company hiring feeds directly, keeps the "
            "internships that match the scope, de-duplicates across sources, records "
            "each role's published date once so it never shifts, extracts the stack "
            "and any stated pay from the posting's own text, and regenerates this "
            "page through GitHub Actions. Every list below is sorted newest posting "
            "first, and every Apply link is re-checked about once a day: a posting "
            "that comes back 404 twice running is removed rather than left for you "
            "to click. The full source is in this repo.",
            "",
            "Companies are found two ways: dataset mining for ATS tokens, and "
            "InternDoer's public feed used purely as a list of which employers are "
            "hiring in India. Either way the roles come from each employer's own "
            "careers system.",
            "",
            "## Scope",
            "",
            "- **Roles:** Software Engineering, Data Science & Machine Learning "
            "(and closely related technical internships)",
            "- **Region:** India & remote",
            f"- **Cycles:** {', '.join(cycle_list)}",
            "",
            "## Platforms polled",
            "",
            _platform_line(),
            "",
            "## Contributing",
            "",
            "Add a company to `companies.txt` and run `python run.py discover`. "
            "See [CONTRIBUTING.md](CONTRIBUTING.md).",
            "",
            "## Legend",
            "",
            "- `~` on the cycle — the title states no year; bucketed from the "
            "posting date",
            "- \U0001f195 — first seen in the last 48 hours",
            "- **Bold** pay — quoted by the company",
            "- ~Pay~ with a tilde — inferred, not stated by the company",
            "",
            "_Roles can close at any time — always confirm on the company's page "
            "before applying._",
            "",
        ]
    )

    os.makedirs(os.path.dirname(paths.README_PATH), exist_ok=True)
    with open(paths.README_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    _write_csv(open_jobs)

    return {"open": len(open_jobs), "companies": _company_count()}


def _engine_line(stats: dict, open_jobs: list[dict]) -> str:
    """One honest sentence about what the last run actually did."""
    companies = stats.get("companies_total") or 0
    platforms = len(stats.get("companies_by_source") or {})
    ok_rate = stats.get("fetch_success_rate")
    coverage = stats.get("posted_date_coverage")
    latency = (stats.get("detection_latency") or {}).get("median_minutes")

    bits = [f"**{companies:,} companies** across **{platforms} ATS platforms**"]
    if isinstance(ok_rate, (int, float)):
        bits.append(f"{int(ok_rate * 100)}% fetch success")
    if isinstance(coverage, (int, float)):
        bits.append(f"real posted dates on {int(coverage * 100)}% of open roles")
    if isinstance(latency, (int, float)):
        bits.append(f"median detection lag {latency / 60:.1f}h")
    return (
        "_Engine (last run): "
        + " · ".join(bits)
        + ". One async pass an hour, with retry/backoff, per-host rate "
        "limits and a circuit breaker that quarantines dead endpoints._"
    )


def _platform_line() -> str:
    """Group the registry by platform family, biggest first."""
    try:
        with open(paths.COMPANIES_PATH, encoding="utf-8") as f:
            registry = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "_(registry unavailable)_"

    families = {
        "Applicant tracking systems": (
            "workday", "greenhouse", "lever", "ashby", "smartrecruiters",
            "workable", "oracle", "rippling", "breezy", "recruitee", "eightfold",
            "icims", "successfactors",
        ),
        "Custom careers scrapers": ("google", "bloomberg", "bytedance", "uber", "custom"),
        "Indian job portals": ("internshala", "naukri", "instahyre", "unstop"),
        "Aggregators": ("linkedin", "indeed", "wellfound", "amazon", "yc_waas"),
    }
    counts: dict[str, int] = {}
    for company in registry:
        ats = company.get("ats", "?")
        for label, members in families.items():
            if ats in members:
                counts[label] = counts.get(label, 0) + 1
                break
        else:
            counts["Other"] = counts.get("Other", 0) + 1

    return "\n".join(
        f"- **{label}** — {n:,} companies" for label, n in sorted(
            counts.items(), key=lambda kv: -kv[1]
        )
    )


def _company_count() -> int:
    try:
        with open(paths.COMPANIES_PATH, encoding="utf-8") as f:
            return len(json.load(f))
    except (OSError, json.JSONDecodeError):
        return 0


_CSV_FIELDS = [
    "company",
    "title",
    "season",
    "season_inferred",
    "category",
    "location",
    "salary",
    "stipend",
    "estimated_stipend",
    "estimated_ctc",
    "pay_source",
    "degree",
    "skills",
    "posted_at",
    "first_seen_at",
    "url",
]


def _write_csv(jobs: list[dict]) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Company",
            "Title",
            "Cycle",
            "Cycle Inferred",
            "Category",
            "Location",
            "Salary",
            "Stipend",
            "Est. Stipend",
            "Est. CTC",
            "Pay Source",
            "Degree",
            "Skills",
            "Posted",
            "First Seen",
            "URL",
        ]
    )
    for job in jobs:
        row = []
        for field in _CSV_FIELDS:
            value = job.get(field)
            if field == "skills" and isinstance(value, list):
                value = "; ".join(value)
            if field == "posted_at":
                value = (value or "")[:10]
            row.append("" if value is None else value)
        writer.writerow(row)
    os.makedirs(os.path.dirname(paths.CSV_PATH), exist_ok=True)
    with open(paths.CSV_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())