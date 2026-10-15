---
name: intern-engine-india
description: Architecture and maintenance notes for the India tech internship tracking engine. Use when adding an ATS connector, changing filters or region handling, editing the job store schema, or debugging why roles are missing or miscategorised.
---

# Intern Engine India

A GitHub-Actions engine that polls company ATS hiring feeds, keeps India-based
tech internships, and regenerates README.md, docs/index.html, docs/feed.xml and
docs/api/*.json on every run. Python 3.11, no database — all state is JSON under
`data/`.

## The shape of a run

`run.py update` calls `pipeline.run_update()`, which returns
`(stats, store, new_ids)` and then hands off to the renderers in `run.py`. The
publish layer is deliberately outside the pipeline so a renderer bug can never
lose a scrape.

Inside `run_update`:

1. load config, blocklist, registry, existing store
2. `_repair_legacy_dates` — clear non-ISO `posted_at` values (see below)
3. `health.partition` — split companies into active and quarantined by the
   circuit breaker
4. one `asyncio.run(_fetch_all(...))`:
   - `_fetch_one` per company, isolated so one dead endpoint cannot stop the run
   - then `_enrich_stage` on the same client session:
     `_keep_matching` → `_dedup` → `_apply_company_cap` → `enrich.enrich_jobs`
5. record health, upsert into the store, purge old closed records
6. write stats and append a history line

## Invariants worth knowing before editing anything

**`posted_at` is never refreshed for a role that already has one.** A real
published date must not shift between runs. The consequence is that a *bad*
`posted_at` is permanent, which is why `_repair_legacy_dates` exists: before the
Workday connector learned to resolve its free-text `postedOn` ("Posted 6 Days
Ago"), hundreds of records were stored with that literal string, and nothing
would ever have replaced it.

**A job closes only when its company's fetch succeeded.** `store.upsert` matches
on `f"{source}:{company_slug}"` against the set of successful keys. A connector
that returns `[]` on failure instead of raising will silently mark every role at
that company as closed.

**Region filtering is strict about foreign countries.** `filters.region_ok`
vetoes any role naming a specific foreign country, and accepts bare "Remote"
only from verified Indian sources. Loosening this lets every US-based Workday
tenant flood an India-scoped list.

**Connectors raise; they do not return empty.** That is what trips the circuit
breaker. See `health.py`.

## Adding an ATS

Write `src/intern_engine/connectors/<ats>.py` with exactly:

```python
async def fetch(company: dict, net: Net) -> list[Job]
```

Register it in `pipeline.CONNECTORS` under the `ats` key, and make it
discoverable in `discover.py` (`_PROBE_URLS` for slug-guessable platforms,
`_PATTERNS` for URL mining). `enrich._FETCHERS` is optional — add an entry only
if the platform has a per-job detail endpoint worth fetching text from.

`data/companies.json` is the registry. Before finishing, check that every `ats`
value present in it resolves to a connector:

```python
from intern_engine import pipeline
import json
reg = json.load(open("data/companies.json"))
missing = {c["ats"] for c in reg} - set(pipeline.CONNECTORS)
assert not missing, missing
```

## Workday

The largest block of the registry. Three things it gets right that a naive
implementation does not, all of which were bugs at some point:

- reads the data centre from `wd`, the key `companies.json` actually stores.
  Reading `dc` (which does not exist) made every tenant probe all five data
  centres in sequence.
- resolves relative `postedOn` text into a real ISO date.
- asks the board for `searchText: "intern"` instead of pulling the whole tenant.

## InternDoor

`interndoor.py` reads InternDoor's public JSON feed **once, as a company
list**, and writes the employer names to `data/interndoor_seeds.json`.
`discover.py` then resolves those names to real ATS boards and the engine polls
each company directly.

Job data is never mirrored from their feed. The roles we publish always come
from the employer's own ATS, and freshness depends on our poll schedule rather
than on a third party staying up.

The network refresh is opt-in (`config: include_interndoor`, default off);
the cached seeds are always used. A third party being down must never be able
to break `discover`.

## Testing

There is no test suite. The checks that matter are:

```bash
python -m py_compile src/intern_engine/*.py src/intern_engine/connectors/*.py run.py

# registry coverage
python - <<'PY'
import json
from intern_engine import pipeline
reg = json.load(open("data/companies.json"))
print({c["ats"] for c in reg} - set(pipeline.CONNECTORS) or "all covered")
PY

# README row count must equal the store's open count
python - <<'PY'
import json
from intern_engine import readme
store = json.load(open("data/jobs.json"))
n = sum(1 for r in store.values() if r.get("is_open"))
print("store", n, "readme", readme.generate(store)["open"])   # must match
PY
```

Markdown tables must be checked for uniform column counts — ATS locations
contain raw `|` characters ("United States | Remote") and an unescaped one
silently reflows the rest of the row into the wrong columns. `readme._cell`
does the escaping.

## Common failures

| symptom | cause |
|---|---|
| README shows far fewer roles than the dashboard | something is filtering by ATS board slug against a list of company names. Match on name, never slug. |
| Every role looks undated, newest-first sort is wrong | a connector is storing non-ISO `posted_at` |
| All of a company's roles suddenly closed | its connector returns `[]` instead of raising |
| README renders empty but the store is populated | `section_limits` capped every section |
| `FileNotFoundError` writing `docs/` | the renderer assumed `docs/` exists; `docs/` is a build artifact |