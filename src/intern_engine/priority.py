"""Company prestige ranking, used to curate capped sections.

When a section has more roles than its cap, we keep roles from the most
recognizable / sought-after companies first. Lower rank = higher priority.
Edit this list to taste — it only affects which roles make the cut when a
section is over its limit, not what gets collected.
"""

from __future__ import annotations

import re

# Roughly ordered by how sought-after they are for SWE/ML interns.
TOP_COMPANIES = [
    # --- global product tech with large India engineering orgs ---
    "google", "microsoft", "amazon", "adobe", "nvidia", "qualcomm", "intel",
    "ibm", "oracle", "sap", "salesforce", "cisco", "vmware", "dell",
    "hewlett packard", "samsung", "atlassian", "red hat", "ebay", "booking.com",
    "spotify", "dropbox", "paypal", "visa", "mastercard", "linkedin", "meta",
    "apple", "siemens", "philips", "bosch", "honeywell",
    # --- indian product companies ---
    "flipkart", "swiggy", "zomato", "razorpay", "cred", "zerodha", "groww",
    "meesho", "nykaa", "paytm", "phonepe", "mphasis", "thoughtworks",
    "freshworks", "zoho", "postman", "hasura", "browserstack", "chargebee",
    "dream11", "games24x7", "junglee", "udaan", "ola", "ather", "zepto",
    "blinkit", "delhivery", "elastic run", "xeno", "quicko", "slice", "jupiter",
    "setu", "smallcase", "sprinklr", "vahan", "deloitte digital", "scaler",
    # --- global tech platforms / dev-tool companies ---
    "elastic", "mongodb", "confluent", "databricks", "snowflake", "stripe",
    "twilio", "hashicorp", "cloudflare", "datadog", "grafana labs", "openai",
    "anthropic", "sourcegraph", "new relic", "github", "gitlab",
    # --- banks and fintech ---
    "goldman sachs", "goldman", "morgan stanley", "jpmorgan", "j.p. morgan",
    "jp morgan", "citi", "citibank", "american express", "amex", "barclays",
    "hsbc", "standard chartered", "deutsche bank", "bnp paribas",
    "societe generale", "socgen", "natwest", "wells fargo", "bank of america",
    "bofa", "ubs", "credit suisse", "icici bank", "hdfc bank",
    "kotak mahindra", "axis bank", "yes bank", "indusind bank", "dbs bank",
    # --- semiconductors and hardware ---
    "micron", "applied materials", "cadence", "synopsys", "mediatek",
    "texas instruments", "analog devices", "marvell", "broadcom", "arm",
    "globalfoundries", "valeo", "harman", "continental", "denso", "zf",
    # --- large india employers / IT services ---
    "accenture", "cognizant", "infosys", "wipro", "tcs", "tata consultancy",
    "hcltech", "hcl tech", "tech mahindra", "ltimindtree", "capgemini",
    "hexaware", "persistent", "birlasoft", "brillio", "quest global", "ust",
    "globallogic", "ntt data", "atkinsrealis", "atkins", "jacobs", "kpmg",
    "pwc", "ernst young", "kearney", "genpact", "thermo fisher", "dxc",
    "cognizant technology solutions",
]

_RANK = {name: i for i, name in enumerate(TOP_COMPANIES)}
# Whole-word matching so "Unity" never claims "Community Bank" and "meta"
# never claims "Metagenomics".
_RANK_RES = [(re.compile(rf"\b{re.escape(name)}\b"), i) for name, i in _RANK.items()]
UNRANKED = 10_000


def rank(company: str) -> int:
    """Lower = more sought-after. Unknown companies sort last."""
    c = (company or "").strip().lower()
    if c in _RANK:
        return _RANK[c]
    for pattern, i in _RANK_RES:
        if pattern.search(c):  # e.g. "Stripe, Inc." contains the word "stripe"
            return i
    return UNRANKED
