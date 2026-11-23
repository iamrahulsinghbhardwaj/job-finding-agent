"""Helpers shared by adapters."""

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from selectolax.lexbor import LexborHTMLParser

# Block-level boundaries become line breaks; inline tags (<b>, <a>, <span>) stay within a line.
_YEARS = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:or\s+more\s+)?years?\b", re.IGNORECASE)

# ISO 3166-1 alpha-2 -> alpha-3 for the countries we search (India + HLD Europe hubs).
ISO3 = {
    "IN": "IND",
    "GB": "GBR",
    "IE": "IRL",
    "NL": "NLD",
    "DE": "DEU",
    "CH": "CHE",
    "PL": "POL",
    "SE": "SWE",
    "DK": "DNK",
}


class ScopeError(ValueError):
    """A search was asked for without the scope the source requires (e.g. no country)."""


_BLOCK_BREAK = re.compile(
    r"<br\s*/?>|</(?:p|div|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|blockquote)\s*>",
    re.IGNORECASE,
)


def schema_hash(item: Mapping[str, Any]) -> str:
    """Short hash of the set of top-level keys: changes when a source changes its response shape."""
    keys = ",".join(sorted(item))
    return hashlib.sha256(keys.encode()).hexdigest()[:16]


def html_to_text(html: str) -> str:
    """Visible text of an HTML fragment, one block per line, no tags, scripts or styles."""
    tree = LexborHTMLParser(_BLOCK_BREAK.sub(lambda m: m.group(0) + "\n", html))
    for node in tree.css("script, style"):
        node.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    lines = (line.strip() for line in root.text(separator="").splitlines())
    return "\n".join(line for line in lines if line)


def extract_min_yoe(text: str) -> int | None:
    """First "N+ years" / "N or more years" / "N year" in `text`, or None.

    The first figure is used on purpose: postings often read "Bachelor's AND 4+ years ... OR
    Master's AND 3+ years", and the first path is the one that applies to most candidates.
    """
    match = _YEARS.search(text)
    return int(match.group(1)) if match else None


def to_iso3(country: str) -> str:
    try:
        return ISO3[country]
    except KeyError:
        raise ScopeError(f"country {country!r} is not supported yet (known: {', '.join(ISO3)})") from None
