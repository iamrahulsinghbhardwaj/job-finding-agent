from pathlib import Path

import pytest

from jobagent.core.models import Platform, SourceStatus, Tier
from jobagent.core.registry import RegistryError, load_registry

REPO_ROOT = Path(__file__).resolve().parents[2]


def entry(slug: str, platform: str = "greenhouse", tenant: str | None = None) -> str:
    return f"""
  - slug: {slug}
    name: {slug.title()}
    tier: T1
    india_presence: true
    europe_presence: false
    us_transfer_path: true
    sources:
      - platform: {platform}
        tenant: {tenant or slug}
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "companies.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_valid_file_loads(tmp_path: Path) -> None:
    path = write(tmp_path, "companies:" + entry("stripe") + entry("plaid", "lever"))
    companies = load_registry(path)
    assert [c.slug for c in companies] == ["stripe", "plaid"]
    assert companies[1].sources[0].platform is Platform.LEVER


def test_company_without_sources_loads(tmp_path: Path) -> None:
    body = "companies:\n  - {slug: acme, name: Acme, tier: T1_STARTUP, india_presence: false,"
    body += " europe_presence: false, us_transfer_path: false}\n"
    assert load_registry(write(tmp_path, body))[0].sources == []


def test_duplicate_slug_fails(tmp_path: Path) -> None:
    path = write(tmp_path, "companies:" + entry("stripe") + entry("plaid") + entry("stripe", "lever"))
    with pytest.raises(RegistryError, match=r"duplicate slugs: stripe \(entries 1, 3\)"):
        load_registry(path)


def test_unknown_platform_fails(tmp_path: Path) -> None:
    path = write(tmp_path, "companies:" + entry("stripe") + entry("acme", "workday"))
    with pytest.raises(RegistryError, match=r"entry 2 \(acme\): sources\.0\.platform") as exc_info:
        load_registry(path)
    assert "greenhouse" in str(exc_info.value)  # lists the allowed platforms


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "top-level 'companies:' list"),
        ("- slug: stripe\n", "top-level 'companies:' list"),
        ("companies: stripe\n", "top-level 'companies:' list"),
        ("companies: [\n", "invalid YAML"),
        ("companies:\n  - slug: stripe\n", r"entry 1 \(stripe\): name: Field required"),
        ("companies:\n  - just-a-string\n", "entry 1: entry: Input should be"),
    ],
)
def test_malformed_files_fail(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(RegistryError, match=message):
        load_registry(write(tmp_path, body))


def test_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="file not found"):
        load_registry(tmp_path / "nope.yaml")


def test_committed_registry_is_valid() -> None:
    companies = {c.slug: c for c in load_registry(REPO_ROOT / "companies.yaml")}
    assert set(companies) == {"microsoft", "amazon", "stripe", "palantir", "openai"}
    assert all(companies[s].tier is Tier.T1 for s in ("microsoft", "amazon", "stripe"))
    assert companies["palantir"].tier is Tier.OTHER  # test entry for the Lever adapter
    assert companies["palantir"].sources[0].platform is Platform.LEVER
    assert companies["openai"].tier is Tier.OTHER  # test entry for the Ashby adapter
    assert companies["openai"].sources[0].platform is Platform.ASHBY
    microsoft = companies["microsoft"].sources[0]
    assert microsoft.platform is Platform.EIGHTFOLD
    assert microsoft.status is SourceStatus.BLOCKED  # 429 + HTML + Set-Cookie, see companies.yaml
    assert microsoft.blocked_reason
    assert microsoft.extra["host"] == "apply.careers.microsoft.com"
    assert companies["microsoft"].active_sources == []
    assert companies["amazon"].sources[0].tenant == "amazon.jobs"
    assert companies["stripe"].india_presence is False
    assert companies["stripe"].europe_presence is True
