"""The ranking profile, built from the CV (HLD §10 "CV loading").

At the start of a run: download the CV PDF with its read-only SAS URL, extract the text, and hash
it together with PROFILE_MODEL and PROFILE_PROMPT_VERSION (the "profile key"). Only when the key
differs from the newest stored profile's, one LLM call (Structured Outputs) turns the text into an
ExtractedProfile (roles with dates, skills, ...), stored as a new version in cv_profile. So a new
CV, a new model or a changed prompt each rebuild the profile; nothing else does.

Reading it back resolves it for today (profile_schema.resolve): years of experience counted in
code from the role dates, then the private overrides file (PROFILE_OVERRIDES_SAS_URL, optional)
merged on top. Neither needs an LLM call, so years stay current and overrides apply at once.

The CV text, the profile, the overrides and the SAS URLs are private: nothing here logs them. Logs
get the profile version, whether it was rebuilt, overridden field names, and token counts.
"""

import hashlib
import io
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlparse

import yaml
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from sqlalchemy.orm import Session

from jobagent.core import llm
from jobagent.core.http import PoliteClient
from jobagent.db import repo
from jobagent.pipeline.profile_schema import (
    CandidateProfile,
    ExtractedProfile,
    ProfileOverrides,
    effective_profile_hash,
    overridden_text,
    resolve,
)

log = logging.getLogger(__name__)

PURPOSE = "profile"
# Bump whenever INSTRUCTIONS or ExtractedProfile change, so the next run rebuilds the profile.
PROFILE_PROMPT_VERSION = 2
SAS_WARN_DAYS = 14

INSTRUCTIONS = """\
You turn a software engineer's CV into a structured profile used to rank job postings for them.
Use only what the CV states or clearly implies; never invent skills, employers, dates or numbers.
roles: every position under work experience, newest first, with dates exactly as the CV gives
them (YYYY-MM, or YYYY when only a year is given). A current role ("Present", "Now", "Current")
has end null. Mark internships, co-ops, apprenticeships and student positions is_internship.
Do not compute years of experience or seniority: the program does that from the dates.
Keep list items short (a few words; projects one line each)."""


class CvError(Exception):
    """The CV could not be read. Message is safe to log (no URL, no text)."""


class OverridesError(Exception):
    """The overrides file could not be read or is invalid. Message names fields, never values."""


class ProfileError(Exception):
    """The stored profile cannot be used (e.g. built by an older prompt). Safe to log."""


@dataclass(frozen=True)
class StoredProfile:
    version: int  # cv_profile.id; rankings record it
    key: str  # profile_key() it was built from
    model: str
    created_at: datetime
    profile: CandidateProfile  # resolved: years up to today, overrides merged
    overridden: tuple[str, ...]  # fields the overrides file replaced
    effective_hash: str  # effective_profile_hash(version, overrides): stored on every Ranking row


@dataclass(frozen=True)
class ProfileSync:
    version: int
    rebuilt: bool
    call: llm.LlmCall | None  # the LLM call, when rebuilt
    profile: CandidateProfile
    overridden: tuple[str, ...]
    effective_hash: str  # effective_profile_hash(version, overrides): stored on every Ranking row


async def load_cv_text(http: PoliteClient, url: str) -> str:
    """Download the CV PDF and return its text. Never log the result."""
    return extract_text(await http.get_bytes(url))


def extract_text(pdf: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf))
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PyPdfError, ValueError, OSError):
        raise CvError("CV is not a readable PDF") from None
    text = "\n".join(line.rstrip() for page in pages for line in page.splitlines()).strip()
    if not text:
        raise CvError("CV PDF has no extractable text (a scanned image?)")
    return text


