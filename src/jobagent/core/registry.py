"""Load and validate the company registry (companies.yaml)."""

from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from jobagent.core.models import Company


class RegistryError(ValueError):
    """The registry file is missing, malformed or inconsistent."""


def load_registry(path: Path) -> list[Company]:
    """Parse `path` into validated companies. Raises RegistryError on any problem."""
    entries = _read_entries(path)
    companies = [_parse_entry(i, entry, path) for i, entry in enumerate(entries, start=1)]
    _check_unique_slugs(companies, path)
    return companies


def _read_entries(path: Path) -> list[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RegistryError(f"{path}: file not found") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("companies"), list):
        raise RegistryError(f"{path}: expected a top-level 'companies:' list")
    return data["companies"]


def _parse_entry(index: int, entry: Any, path: Path) -> Company:
    label = f"entry {index}"
    if isinstance(entry, dict) and isinstance(entry.get("slug"), str):
        label += f" ({entry['slug']})"
    try:
        return Company.model_validate(entry)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'entry'}: {err['msg']}" for err in exc.errors()
        )
        raise RegistryError(f"{path}: {label}: {problems}") from None


def _check_unique_slugs(companies: list[Company], path: Path) -> None:
    positions: defaultdict[str, list[int]] = defaultdict(list)
    for i, company in enumerate(companies, start=1):
        positions[company.slug].append(i)
    duplicates = {slug: idx for slug, idx in positions.items() if len(idx) > 1}
    if duplicates:
        listed = ", ".join(f"{slug} (entries {', '.join(map(str, idx))})" for slug, idx in duplicates.items())
        raise RegistryError(f"{path}: duplicate slugs: {listed}")
