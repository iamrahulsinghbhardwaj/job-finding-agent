"""Print the company registry: `python -m jobagent.jobs.registry [--path companies.yaml]`."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from jobagent.core.models import Company
from jobagent.core.registry import RegistryError, load_registry

COLUMNS = ("slug", "tier", "platform", "tenant", "status")
Row = tuple[str, str, str, str, str]


def registry_rows(companies: list[Company]) -> list[Row]:
    """One row per source; companies without sources get a single row with '-'."""
    rows: list[Row] = []
    for company in companies:
        if not company.sources:
            rows.append((company.slug, company.tier.value, "-", "-", "-"))
        for source in company.sources:
            rows.append(
                (company.slug, company.tier.value, source.platform.value, source.tenant, source.status.value)
            )
    return rows


def format_table(rows: list[Row]) -> str:
    widths = [max(len(cell) for cell in column) for column in zip(COLUMNS, *rows, strict=True)]
    lines = [COLUMNS, tuple("-" * w for w in widths), *rows]
    return "\n".join(_format_line(line, widths) for line in lines)


def _format_line(cells: Sequence[str], widths: list[int]) -> str:
    return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobagent.jobs.registry", description="Print the company registry.")
    parser.add_argument("--path", type=Path, default=Path("companies.yaml"), help="registry file")
    args = parser.parse_args(argv)
    try:
        companies = load_registry(args.path)
    except RegistryError as exc:
        print(f"Registry error: {exc}", file=sys.stderr)
        return 1
    print(format_table(registry_rows(companies)))
    print(f"\n{len(companies)} companies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