async def load_overrides(http: PoliteClient, url: str) -> ProfileOverrides:
    """Download and validate the private overrides file (YAML). Never log its content."""
    raw = await http.get_bytes(url)
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise OverridesError("overrides file is not valid UTF-8 YAML") from None
    if not isinstance(data, dict):
        raise OverridesError("overrides file must be a YAML mapping (target_titles: [...], ...)")
    try:
        return ProfileOverrides.model_validate(data)
    except ValidationError as exc:
        # Locations and error types only: pydantic's message would quote the values.
        problems = sorted({f"{'.'.join(map(str, e['loc'])) or 'file'} ({e['type']})" for e in exc.errors()})
        raise OverridesError(f"overrides file is invalid: {', '.join(problems)}") from None


def profile_key(cv_text: str, model: str, prompt_version: int) -> str:
    """sha256 over the CV text, the model and the prompt version: a change in any one rebuilds."""
    material = json.dumps({"cv": cv_text, "model": model, "prompt_version": prompt_version}, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def sas_days_left(url: str, now: datetime | None = None) -> int | None:
    """Whole days until the SAS URL's expiry (`se=`), or None when it has none we can read."""
    expiry = parse_qs(urlparse(url).query).get("se")
    if not expiry:
        return None
    try:
        expires = datetime.fromisoformat(expiry[0].replace("Z", "+00:00"))
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return (expires - (now or datetime.now(UTC))).days


async def build_profile(
    client: AsyncOpenAI, *, model: str, cv_text: str, ledger: llm.LlmLedger | None = None
) -> tuple[ExtractedProfile, llm.LlmCall]:
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": INSTRUCTIONS},
        {"role": "user", "content": cv_text},
    ]
    return await llm.parse(
        client, model=model, messages=messages, schema=ExtractedProfile, purpose=PURPOSE, ledger=ledger
    )


async def ensure_profile(
    session: Session,
    *,
    http: PoliteClient,
    client: AsyncOpenAI,
    cv_url: str,
    model: str,
    overrides_url: str | None = None,
    ledger: llm.LlmLedger | None = None,
    today: date | None = None,
) -> ProfileSync:
    """Rebuild the stored profile only when its key (CV text, model, prompt version) changed,
    then resolve it for today with the overrides file (when configured). Flushes, never commits.

    The overrides are read every run and are not part of the key: editing them costs no LLM call.
    """
    overrides = await load_overrides(http, overrides_url) if overrides_url else None
    text = await load_cv_text(http, cv_url)
    key = profile_key(text, model, PROFILE_PROMPT_VERSION)
    latest = repo.latest_cv_profile(session)
    if latest is not None and latest.hash == key:
        extracted = _extracted(latest.id, latest.profile_json)
        version, rebuilt, call = latest.id, False, None
        log.info("profile unchanged (v%d)", version)
    else:
        extracted, call = await build_profile(client, model=model, cv_text=text, ledger=ledger)
        version = repo.add_cv_profile(session, key=key, model=model, profile_json=extracted.model_dump())
        rebuilt = True
        log.info("profile rebuilt (v%d)", version)
    profile, overridden = resolve(extracted, overrides, today or _today())
    log.info("profile overrides: %s", overridden_text(overridden))  # field names only
    effective_hash = effective_profile_hash(version, overrides)
    return ProfileSync(version, rebuilt, call, profile, overridden, effective_hash)


def get_profile(
    session: Session, *, overrides: ProfileOverrides | None = None, today: date | None = None
) -> StoredProfile | None:
    """The newest stored profile resolved for `today` (default: now), or None before the first build."""
    row = repo.latest_cv_profile(session)
    if row is None:
        return None
    profile, overridden = resolve(_extracted(row.id, row.profile_json), overrides, today or _today())
    return StoredProfile(
        version=row.id,
        key=row.hash,
        model=row.model,
        created_at=row.created_at,
        profile=profile,
        overridden=overridden,
        effective_hash=effective_profile_hash(row.id, overrides),
    )


def _extracted(version: int, data: object) -> ExtractedProfile:
    try:
        return ExtractedProfile.model_validate(data)
    except ValidationError:
        raise ProfileError(
            f"stored profile v{version} has an older shape: run `python -m jobagent.jobs.profile` to rebuild"
        ) from None


def _today() -> date:
    return datetime.now(UTC).date()
