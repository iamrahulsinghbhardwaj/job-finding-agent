# TODO

Tick a box (`- [ ]` → `- [x]`) when a task is done. Each phase ends with a **gate**: don't start the next phase until the gate passes.
Design: [docs/HLD.md](docs/HLD.md)

**Current phase:** Phase 0 · Validate

---

## Open decisions

- [ ] Confirm the ~50 Tier 1 companies (incl. whether Flipkart, PhonePe, Razorpay, CRED, Swiggy, Zomato, Meesho count as Tier 1)
- [x] Upload CV (PDF) to a private Azure Blob container; create a read-only SAS URL for that blob (with expiry); save it as the `CV_BLOB_SAS_URL` secret
- [ ] Decide hours per week committed to this project
- [ ] Confirm experience rule: roles asking 4+ years, Tier 1 capped at 6

---

## Phase 0 · Validate (week 1)

- [ ] Set up a Claude scheduled task: crawl 10 Tier 1 companies twice a day and email a digest
- [ ] Read the digest daily for 7 days; note what was useful and what was noise
- [ ] Adjust title, location and experience filters from what you saw
- [ ] **Gate:** digest was useful on at least 5 of 7 days

## Phase 1 · MVP (weeks 2–3)

**Project setup**
- [x] Python 3.12 skeleton: `core/`, `adapters/`, `pipeline/`, `agents/`, `digest/`, `jobs/`, `tests/`
- [x] `pyproject.toml`, ruff, pytest, pre-commit
- [ ] Neon Postgres project on the Free plan; `DATABASE_URL` secret
- [x] CV loader: download via SAS, extract text with `pypdf`, hash; rebuild ranking profile only when the hash changes (hash of CV text + PROFILE_MODEL + PROFILE_PROMPT_VERSION)
  - `pipeline/profile.py`, table `cv_profile` (id = profile version), CLI `python -m jobagent.jobs.profile [--show]`
  - Not yet called by the daily run: the ranker brick calls `ensure_profile` at run start
  - Still to do: add `CV_BLOB_SAS_URL` as a GitHub Actions secret; the profile CLI warns when the SAS has under 14 days left
- [x] Alembic migrations: Company, Source, Job, Ranking, Run
- [ ] OpenAI key as `OPENAI_API_KEY` secret + monthly budget cap in the OpenAI dashboard
- [ ] Gmail API OAuth (send, compose drafts, read) → `GMAIL_TOKEN` secret
- [ ] `.github/workflows/run.yml`: cron `30 2,12 * * *` + `workflow_dispatch`
- [ ] `.github/workflows/keepalive.yml`: weekly, so the schedule never pauses
- [ ] Logging rule: counts only, never job data, CV or email content (Actions logs are public)

**Registry**
- [ ] `companies.yaml` seed: ~50 Tier 1 with careers URL, platform, India/Europe presence, `us_transfer_path`
- [x] Loader that upserts the registry into the DB

**Adapters**
- [x] `JobSource` protocol + `SearchSpec`, `RawJob`, `JobDetail`, `HealthReport` models
- [x] Greenhouse adapter
- [x] Lever adapter
- [x] Ashby adapter
- [ ] Eightfold adapter (Microsoft, Netflix): `pcsx/search` + `pcsx/position_details`
- [x] Amazon adapter (`search.json`, split by category to stay under the 10,000 cap)
- [ ] Contract tests per adapter against recorded responses
- [x] Incremental crawl: paged sources stop a query at the first page of already-stored ids (`max_pages` hard cap); board sources fetch the whole board
- [ ] Weekly full crawl for paged sources (Amazon) with `complete=True`, so their closed jobs go stale
- [x] Per-domain rate limit, backoff retries, dead-letter table

**Pipeline**
- [x] Normalize: location → ISO country code; title cleanup
- [x] Dedupe upsert on (company, source_job_id); `first_seen` / `last_seen`
- [x] Cross-source dedupe on company + canonical title + country (jobs from the same source are never merged)
- [x] Rule filter: India + Europe hubs (incl. Copenhagen), title keywords, exclusions (Staff, Principal, Manager, Intern, Contract), 4–6 YOE
  - Scope by `NormalizedJob.countries` (the truth), not `country`: a job is in scope if ANY of its countries matches the tier ("London or Dublin", "US / Canada")
  - Bare "Remote" with no country: Tier 1 → drop but count; Tier 1 Startups → keep, label "Remote (unspecified)"
  - Decided: Europe by country, not hub city (Munich in DE is in scope)
  - Lists live in `pipeline/filter_config.py`; min_yoe read from listings (all four platforms)
  - [ ] Per-company SDE level map (e.g. Google SWE III = the 4–6 YOE band); hook is `filter.sde2_levels`
