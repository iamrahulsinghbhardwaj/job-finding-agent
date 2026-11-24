"""Map messy location strings ("Hyderabad, Telangana, IND", "Bangalore - Remote") to country/city/remote.

Rules plus lookup tables, no guessing:
- the string is split into tokens on , ; / | ( ) and spaced dashes;
- "Remote" (alone, or glued on as in "US-Remote") sets the remote flag and is removed;
- tokens are looked up as known cities (which imply their country) or country names / ISO-3 codes;
- "<city>, <US state code>" is always the US city of that name ("Dublin, CA", "Dublin, OH" -> US),
  unless the code is the city's own country ("Bengaluru, IN", "Berlin, DE" are India and Germany);
- other 2-letter tokens are ambiguous (IN = India or Indiana), so they count only when a city in
  that country confirms them ("Dublin, IE"); otherwise a US state code means US ("Denver, CO");
- one distinct country -> `country`; none or several -> None. Unknown tokens are ignored.
"""

import re
from dataclasses import dataclass

# (canonical name, ISO-2, is an HLD hub, aliases). Hubs are the India tech hubs and the 8 Europe
# hubs from HLD §4; the others are big tech cities, listed so their jobs are not "unknown".
_CITY_TABLE: tuple[tuple[str, str, bool, tuple[str, ...]], ...] = (
    # India hubs
    ("Bengaluru", "IN", True, ("bengaluru", "bangalore", "bengaluru urban")),
    ("Hyderabad", "IN", True, ("hyderabad",)),
    ("Pune", "IN", True, ("pune",)),
    ("Chennai", "IN", True, ("chennai",)),
    ("Gurugram", "IN", True, ("gurugram", "gurgaon")),
    ("Noida", "IN", True, ("noida", "greater noida")),
    ("Delhi", "IN", True, ("delhi", "new delhi", "delhi ncr")),
    ("Mumbai", "IN", True, ("mumbai", "bombay", "navi mumbai")),
    ("Kolkata", "IN", True, ("kolkata", "calcutta")),
    # Europe hubs
    ("Dublin", "IE", True, ("dublin",)),
    ("London", "GB", True, ("london",)),
    ("Amsterdam", "NL", True, ("amsterdam",)),
    ("Berlin", "DE", True, ("berlin",)),
    ("Zurich", "CH", True, ("zurich", "z\u00fcrich", "zuerich")),
    ("Warsaw", "PL", True, ("warsaw", "warszawa")),
    ("Stockholm", "SE", True, ("stockholm",)),
    ("Copenhagen", "DK", True, ("copenhagen", "k\u00f8benhavn", "kobenhavn")),
    # other tech cities (out of scope, but known)
    ("San Francisco", "US", False, ("san francisco", "san francisco bay area", "sf")),
    ("Palo Alto", "US", False, ("palo alto",)),
    ("Mountain View", "US", False, ("mountain view",)),
    ("Sunnyvale", "US", False, ("sunnyvale",)),
    ("San Jose", "US", False, ("san jose",)),
    ("Seattle", "US", False, ("seattle",)),
    ("New York", "US", False, ("new york", "new york city", "nyc")),
    ("Austin", "US", False, ("austin",)),
    ("Boston", "US", False, ("boston",)),
    ("Toronto", "CA", False, ("toronto",)),
    ("Vancouver", "CA", False, ("vancouver",)),
    ("Singapore", "SG", False, ("singapore",)),
    ("Tokyo", "JP", False, ("tokyo",)),
    ("Sydney", "AU", False, ("sydney",)),
    ("Tel Aviv", "IL", False, ("tel aviv", "tel aviv-yafo")),
)
CITIES: dict[str, tuple[str, str]] = {
    alias: (name, country) for name, country, _, aliases in _CITY_TABLE for alias in aliases
}
HUB_CITIES: frozenset[str] = frozenset(name for name, _, is_hub, _ in _CITY_TABLE if is_hub)

