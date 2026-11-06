# TODO

Tick a box (`- [ ]` → `- [x]`) when a task is done. Each phase ends with a **gate**: don't start the next phase until the gate passes.
Design: [docs/HLD.md](docs/HLD.md)

**Current phase:** Phase 0 · Validate

---

## Open decisions

- [ ] Confirm the ~50 Tier 1 companies (incl. whether Flipkart, PhonePe, Razorpay, CRED, Swiggy, Zomato, Meesho count as Tier 1)
- [ ] Add the current CV as the ranking source of truth (stored as a GitHub secret, never committed)
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
- [ ] Python 3.12 skeleton: `core/`, `adapters/`, `pipeline/`, `agents/`, `digest/`, `jobs/`, `tests/`
- [ ] `pyproject.toml`, ruff, pytest, pre-commit
- [ ] Neon Postgres free project; `DATABASE_URL` secret
- [ ] Alembic migrations: Company, Source, Job, Ranking, Run
- [ ] OpenAI key as `OPENAI_API_KEY` secret + monthly budget cap in the OpenAI dashboard
- [ ] Gmail API OAuth (send, compose drafts, read) → `GMAIL_TOKEN` secret
- [ ] `.github/workflows/run.yml`: cron `30 2,12 * * *` + `workflow_dispatch`
- [ ] `.github/workflows/keepalive.yml`: weekly, so the schedule never pauses
- [ ] Logging rule: counts only, never job data, CV or email content (Actions logs are public)

**Registry**
- [ ] `companies.yaml` seed: ~50 Tier 1 with careers URL, platform, India/Europe presence, `us_transfer_path`
- [ ] Loader that upserts the registry into the DB

**Adapters**
- [ ] `JobSource` protocol + `SearchSpec`, `RawJob`, `JobDetail`, `HealthReport` models
- [ ] Greenhouse adapter
- [ ] Lever adapter
- [ ] Ashby adapter
- [ ] Eightfold adapter (Microsoft, Netflix): `pcsx/search` + `pcsx/position_details`
- [ ] Amazon adapter (`search.json`, split by category to stay under the 10,000 cap)
- [ ] Contract tests per adapter against recorded responses
- [ ] Incremental crawl: stop paging at the first job ID already stored
- [ ] Per-domain rate limit, backoff retries, dead-letter table

**Pipeline**
- [ ] Normalize: location → ISO country code; title cleanup
- [ ] Dedupe upsert on (company, source_job_id); `first_seen` / `last_seen`
- [ ] Rule filter: India + Europe hubs (incl. Copenhagen), title keywords, exclusions (Staff, Principal, Manager, Intern, Contract), 4–6 YOE
- [ ] LLM ranker with Structured Outputs (score, fit, reason) + rule-score fallback
- [ ] Mark jobs stale after 3 missed runs

**Digest**
- [ ] HTML template: Tier 1 India (all), Tier 1 Europe (top 10)
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
- [ ] **Gate:** one broken source repaired automatically

## v2

- [ ] Other tier: Fortune 500 India engineering centres + remaining startups, once a day
- [ ] Learned preference weights from replies
- [ ] Tracker sheet export (Company + Outreach)
- [ ] Unreviewed companies: confirm tier by reply
- [ ] Public write-up + demo in README
