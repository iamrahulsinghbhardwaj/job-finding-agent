"""Every location string that was UNKNOWN in the first live crawl (dev branch, 2026-11-24).

93 distinct strings, 235 jobs (Stripe, OpenAI, Palantir). Each case was checked by hand. The
`countries` list is the truth for scope: a job is in scope if ANY of its countries matches the tier.
Strings that stay unknown (no country) are only the truly vague ones: N/A, LOCATION, na, AMER,
North America, a bare Remote, and SEA (Seattle or Southeast Asia: never guessed).
"""

import pytest

from jobagent.pipeline.locations import parse_location

# (raw string, countries, city, remote)
LIVE_UNKNOWNS: list[tuple[str, tuple[str, ...], str | None, bool]] = [
    ("N/A", (), None, False),  # 24 jobs
    ("Mexico City", ("MX",), "Mexico City", False),  # 19 jobs
    ("Chicago", ("US",), "Chicago", False),  # 18 jobs
    ("Seoul, South Korea", ("KR",), "Seoul", False),  # 15 jobs
    ("São Paulo", ("BR",), "Sao Paulo", False),  # 8 jobs
    ("US / Canada", ("US", "CA"), None, False),  # 8 jobs
    ("LOCATION", (), None, False),  # 7 jobs
    ("Barcelona", ("ES",), "Barcelona", False),  # 5 jobs
    ("Sydney Or Melbourne", ("AU",), "Sydney", False),  # 5 jobs
    ("Bucharest", ("RO",), "Bucharest", False),  # 4 jobs
    ("Dublin, London", ("IE", "GB"), None, False),  # 4 jobs
    ("NYC-Privy", ("US",), "New York", False),  # 4 jobs
    ("Ontario - Remote", ("CA",), None, True),  # 4 jobs
    ("Chicago, Illinois", ("US",), "Chicago", False),  # 3 jobs
    ("London OR Dublin", ("GB", "IE"), None, False),  # 3 jobs
    ("Luxembourg", ("LU",), "Luxembourg", False),  # 3 jobs
    ("Paris", ("FR",), "Paris", False),  # 3 jobs
    ("Poland OR Romania", ("PL", "RO"), None, False),  # 3 jobs
    ("Remote", (), None, True),  # 3 jobs
    ("Remote in the US", ("US",), None, True),  # 3 jobs
    ("na", (), None, False),  # 3 jobs
    ("AMER", (), None, False),  # 2 jobs
    ("Dublin/London", ("IE", "GB"), None, False),  # 2 jobs
    ("London, Dublin", ("GB", "IE"), None, False),  # 2 jobs
    ("Madrid", ("ES",), "Madrid", False),  # 2 jobs
    ("Mexico City, MX", ("MX",), "Mexico City", False),  # 2 jobs
    ("SEA", (), None, False),  # 2 jobs
    ("San Francisco Or New York", ("US",), "San Francisco", False),  # 2 jobs
    ("San Francisco or Seattle", ("US",), "San Francisco", False),  # 2 jobs
    ("Sao Paulo", ("BR",), "Sao Paulo", False),  # 2 jobs
    ("Seattle or San Francisco", ("US",), "Seattle", False),  # 2 jobs
    ("Sydney or Melbourne", ("AU",), "Sydney", False),  # 2 jobs
    ("Taipei,Taiwan", ("TW",), "Taipei", False),  # 2 jobs
    ("Taiwan", ("TW",), None, False),  # 2 jobs
    ("US-NYC", ("US",), "New York", False),  # 2 jobs
    ("Abu Dhabi, UAE", ("AE",), "Abu Dhabi", False),  # 1 job
    ("Abu Dhabi, United Arab Emirates", ("AE",), "Abu Dhabi", False),  # 1 job
    ("Auckland", ("NZ",), "Auckland", False),  # 1 job
    ("Auckland, New Zealand", ("NZ",), "Auckland", False),  # 1 job
    ("Belgium", ("BE",), None, False),  # 1 job
    ("Berlin and Munich", ("DE",), "Berlin", False),  # 1 job
    ("Brussels", ("BE",), "Brussels", False),  # 1 job
    ("Bucharest, Romania", ("RO",), "Bucharest", False),  # 1 job
    ("CDMX", ("MX",), "Mexico City", False),  # 1 job
    ("Chicago, Atlanta, Remote", ("US",), "Chicago", True),  # 1 job
    ("Chicago, Atlanta, US-Remote, Toronto", ("US", "CA"), None, True),  # 1 job
    ("Chicago, IL; Atlanta, GA; Toronto, Canada", ("US", "CA"), None, False),  # 1 job
    ("Chicago, Remote", ("US",), "Chicago", True),  # 1 job
    ("Chicago; Atlanta", ("US",), "Chicago", False),  # 1 job
    ("Dublin OR Paris", ("IE", "FR"), None, False),  # 1 job
    ("Dublin, London, Brussels", ("IE", "GB", "BE"), None, False),  # 1 job
    ("Dublin, London, Paris, Berlin, Stockholm", ("IE", "GB", "FR", "DE", "SE"), None, False),  # 1 job
    ("IN-Bengaluru", ("IN",), "Bengaluru", False),  # 1 job
    ("London or Dublin", ("GB", "IE"), None, False),  # 1 job
    ("London, Dublin, UK-Remote", ("GB", "IE"), None, True),  # 1 job
    ("MX- Mexico City", ("MX",), "Mexico City", False),  # 1 job
    ("Madrid, Barcelona", ("ES",), "Madrid", False),  # 1 job
    ("Malaysia", ("MY",), None, False),  # 1 job
    ("New York City, Toronto, Chicago, EMEA, or Remote", ("US", "CA"), None, True),  # 1 job
    ("New York Privy HQ", ("US",), "New York", False),  # 1 job
    ("New York, US - Remote (East Coast), Toronto", ("US", "CA"), None, True),  # 1 job
    ("New York, US, Canada, Remote", ("US", "CA"), None, True),  # 1 job
    ("New Zealand", ("NZ",), None, False),  # 1 job
    ("North America", (), None, False),  # 1 job
    ("Remote/Hybrid", (), None, True),  # 1 job
    ("SEA, SF, NY, Toronto", ("US", "CA"), None, False),  # 1 job
    ("SF, SEA, NYC, Chicago, Toronto", ("US", "CA"), None, False),  # 1 job
    ("SSF, Seattle, NYC, Chicago, Toronto", ("US", "CA"), None, False),  # 1 job
    ("San Francisco, New York, Seattle, Chicago, Remote, Toronto", ("US", "CA"), None, True),  # 1 job
    ("San Francsico", ("US",), "San Francisco", False),  # 1 job
    ("Seattle or San Francisco or Chicago or New York City or US-Remote", ("US",), "Seattle", True),  # 1 job
    (
        "Seattle, San Francisco, New York, Chicago, Atlanta, Remote in the US, Canada",
        ("US", "CA"),
        None,
        True,
    ),  # 1 job
    ("Seattle, San Francisco, Seattle, New York, Toronto", ("US", "CA"), None, False),  # 1 job
    ("Taipei, Taiwan", ("TW",), "Taipei", False),  # 1 job
    ("Toronto or Chicago", ("CA", "US"), None, False),  # 1 job
    ("US Hybrid", ("US",), None, False),  # 1 job
    ("US Remote; Canada", ("US", "CA"), None, True),  # 1 job
    ("US, Canada", ("US", "CA"), None, False),  # 1 job
    ("US-NY", ("US",), None, False),  # 1 job
    (
        "US-NYC; US-San Francisco; US-Seattle; US-Remote; US- Chicago; US-Atlanta; "
        "Canada-Toronto; Canada-Remote",
        ("US", "CA"),
        None,
        True,
    ),  # 1 job
    ("US-SF", ("US",), "San Francisco", False),  # 1 job
    (
        "US-SF, US-Seattle, US-NYC, US-Chicago, US-Georgia or US-Remote",
        ("US",),
        "San Francisco",
        True,
    ),  # 1 job
    ("US-SF, US-Seattle, US-West-Remote", ("US",), "San Francisco", True),  # 1 job
    ("US-SF-HQ", ("US",), "San Francisco", False),  # 1 job
    ("US-SF-HQ, US-NYC, US-CHI, US-SEA", ("US",), "San Francisco", False),  # 1 job
    ("US-SF-HQ, US-SEA, US-CHI, US-NYC", ("US",), "San Francisco", False),  # 1 job
    ("US-San Francisco", ("US",), "San Francisco", False),  # 1 job
    ("US-San Francisco, US- New York, US-Chicago", ("US",), "San Francisco", False),  # 1 job
    ("US-Seattle, US-Chicago", ("US",), "Seattle", False),  # 1 job
    ("United Kingdom; Ireland", ("GB", "IE"), None, False),  # 1 job
    ("Vilnius, Lithuania", ("LT",), "Vilnius", False),  # 1 job
    ("Washington DC metro area", ("US",), "Washington", False),  # 1 job
    ("Washington DC-metro area", ("US",), "Washington", False),  # 1 job
]

STILL_UNKNOWN = {"N/A", "LOCATION", "na", "AMER", "SEA", "North America", "Remote", "Remote/Hybrid"}


@pytest.mark.parametrize(("raw", "countries", "city", "remote"), LIVE_UNKNOWNS)
def test_live_unknown_strings(raw: str, countries: tuple[str, ...], city: str | None, remote: bool) -> None:
    location = parse_location(raw)
    assert (location.countries, location.city, location.remote) == (countries, city, remote)
    assert location.country == (countries[0] if len(countries) == 1 else None)


def test_table_is_complete_and_only_vague_strings_stay_unknown() -> None:
    assert len(LIVE_UNKNOWNS) == 93
    assert len({case[0] for case in LIVE_UNKNOWNS}) == 93
    assert {raw for raw, countries, _, _ in LIVE_UNKNOWNS if not countries} == STILL_UNKNOWN