# Country names and ISO-3 codes. Includes common out-of-scope countries so their jobs are known
# (and filtered out) rather than counted as unknown.
COUNTRIES: dict[str, str] = {
    **dict.fromkeys(("india", "ind"), "IN"),
    **dict.fromkeys(("united kingdom", "uk", "great britain", "england", "scotland", "gbr"), "GB"),
    **dict.fromkeys(("ireland", "republic of ireland", "irl"), "IE"),
    **dict.fromkeys(("netherlands", "the netherlands", "holland", "nld"), "NL"),
    **dict.fromkeys(("germany", "deutschland", "deu"), "DE"),
    **dict.fromkeys(("switzerland", "che"), "CH"),
    **dict.fromkeys(("poland", "pol"), "PL"),
    **dict.fromkeys(("sweden", "swe"), "SE"),
    **dict.fromkeys(("denmark", "dnk"), "DK"),
    **dict.fromkeys(("united states", "united states of america", "us", "usa"), "US"),
    **dict.fromkeys(("canada",), "CA"),
    **dict.fromkeys(("singapore", "sgp"), "SG"),
    **dict.fromkeys(("japan", "jpn"), "JP"),
    **dict.fromkeys(("australia", "aus"), "AU"),
    **dict.fromkeys(("france", "fra"), "FR"),
    **dict.fromkeys(("spain", "esp"), "ES"),
    **dict.fromkeys(("israel", "isr"), "IL"),
    **dict.fromkeys(("brazil", "bra"), "BR"),
    **dict.fromkeys(("mexico", "mex"), "MX"),
}

_US_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
)
US_STATES = frozenset(_US_STATE_CODES.split())

_SPLIT = re.compile(r"\s*[,;/|()]\s*|\s+[-\u2013\u2014]\s+")
_REMOTE_PREFIX = re.compile(r"^remote\s*[-\u2013\u2014:]?\s*", re.IGNORECASE)
_REMOTE_SUFFIX = re.compile(r"\s*[-\u2013\u2014]?\s*remote$", re.IGNORECASE)


@dataclass(frozen=True)
class Location:
    country: str | None  # ISO-2, only when exactly one country was found
    countries: tuple[str, ...]  # every country found, in order of appearance
    city: str | None  # canonical city name, only when it is in `country`
    remote: bool


def parse_location(raw: str) -> Location:
    remote = False
    tokens: list[str] = []
    for part in _SPLIT.split(raw):
        token, was_remote = _strip_remote(part.strip())
        remote = remote or was_remote
        if token:
            tokens.append(token)

    keys = [token.casefold().replace(".", "").strip() for token in tokens]
    found: list[str] = []
    city: tuple[str, str] | None = None
    codes: list[str] = []
    for i, key in enumerate(keys):
        following = keys[i + 1].upper() if i + 1 < len(keys) else ""
        if key in CITIES:
            name, city_country = CITIES[key]
            if city_country != "US" and following in US_STATES and following != city_country:
                _add(found, "US")  # "Dublin, CA": the US namesake, not the hub
                continue
            city = city or (name, city_country)
            _add(found, city_country)
        elif key in COUNTRIES:
            _add(found, COUNTRIES[key])
        elif len(key) == 2 and key.isalpha():
            codes.append(key.upper())
    for code in codes:
        if code in found:
            continue  # confirmed by a city or name ("Dublin, IE", "Hyderabad, TS, IN")
        if code in US_STATES:
            _add(found, "US")  # "Denver, CO"; an unconfirmed "IN" is Indiana, not India

    country = found[0] if len(found) == 1 else None
    city_name = city[0] if city and city[1] == country else None
    return Location(country=country, countries=tuple(found), city=city_name, remote=remote)


def _strip_remote(token: str) -> tuple[str, bool]:
    if token.casefold() == "remote":
        return "", True
    for pattern in (_REMOTE_PREFIX, _REMOTE_SUFFIX):
        stripped = pattern.sub("", token)
        if stripped != token:
            return stripped.strip(), True
    return token, False


def _add(found: list[str], country: str) -> None:
    if country not in found:
        found.append(country)
