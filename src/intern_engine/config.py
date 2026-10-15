"""Tunable settings, loaded from data/config.json (with safe defaults).

Configured for India-based internship tracking.
"""

from __future__ import annotations

import json
import os
import re

from . import paths

DEFAULTS = {
    "cycles": ["Summer 2027", "Fall 2026"],
    "default_cycle": "Summer 2027",
    "regions": ["India", "Remote"],
    "role_scope": "tech",
}

_FALLBACK_REPO = "your-username/intern-engine-india"

_INDIA_TOKENS = {"india", "in", "bharat"}
_REMOTE_TOKENS = {"remote", "wfh", "work from home", "anywhere"}
_US_TOKENS = {"us", "usa", "united states", "u.s.", "america"}
_CANADA_TOKENS = {"canada"}
_GLOBAL_TOKENS = {"global", "international", "worldwide", "any", "all"}


def repo_slug() -> str:
    """'owner/name' for this repo: from Actions env, else the git remote."""
    env = os.environ.get("GITHUB_REPOSITORY")
    if env and "/" in env:
        return env
    try:
        with open(os.path.join(paths.ROOT, ".git", "config"), encoding="utf-8") as f:
            m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?\s", f.read())
            if m:
                return m.group(1)
    except OSError:
        pass
    return _FALLBACK_REPO


def pages_base() -> str:
    owner, _, name = repo_slug().partition("/")
    return f"https://{owner.lower()}.github.io/{name}"


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(paths.CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def cycles(cfg: dict) -> list[str]:
    return list(cfg.get("cycles") or DEFAULTS["cycles"])


def want_india(cfg: dict) -> bool:
    return any(str(r).lower() in _INDIA_TOKENS for r in (cfg.get("regions") or []))


def want_remote(cfg: dict) -> bool:
    return any(str(r).lower() in _REMOTE_TOKENS for r in (cfg.get("regions") or []))


def want_us(cfg: dict) -> bool:
    return any(str(r).lower() in _US_TOKENS for r in (cfg.get("regions") or []))


def want_canada(cfg: dict) -> bool:
    return any(str(r).lower() in _CANADA_TOKENS for r in (cfg.get("regions") or []))


def restrict_region(cfg: dict) -> bool:
    regions = cfg.get("regions") or []
    if not regions:
        return False
    return not any(str(r).lower() in _GLOBAL_TOKENS for r in regions)


def allowlist_only(cfg: dict) -> bool:
    """Only keep roles from companies in the curated priority list. Strict:
    much less coverage, much less noise. Off by default."""
    return bool(cfg.get("allowlist_only", False))


def include_international(cfg: dict) -> bool:
    """Keep roles outside the configured regions. They get labelled rather
    than dropped, which is how "verified remote" roles survive a strict
    India-only region setting."""
    return bool(cfg.get("include_international", False))


def max_age_days(cfg: dict) -> int:
    return int(cfg.get("max_age_days") or 365)


def max_per_company(cfg: dict) -> int:
    """Cap on roles kept per company per run. 0 means no cap."""
    return int(cfg.get("max_per_company") or 0)


def infer_undated(cfg: dict) -> bool:
    return bool(cfg.get("infer_undated", True))


def infer_max_age_days(cfg: dict) -> int:
    return int(cfg.get("infer_max_age_days", 45))


def section_limit(cfg: dict, label: str) -> int | None:
    limit = (cfg.get("section_limits") or {}).get(label)
    return int(limit) if limit else None


def enrich_enabled(cfg: dict) -> bool:
    """Fetch each kept posting's full text to pull out skills/pay/degree."""
    return bool(cfg.get("enrich", True))


def pay_lookup_enabled(cfg: dict) -> bool:
    """Look up pay for roles whose posting states none. Off by default: it
    needs an API key and a network call per unknown role."""
    return bool(cfg.get("pay_lookup", False))


def max_enrich_per_run(cfg: dict) -> int:
    return int(cfg.get("max_enrich_per_run", 300))


def company_filter_enabled(cfg: dict) -> bool:
    """Restrict the published list to companies named in companies.txt.

    Off by default. When on, matching is by company NAME (and a normalized
    form of it), never by ATS board slug — a slug like "1kosmos-in" does not
    appear in a list of human-typed company names, and matching on it silently
    drops almost every role.
    """
    return bool(cfg.get("company_filter", False))


def include_interndoor(cfg: dict) -> bool:
    """Also seed discovery from InternDoer's public feed.

    Off by default because it makes `discover` depend on a third party being
    up. When off, the cached data/interndoor_seeds.json is still used, so a
    one-off refresh is enough to keep feeding the seed list.
    """
    return bool(cfg.get("include_interndoor", False))
