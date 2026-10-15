"""Renders the Atom feed + static JSON API under docs/."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from html import escape

from . import config, paths


def write_feed(store_data: dict) -> int:
    """Write an Atom feed of open roles (newest-spotted first)."""
    open_jobs = [r for r in store_data.values() if r.get("is_open")]
    open_jobs.sort(key=lambda r: (r.get("first_seen_at", ""), _sort_key(r)), reverse=True)
    entries = open_jobs[:50]

    repo = config.repo_slug()
    base = config.pages_base()
    updated = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    xml_entries = []
    for r in entries:
        xml_entries.append(f"""  <entry>
    <title>{escape(r.get('company', ''))} — {escape(r.get('title', ''))}</title>
    <link href="{escape(r.get('url', ''))}" rel="alternate"/>
    <id>{escape(r.get('id', ''))}</id>
    <updated>{escape(r.get('first_seen_at', updated))}</updated>
    <summary>{escape(r.get('location', ''))} · {escape(r.get('category', ''))} · {escape(r.get('season', ''))}</summary>
  </entry>""")

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Intern Engine India — New Internships</title>
  <link href="{base}/feed.xml" rel="self"/>
  <link href="{base}/" rel="alternate"/>
  <id>{base}/</id>
  <updated>{updated}</updated>
  <author><name>Intern Engine India</name></author>
{"".join(xml_entries)}
</feed>
"""
    os.makedirs(paths.DOCS_DIR, exist_ok=True)
    with open(paths.FEED_PATH, "w", encoding="utf-8") as f:
        f.write(xml)
    return len(entries)


def _sort_key(r: dict):
    return ((r.get("posted_at") or "")[:10], (r.get("first_seen_at") or ""))


# The public API is a projection of the store, so adding a field here is the
# only thing needed to expose it. Anything a reader would reasonably filter or
# display belongs in this list; anything internal (closed_at, enriched_at,
# company_slug) does not.
_API_FIELDS = (
    "id",
    "company",
    "title",
    "season",
    "season_inferred",
    "category",
    "region",
    "location",
    "url",
    "posted_at",
    "first_seen_at",
    "salary",
    "stipend",
    "estimated_stipend",
    "estimated_ctc",
    "pay_source",
    "degree",
    "experience",
    "skills",
    "source",
)


def write_api(store_data: dict, stats: dict) -> None:
    """Write static JSON API files."""
    open_jobs = [r for r in store_data.values() if r.get("is_open")]
    open_jobs.sort(key=_sort_key, reverse=True)

    api_jobs = [{field: r.get(field) for field in _API_FIELDS} for r in open_jobs]

    os.makedirs(paths.API_DIR, exist_ok=True)
    with open(os.path.join(paths.API_DIR, "jobs.json"), "w", encoding="utf-8") as f:
        json.dump(api_jobs, f, indent=2, ensure_ascii=False)
    with open(os.path.join(paths.API_DIR, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    with open(os.path.join(paths.API_DIR, "companies.json"), "w", encoding="utf-8") as f:
        try:
            with open(paths.COMPANIES_PATH, encoding="utf-8") as cf:
                registry = json.load(cf)
        except (OSError, json.JSONDecodeError):
            registry = []
        json.dump(
            {
                "total": len(registry),
                "by_ats": _count_by(registry, "ats"),
                "companies": registry,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )


def _count_by(rows: list[dict], key: str) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.get(key, "?")] = counts.get(row.get(key, "?"), 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