- [x] LLM ranker with Structured Outputs (score, fit, reason) + rule-score fallback
  - `pipeline/rank.py`: static rubric + profile first, job last in `<job_posting>` tags; 5 at a time; 2 attempts; per-run token budget (`RANK_TOKEN_BUDGET`)
  - Ranks survivors with no LLM ranking for the current effective profile hash (rule rows re-ranked next run); stale jobs skipped
  - Description from the listing (stored for survivors only); `get_details` + re-filter only when the listing had none
  - Prompt caching: gpt-5.4 models do not cache the shared prefix across different jobs (checked live); revisit with gpt-5.6 explicit cache breakpoints
  - [ ] Pick RANK_MODEL (nano vs mini comparison done)
- [x] Mark jobs stale after 3 missed runs

**Digest**
- [x] HTML template: Tier 1 India, Tier 1 Europe; show the ORIGINAL title (`title`), never `title_canonical`
  - `digest/select.py` + `digest/render.py` + `digest/templates/digest.html.j2`; `--dry-run` writes `out/digest.html`
  - Every section: all High in full, top 10 Medium in full, every other Medium as a compact row; Low never sent
  - Exact duplicates (company + title + city + description hash) merged into one line
  - [ ] Gmail clips HTML over ~102 KB: the first (backlog) digest is ~129 KB, half of it inline styles
- [x] Run summary: include `NormalizeStats.as_counts()` per source in `Run.per_source_counts` (unknown-location share, target < 5%)
- [ ] Send via Gmail; idempotent via `Run.emailed_job_ids`; no email when nothing is new
- [ ] **Gate:** 7 consecutive scheduled runs, 0 duplicate emails

## Phase 2 · Startups (weeks 4–5)

- [ ] YC directory ingestion (Algolia; key read fresh each run; hiring, recent batches, 10–500 people, active)
- [ ] VC portfolio ingestion: a16z, Sequoia, Benchmark, Founders Fund, Accel, Lightspeed
- [ ] YC job listings adapter (salary, experience, visa fields)
- [ ] Startup companies tracked even with zero openings; visa label on roles
- [ ] Migrations: Signal, Outreach
- [ ] Signal detection: funding round, new batch, launch, first engineering post
- [ ] Take-your-shot scorer: 3–5 per run, max 2 per niche, 60-day cool-off
- [ ] Research agent: brief + Gmail draft (10-step limit, public contacts only, never sends)
- [ ] Reply-command parser: `draft N`, `skip N reason`, `snooze N`, `confirm N tier`
- [ ] Outreach tracker: drafted → sent → followed_up → replied; one follow-up after 7 days; 5 drafts/day cap
- [ ] Digest sections: Hot signals, Open roles, Take your shot, Outreach tracker
- [ ] **Gate:** first cold email sent from an agent draft

## Phase 3 · Agents + hardening (weeks 6–8)

- [ ] Discovery / self-heal agent: tool loop, 15-step limit, `validate_feed` before any config goes live
- [ ] Health checks: job count vs 7-day average, response schema hash
- [ ] Google adapter (embedded JSON, 1,000-result cap per query)
- [ ] Apple adapter
- [ ] Uber adapter (endpoint found by the discovery agent)
- [ ] Meta via job-alert emails parsed from Gmail
- [ ] Workday adapter
- [ ] Run-failure alert email; schedule-paused alert
- [ ] Cost per run logged
  - Per call done: `core/llm.py` (tokens, cached, latency, cost; prices in `core/llm_prices.py`); `LlmLedger` to total into `Run.cost_usd`
- [ ] **Gate:** one broken source repaired automatically

## v2

- [ ] Other tier: Fortune 500 India engineering centres + remaining startups, once a day
- [ ] Learned preference weights from replies
- [ ] Tracker sheet export (Company + Outreach)
- [ ] Unreviewed companies: confirm tier by reply
- [ ] Public write-up + demo in README
