"""Helpers shared by adapters."""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from selectolax.lexbor import LexborHTMLParser

# Block-level boundaries become line breaks; inline tags (<b>, <a>, <span>) stay within a line.

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


MAX_YOE = 50

# --- years of experience ------------------------------------------------------------------------
#
# Every figure ("3+ years", "2-12+ yrs", "5 or more years") is classified by the words after it,
# up to the end of its clause:
#   general  "... of professional / industry / software development / engineering / relevant / work
#            experience", or plain "years of experience" not tied to a skill;
#   skill    "years with / in / using <tech>", "years of <tech> experience", "years Java";
#   anything else is ambiguous and ignored ("founded 10 years ago", "10+ years leading teams").
# A bare figure ending its line ("- 5+ years") counts as general only under a required heading.

_FIGURE = re.compile(
    r"\b(\d{1,2})(?:\s*(?:-|\u2013|\u2014|to)\s*(\d{1,2}))?\s*\+?\s*(?:or\s+more\s+)?(?:years?|yrs?)\b"
    r"(?:['\u2019](?!\w))?",  # possessive: "10+ years' experience"
    re.IGNORECASE,
)
# How far after the figure its clause is read: up to 60 characters before "experience", plus the
# word itself. Amazon's standard "3+ years of non-internship professional software development
# experience" has 53 characters between.
_GAP = 75
_CLAUSE_END = re.compile(r"[,;()]|\.(?:\s|$)|\s(?:and|or)\s+(?:at\s+least\s+)?\d|\d")
_GENERAL = re.compile(
    r"^\s*(?:of\s+)?(?:[\w/-]+\s+){0,6}?"
    r"(?:professional|industry|software\s+development|software\s+engineering|engineering|relevant|work)"
    r"\s+experience\b",
    re.IGNORECASE,
)
_GENERAL_PLAIN = re.compile(
    r"^\s*(?:of\s+)?(?:(?:hands-on|overall|total|full-time)\s+)?experience\b"
    r"(?!\s+(?:with|in|using|working\s+with|on)\b)",
    re.IGNORECASE,
)
_SKILL = re.compile(
    r"^\s*(?:of\s+)?(?:(?:hands-on\s+)?experience\s+|\w+\s+){0,2}(?:with|in|using|working\s+with|on)\s+\S"
    r"|^\s*(?:of\s+)?(?:[\w+#./-]+\s+){1,4}?experience\b",
    re.IGNORECASE,
)
_TECH_WORD = re.compile(r"^\s*(?:of\s+)?(?:[A-Z][\w+#.]*|\w*[+#]\w*)(?:\s|$)")  # "Java", "C++", "C#"
# Right after a figure: "5+ years of experience preferred" / "... is a plus" is not a minimum.
_PREFERRED_AFTER = re.compile(r"[^\n.;]{0,40}?\b(?:preferred|nice to have|a plus|bonus)\b", re.IGNORECASE)
# Right before a figure, it is not a minimum: the company's age ("for over 20 years", "we have 30
# years") or a part of a bigger figure ("6+ years of experience, including 2+ years of web").
_NOT_A_MINIMUM_BEFORE = re.compile(
    r"(?:\bfor(?:\s+(?:over|more\s+than|nearly|almost|the\s+(?:past|last)))?|\bwe\s+have|\bwe've|\bour"
    r"|\bincluding(?:\s+at\s+least)?|\bof\s+which(?:\s+at\s+least)?|\bover)\s+$",
    re.IGNORECASE,
)
_REQUIRED_HEADING = re.compile(
    r"\b(?:minimum|basic|required)\s+(?:qualifications|requirements)\b|^requirements\b|\bwhat you need\b",
    re.IGNORECASE,
)
_PREFERRED_HEADING = re.compile(r"\b(?:preferred|desired|bonus|nice[- ]to[- ]have)\b", re.IGNORECASE)
_HEADING_MAX = 60  # a longer line is a sentence, not a section heading


@dataclass(frozen=True)
class YearsRequired:
    """Years of experience a posting asks for. `max` is set only for an explicit range ("2-12+")."""

    min: int | None = None
    max: int | None = None


@dataclass
class _Mentions:
    general: list[YearsRequired] = field(default_factory=list)
    skill: list[YearsRequired] = field(default_factory=list)


def extract_yoe(text: str) -> YearsRequired:
    """The years a posting REQUIRES, or YearsRequired() (unknown) when it is not clear.

    Mentions under a required heading ("Minimum qualifications", "Basic qualifications", "Minimum
    requirements", "Requirements") are used if there are any; else mentions anywhere outside a
    preferred section. Preferred sections and "... preferred" / "a plus" figures never count.
    Then: general mentions -> the smallest (several paths: "Bachelor's and 4+ years, or Master's
    and 3+ years" needs 3); only skill mentions -> the LARGEST ("5+ years Java, 2+ years
    Kubernetes" needs 5); nothing classified -> unknown.
    """
    required, other = _Mentions(), _Mentions()
    section: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if heading := _heading(line):
            section = heading
            continue
        if section == "preferred":
            continue
        for match in _FIGURE.finditer(line):
            _classify(line, match, required if section == "required" else other, section == "required")
    chosen = required if required.general or required.skill else other
    if chosen.general:
        return min(chosen.general, key=lambda y: y.min or 0)
    if chosen.skill:
        return max(chosen.skill, key=lambda y: y.min or 0)
    return YearsRequired()


def extract_min_yoe(text: str) -> int | None:
    return extract_yoe(text).min


def years_fields(years: YearsRequired) -> dict[str, int | None]:
    """RawJob / JobDetail keyword arguments."""
    return {"min_yoe": years.min, "max_yoe": years.max}


def _classify(line: str, match: re.Match[str], into: _Mentions, in_required: bool) -> None:
    low = int(match.group(1))
    high = int(match.group(2)) if match.group(2) else None
    if low > MAX_YOE or _NOT_A_MINIMUM_BEFORE.search(line[max(0, match.start() - 30) : match.start()]):
        return
    if _PREFERRED_AFTER.match(line, match.end()):
        return
    if high is not None and not low < high <= MAX_YOE:
        high = None  # "4-2 years" or "2-99 years": keep the minimum only
    years = YearsRequired(low, high)
    rest = line[match.end() : match.end() + _GAP]
    clause = rest[: end.start()] if (end := _CLAUSE_END.search(rest)) else rest
    if _GENERAL.match(clause) or _GENERAL_PLAIN.match(clause):
        into.general.append(years)
    elif _SKILL.match(clause) or _TECH_WORD.match(clause):
        into.skill.append(years)
    elif in_required and not clause.strip(" :-\u2013"):
        into.general.append(years)  # "Minimum requirements: - 5+ years"


def _heading(line: str) -> str | None:
    """A section heading such as "Preferred qualifications": short, with no figures in it."""
    if len(line) > _HEADING_MAX or any(ch.isdigit() for ch in line):
        return None
    if _PREFERRED_HEADING.search(line):
        return "preferred"
    if _REQUIRED_HEADING.search(line):
        return "required"
    return None


def to_iso3(country: str) -> str:
    try:
        return ISO3[country]
    except KeyError:
        raise ScopeError(f"country {country!r} is not supported yet (known: {', '.join(ISO3)})") from None
