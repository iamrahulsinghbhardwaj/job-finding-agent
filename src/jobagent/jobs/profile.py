"""The CV profile: `python -m jobagent.jobs.profile [--show]`.

Without flags: download the CV, rebuild the profile if the CV (or model, or prompt) changed, print
the version and the LLM cost. `--show` prints the resolved profile JSON (years up to today, the
overrides file merged in). It is derived from your private CV, so run it in your own terminal
only, never in CI (Actions logs are public).
"""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence

from openai import AsyncOpenAI
from sqlalchemy.orm import Session

from jobagent.core.http import FetchError, PoliteClient
from jobagent.core.llm import LlmError, LlmLedger
from jobagent.core.settings import Settings
from jobagent.db.session import make_engine
from jobagent.pipeline.profile import (
    SAS_WARN_DAYS,
    CvError,
    OverridesError,
    ProfileError,
    ProfileSync,
    ensure_profile,
    get_profile,
    load_overrides,
    sas_days_left,
)
from jobagent.pipeline.profile_schema import ProfileOverrides, overridden_text

ERRORS = (CvError, OverridesError, ProfileError, FetchError, LlmError)
LINKS = (("cv_blob_sas_url", "CV link"), ("profile_overrides_sas_url", "overrides link"))

SYNC_SETTINGS = ("database_url", "openai_api_key", "profile_model", "cv_blob_sas_url")


def format_sync(sync: ProfileSync, ledger: LlmLedger) -> str:
    overrides = f"overrides: {overridden_text(sync.overridden)}"
    if not sync.rebuilt:
        return f"profile: unchanged (v{sync.version}), no LLM call; {overrides}"
    cost = "unknown cost" if ledger.unpriced else f"${ledger.cost_usd:.4f}"
    return (
        f"profile: rebuilt (v{sync.version}), {len(ledger.calls)} LLM call, "
        f"{ledger.input_tokens:,} in / {ledger.output_tokens:,} out tokens, {cost}; {overrides}"
    )


def expiry_warning(
    days_left: int | None, setting: str = "cv_blob_sas_url", label: str = "CV link"
) -> str | None:
    if days_left is None or days_left >= SAS_WARN_DAYS:
        return None
    name = setting.upper()
    if days_left < 0:
        return f"warning: the {label} ({name}) has expired: generate a new SAS URL"
    return f"warning: the {label} ({name}) expires in {days_left} days: generate a new SAS URL"


def _optional(settings: Settings, name: str) -> str | None:
    return settings.require(name) if getattr(settings, name) is not None else None


async def _sync(session: Session, settings: Settings, ledger: LlmLedger) -> ProfileSync:
    async with PoliteClient() as http:
        client = AsyncOpenAI(api_key=settings.require("openai_api_key"))
        try:
            return await ensure_profile(
                session,
                http=http,
                client=client,
                cv_url=settings.require("cv_blob_sas_url"),
                model=settings.require("profile_model"),
                overrides_url=_optional(settings, "profile_overrides_sas_url"),
                ledger=ledger,
            )
        finally:
            await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jobagent.jobs.profile", description="Build or show the CV profile."
    )
    parser.add_argument("--show", action="store_true", help="print the stored profile JSON (local only)")
    args = parser.parse_args(argv)

    settings = Settings()
    needed = ("database_url",) if args.show else SYNC_SETTINGS
    missing = [name.upper() for name in needed if getattr(settings, name) is None]
    if missing:
        print(f"Missing in .env: {', '.join(missing)}", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("openai").setLevel(logging.WARNING)  # httpx URL logs are quieted in core/http

    engine = make_engine(settings.require("database_url"))
    try:
        with Session(engine) as session:
            if args.show:
                try:
                    return _show(session, _fetch_overrides(settings))
                except ERRORS as exc:
                    print(f"profile: failed: {exc}", file=sys.stderr)
                    return 1
            ledger = LlmLedger()
            try:
                sync = asyncio.run(_sync(session, settings, ledger))
            except ERRORS as exc:
                print(f"profile: failed: {exc}", file=sys.stderr)
                return 1
            session.commit()
    finally:
        engine.dispose()

    print(format_sync(sync, ledger))
    for setting, label in LINKS:
        url = _optional(settings, setting)
        warning = expiry_warning(sas_days_left(url), setting, label) if url else None
        if warning:
            print(warning, file=sys.stderr)
    return 0


def _fetch_overrides(settings: Settings) -> ProfileOverrides | None:
    url = _optional(settings, "profile_overrides_sas_url")
    if url is None:
        return None

    async def fetch() -> ProfileOverrides:
        async with PoliteClient() as http:
            return await load_overrides(http, url)

    return asyncio.run(fetch())


def _show(session: Session, overrides: ProfileOverrides | None = None) -> int:
    stored = get_profile(session, overrides=overrides)
    if stored is None:
        print("No profile stored yet: run `python -m jobagent.jobs.profile` first.", file=sys.stderr)
        return 1
    print(f"profile v{stored.version}, model {stored.model}, built {stored.created_at:%Y-%m-%d %H:%M %Z}")
    print(f"overridden: {overridden_text(stored.overridden)}")
    print(f"effective profile hash: {stored.effective_hash}")
    print(json.dumps(stored.profile.model_dump(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
