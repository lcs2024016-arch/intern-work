# Intern Engine India

A self-updating engine that tracks tech internships in India. It reads public
company hiring feeds directly, keeps the roles that match the scope, and
regenerates the README, dashboard, RSS feed and JSON API on every run.

The public-facing page is `README.md` — it is generated, so edit
`src/intern_engine/readme.py` rather than the file itself.

## Layout

```
run.py                     CLI entrypoint
src/intern_engine/
  paths.py                 every file path, computed from the repo root
  models.py                the one Job dataclass every connector produces
  net.py                   shared async HTTP: retries, backoff, per-host limits
  filters.py               is it an internship / tech / which cycle / which region
  skills.py                pulls stack, pay, stipend, degree out of posting text
  compensation.py          pay estimates for roles that state none
  priority.py              company prestige ranking, used to cap sections
  quality.py               blocklist + allowlist gate
  slug_variants.py         company name -> candidate ATS slugs
  discover.py              finds new companies and resolves their ATS boards
  interndoor.py            InternDoor's feed as a company-discovery source
                           (see also connectors/interndoor.py, which reads the
                           same feed as a source of roles)
  harvester.py             probes candidate slugs to validate a registry entry
  pipeline.py              the run: fetch -> classify -> enrich -> store
  enrich.py                per-role detail fetch that fills in skills and pay
  health.py                circuit breaker that quarantines dead endpoints
  store.py                 the job store: first-seen, open/closed, retention
  trends.py                postings-per-week chart
  readme.py                renders README.md + data/internships.csv
  dashboard.py             renders docs/index.html
  publish.py               renders docs/feed.xml + docs/api/*.json
  connectors/              one module per ATS, each exposing fetch(company, net)
```

## Adding a company

1. Append the name to `companies.txt` (one per line).
2. Run `python run.py discover`.

Discovery resolves the name to a real ATS board by generating slug candidates
(`slug_variants.generate_candidates`) and probing them. For Workday and Oracle
tenants, which cannot be derived from a company name, it instead mines the
apply URLs out of the public internship datasets — which is how most large
Indian employers get picked up.

Confirm the outcome in `tracked_companies.txt` / `untracked_companies.txt`.

## Adding an ATS

1. Create `src/intern_engine/connectors/<ats>.py` exposing exactly
   `async def fetch(company: dict, net: Net) -> list[Job]`.
2. Register it in `pipeline.CONNECTORS`, keyed on the `ats` value.
3. Make it discoverable: add a probe template in `discover._PROBE_URLS` and a
   URL pattern in `discover._PATTERNS`. Without one of those, the platform can
   only be onboarded by hand-editing `data/companies.json`.

A connector should raise when the board is entirely unavailable — that is what
trips the circuit breaker in `health.py`, and returning an empty list instead
would mark every one of the company's roles closed.

`ypatodkar` is a deliberate exception to the India scope: it is a US-only VC
directory, so its roles would be vetoed by the region filter. It keeps only
remote internships, tags them `scope="global"`, and `readme._group` renders them
into their own trailing section (`readme.GLOBAL_SECTION`) with a caveat. A new
connector that is also out-of-scope follows the same path — set `scope` rather
than loosening `filters.region_ok`, which would let every US Workday tenant
flood the India list.

## Config

`data/config.json`, overlaid onto `config.DEFAULTS`.

| key | default | meaning |
|---|---|---|
| `cycles` | `["Summer 2027", "Fall 2026"]` | which recruiting cycles to keep |
| `regions` | `["India", "Remote"]` | `global`/`any` disables region filtering |
| `role_scope` | `"tech"` | `tech` keeps only technical roles |
| `max_age_days` | `365` | drop roles older than this |
| `max_per_company` | `0` | cap roles per company per run (0 = no cap) |
| `section_limits` | `{}` | cap roles per README section, keyed by cycle |
| `infer_undated` | `true` | bucket roles whose title states no year |
| `infer_max_age_days` | `45` | how recent an undated role must be to infer |
| `enrich` | `true` | fetch full posting text to extract skills and pay |
| `pay_lookup` | `false` | look up pay for roles that state none (costs an API call) |
| `max_enrich_per_run` | `300` | ceiling on detail fetches per run |
| `allowlist_only` | `false` | keep only companies in the priority list |
| `include_international` | `false` | keep roles outside the configured regions |
| `company_filter` | `false` | restrict the README to `companies.txt` |
| `include_interndoor` | `true` | refresh InternDoor seeds on each discover. Left on so the seed list tracks new employers daily; the cached `data/interndoor_seeds.json` is still used whenever the feed is unreachable, so a third-party outage cannot break discovery. Unrelated to `connectors/interndoor.py`, which publishes InternDoor roles on every update run. |

## Environment

All optional — the engine runs without any of them.

| var | effect |
|---|---|
| `WORKDAY_PROXY` | routes Workday/Oracle fetches through a proxy. Datacentre IPs are blocked far more aggressively than residential ones, so this materially raises the hit rate on the largest block of the registry. |
| `GEMINI_API_KEY` | enables grounded pay lookup in `compensation.py` |
| `DISCORD_WEBHOOK_URL` | not wired up in this fork |
| `YC_WAAS_SESSION_KEY` | enables the YC Work at a Startup connector |

## Commands

```
python run.py discover   # find new companies -> data/companies.json
python run.py harvest    # probe candidate slugs -> data/companies.json
python run.py update     # fetch, filter, enrich, store, publish everything
python run.py all        # all three
```

`verify_all.py` audits the registry and reports which boards are alive, empty
or dead. It writes to `logs/` and is not part of CI.

## Data files

| file | what it is |
|---|---|
| `data/companies.json` | the registry: every company and the ATS board it resolves to |
| `data/jobs.json` | the job store, the source of truth for all output |
| `data/health.json` | per-endpoint failure counts driving the circuit breaker |
| `data/history.jsonl` | one line per run, for the dashboard sparkline |
| `data/stats.json` | the latest run's metrics |
| `data/interndoor_seeds.json` | cached InternDoor company seeds |
| `data/compensation_cache.json` | pay lookups, cached |
| `companies.txt` | hand-curated seed company names |
| `tracked_companies.txt` | generated coverage report, do not edit |
| `untracked_companies.txt` | generated coverage report, do not edit |

Roles can close at any time; always confirm on the company's own page.