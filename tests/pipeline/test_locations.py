import httpx
import pytest
import respx

from jobagent.adapters import build_source
from jobagent.core.http import PoliteClient
from jobagent.core.models import Company, RawJob, SearchSpec
from jobagent.pipeline.locations import HUB_CITIES, Location, parse_location
from jobagent.pipeline.normalize import normalize_all
from tests.adapters.conftest import FIXTURES

# (raw string, country, countries, city, remote). Every string here is a real location from the
# recorded fixtures; test_real_cases_come_from_fixtures keeps that true.
REAL_CASES = [
    # Amazon (India search)
    ("Bengaluru, Karnataka, IND", "IN", ("IN",), "Bengaluru", False),
    ("Chennai, Tamil Nadu, IND", "IN", ("IN",), "Chennai", False),
    ("Gurugram, Haryana, IND", "IN", ("IN",), "Gurugram", False),
    ("Hyderabad, Telangana, IND", "IN", ("IN",), "Hyderabad", False),
    ("Pune, Maharashtra, IND", "IN", ("IN",), "Pune", False),
    # Ashby (OpenAI)
    ("Bangalore - Remote", "IN", ("IN",), "Bengaluru", True),
    ("Delhi, India", "IN", ("IN",), "Delhi", False),
    ("London, UK", "GB", ("GB",), "London", False),
    ("Mumbai, India", "IN", ("IN",), "Mumbai", False),
    ("San Francisco", "US", ("US",), "San Francisco", False),
    ("Singapore", "SG", ("SG",), "Singapore", False),
    ("Tokyo, Japan", "JP", ("JP",), "Tokyo", False),
    ("US - Remote", "US", ("US",), None, True),
    # Greenhouse (Stripe)
    ("Bangalore", "IN", ("IN",), "Bengaluru", False),
    ("Bengaluru", "IN", ("IN",), "Bengaluru", False),
    ("Bengaluru, India", "IN", ("IN",), "Bengaluru", False),
    ("Dublin", "IE", ("IE",), "Dublin", False),
    ("Dublin, Ireland", "IE", ("IE",), "Dublin", False),
    ("LOCATION", None, (), None, False),
    ("London", "GB", ("GB",), "London", False),
    ("N/A", None, (), None, False),
    ("San Francisco, Seattle, New York", "US", ("US",), "San Francisco", False),
    ("Singapore", "SG", ("SG",), "Singapore", False),
    ("Toronto", "CA", ("CA",), "Toronto", False),
    ("US-Remote", "US", ("US",), None, True),
    # Lever (Palantir)
    ("Denver, CO", "US", ("US",), None, False),
    ("London, United Kingdom", "GB", ("GB",), "London", False),
    ("New York, NY", "US", ("US",), "New York", False),
    ("North America", None, (), None, False),
    ("Palo Alto, CA", "US", ("US",), "Palo Alto", False),
    ("Seattle, WA", "US", ("US",), "Seattle", False),
    ("Singapore, Singapore", "SG", ("SG",), "Singapore", False),
    ("Washington, D.C.", "US", ("US",), None, False),
]

