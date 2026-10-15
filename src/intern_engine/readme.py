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


def _specs(job: dict) -> list[str]:
    """Pay, degree and experience, in that order, as short display strings."""
    out = []
    pay = (
        job.get("salary")
        or job.get("stipend")
        or job.get("estimated_stipend")
        or job.get("estimated_ctc")
    )
    if pay:
        out.append(f"~{pay}")
    if job.get("degree"):
        out.append(str(job["degree"]))
    if job.get("experience"):
        out.append(str(job["experience"]))
    return out


def _sort_key(record: dict):
    """Newest real posted date first; undated rows sink below dated ones."""
    return (
        (record.get("posted_at") or "")[:10] or "0000-00-00",
        (record.get("first_seen_at") or ""),
    )


def _select(
    records: list[dict], cfg: dict, label: str
) -> list[dict]:
    """Apply the per-section cap, keeping the most sought-after companies first.

    The cap is what stops one huge tenant filling the whole page. Within the
    cap, roles at higher-priority companies win ties.
    """
    # Sections are labelled "<cycle> (India)" / "<cycle> (Remote)", but
    # section_limits in config.json is keyed by the bare cycle. Try the full
    # label first so an exact per-section override still wins, then the cycle,
    # then a global "default".
    cycle = label.split(" (")[0]
    limit = (
        config.section_limit(cfg, label)
        or config.section_limit(cfg, cycle)
        or config.section_limit(cfg, "default")
    )
    if not limit:
        return records

    # Recency first, then let priority break ties between equally-fresh rows.
    # Two stable sorts rather than one compound key, so the intent stays
    # readable: sort by recency, then re-sort by rank while preserving recency
    # order inside each rank band.
    scored = sorted(records, key=_sort_key, reverse=True)
    scored.sort(key=lambda r: priority.rank(r.get("company", "")))
    return scored[:limit]


def _group(records: list[dict], cycles: list[str]) -> dict[str, list[dict]]:
    """Bucket roles by cycle and whether they are remote."""
    sections: dict[str, list[dict]] = {}
    for cycle in cycles:
        sections[f"{cycle} (India)"] = []
        sections[f"{cycle} (Remote)"] = []

    for job in records:
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


def generate(store_data: dict) -> dict:
    cfg = config.load_config()
    cycle_list = config.cycles(cfg)
    repo = config.repo_slug()
    owner, _, name = repo.partition("/")

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
        "    <img src=\"https://img.shields.io/badge/Updates-Every%202h-3fb950?style=for-the-badge\" alt=\"Updates\" />",
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
        lines.append(
            "| \U0001f3e2 Company | \U0001f4bc Role | \U0001f3f7\ufe0f Category "
            "| \U0001f4cd Location | \U0001f4c5 Posted | \U0001f517 Apply |"
        )
        lines.append("|---|---|---|---|---|:---:|")
        for job in jobs:
            url = job.get("url") or ""
            apply_link = f"[Apply ↗]({url})" if url else "—"
            tilde = " <sup>~</sup>" if job.get("season_inferred") else ""
            flag = _flag(job)
            if job.get("season_inferred"):
                inferred_count += 1
            lines.append(
                f"| **{_cell(job.get('company', ''))}** "
                f"| {_cell(job.get('title', ''))}{tilde}{flag} "
                f"| `{_cell(job.get('category', ''))}` "
                f"| {_cell(_short_location(job.get('location', '')))} "
                f"| {_pretty_date(job)} | {apply_link} |"
            )
        lines.append("")

    if inferred_count:
        lines.append(
            "_~ = the title doesn't state a year; bucketed here from its posting "
            f"date ({inferred_count} of {len(open_jobs)})._"
        )
        lines.append("")

    # Recently closed
    window = (datetime.now(UTC) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    closed = [
        r
        for r in store_data.values()
        if not r.get("is_open") and (r.get("closed_at") or "") >= window
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
            "## How it works",
            "",
            "A Python engine reads public company hiring feeds directly, keeps the "
            "internships that match the scope (India-based tech roles), "
            "de-duplicates across sources, extracts the stack and any stated pay "
            "from each posting's text, and regenerates this page through GitHub "
            "Actions. Every link points at the company's own posting. The full "
            "source is in this repo.",
            "",
            "## Scope",
            "",
            "- **Roles:** Software Engineering, Data Science & Machine Learning "
            "(and closely related technical internships)",
            "- **Region:** India & verified remote",
            f"- **Cycles:** {', '.join(cycle_list)}",
            "",
            "## Contributing",
            "",
            "Add a company to `companies.txt` and run `python run.py discover`.",
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