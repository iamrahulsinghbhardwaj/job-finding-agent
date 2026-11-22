"""Run entry point: `python -m jobagent.jobs.run --dry-run`."""

import argparse
import sys
from collections.abc import Sequence

from jobagent.core.settings import Settings


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jobagent.jobs.run", description="Run the job agent.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load settings, report which are present (names only) and exit.",
    )
    return parser.parse_args(argv)


def _report_settings(settings: Settings) -> None:
    present = settings.present_settings()
    for name, is_set in present.items():
        print(f"{name}: {'present' if is_set else 'missing'}")
    print(f"{sum(present.values())}/{len(present)} settings present")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = Settings()
    if args.dry_run:
        _report_settings(settings)
        return 0
    print("Full run not implemented yet; use --dry-run.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