OTHER_CASES = [
    # the brick's examples
    ("India, Telangana, Hyderabad", "IN", ("IN",), "Hyderabad", False),
    ("Hyderabad, TS, IN", "IN", ("IN",), "Hyderabad", False),  # IN confirmed by Hyderabad
    ("Dublin, IE", "IE", ("IE",), "Dublin", False),
    ("Remote - India", "IN", ("IN",), None, True),
    ("Multiple Locations, EMEA", None, (), None, False),
    # ambiguity and edge cases
    ("Indianapolis, IN", "US", ("US",), None, False),  # unconfirmed IN is Indiana
    # "<city>, <US state>" is always the US namesake; a bare "Dublin" is the Irish hub
    ("Dublin, CA", "US", ("US",), None, False),
    ("Dublin, OH", "US", ("US",), None, False),
    ("Vancouver, WA", "US", ("US",), None, False),
    # ...unless the 2-letter code is the city's own country (IN = Indiana, DE = Delaware too)
    ("Bengaluru, IN", "IN", ("IN",), "Bengaluru", False),
    ("Berlin, DE", "DE", ("DE",), "Berlin", False),
    # aliases
    ("Navi Mumbai, Maharashtra", "IN", ("IN",), "Mumbai", False),
    ("Bombay", "IN", ("IN",), "Mumbai", False),
    ("Greater Noida", "IN", ("IN",), "Noida", False),
    ("Delhi NCR", "IN", ("IN",), "Delhi", False),
    ("India", "IN", ("IN",), None, False),
    ("Tel Aviv-Yafo, Israel", "IL", ("IL",), "Tel Aviv", False),
    ("Mountain View, CA", "US", ("US",), "Mountain View", False),
    ("London / Dublin", None, ("GB", "IE"), None, False),  # several countries: all kept
    ("Berlin (Remote)", "DE", ("DE",), "Berlin", True),
    ("Bangalore \u2013 Remote", "IN", ("IN",), "Bengaluru", True),  # en dash
    ("Remote-India", "IN", ("IN",), None, True),
    ("Zürich, Switzerland", "CH", ("CH",), "Zurich", False),
    ("København, Denmark", "DK", ("DK",), "Copenhagen", False),
    ("Warszawa; Poland", "PL", ("PL",), "Warsaw", False),
    ("Amsterdam, Netherlands", "NL", ("NL",), "Amsterdam", False),
    ("Stockholm, SE", "SE", ("SE",), "Stockholm", False),
    ("Gurgaon | Haryana", "IN", ("IN",), "Gurugram", False),
    ("  new delhi ,  india  ", "IN", ("IN",), "Delhi", False),
    ("Remote", None, (), None, True),
    ("", None, (), None, False),
]


@pytest.mark.parametrize(("raw", "country", "countries", "city", "remote"), REAL_CASES + OTHER_CASES)
def test_parse_location(
    raw: str, country: str | None, countries: tuple[str, ...], city: str | None, remote: bool
) -> None:
    assert parse_location(raw) == Location(country=country, countries=countries, city=city, remote=remote)


def test_at_least_30_real_strings() -> None:
    assert len({case[0] for case in REAL_CASES}) >= 30


def test_city_only_implies_its_own_country() -> None:
    # a hub city is reported only when it lies in the chosen country
    assert parse_location("London, India").city is None
    assert parse_location("London, India").countries == ("GB", "IN")


async def _fixture_jobs(companies: list[Company], http: PoliteClient) -> list[RawJob]:
    replies = {
        "https://boards-api.greenhouse.io": "greenhouse_stripe.json",
        "https://api.lever.co": "lever_palantir.json",
        "https://api.ashbyhq.com": "ashby_openai.json",
        "https://www.amazon.jobs": "amazon_india_sde.json",
    }
    spec = SearchSpec(keywords=[], countries=["IN"], max_pages=1)
    found: list[RawJob] = []
    with respx.mock(assert_all_called=False) as mock:
        for prefix, name in replies.items():
            body = (FIXTURES / name).read_bytes()
            mock.get(url__startswith=prefix).mock(return_value=httpx.Response(200, content=body))
        for company in companies:
            source = build_source(company, company.sources[0], http)
            found += [job async for job in source.search(spec, None)]
    return found


async def test_real_cases_come_from_fixtures(
    stripe: Company, palantir: Company, openai: Company, amazon: Company, http: PoliteClient
) -> None:
    jobs = await _fixture_jobs([stripe, palantir, openai, amazon], http)
    missing = {case[0] for case in REAL_CASES} - {job.location_raw for job in jobs}
    assert not missing, f"not in any fixture: {sorted(missing)}"


async def test_unknown_location_share_across_fixtures_is_under_5_percent(
    stripe: Company, palantir: Company, openai: Company, amazon: Company, http: PoliteClient
) -> None:
    jobs = await _fixture_jobs([stripe, palantir, openai, amazon], http)
    _, stats = normalize_all(jobs, source="fixtures")
    assert stats.total == 80
    assert stats.unknown_location == 3  # "LOCATION", "N/A", "North America"
    assert stats.unknown_share < 0.05


def test_all_hld_europe_hubs_are_mapped() -> None:
    hubs = {"Dublin", "London", "Amsterdam", "Berlin", "Zurich", "Warsaw", "Stockholm", "Copenhagen"}
    assert hubs <= HUB_CITIES
    assert parse_location("Z\u00fcrich").city == "Zurich"
    assert parse_location("K\u00f8benhavn").city == "Copenhagen"


def test_only_hld_hubs_are_hubs() -> None:
    assert "San Francisco" not in HUB_CITIES
    assert len(HUB_CITIES) == 9 + 8  # India hubs + Europe hubs
