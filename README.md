# Job Finding Agent

A twice-daily agent that crawls careers pages at Tier 1 companies and top startups, ranks new roles against my CV, and emails one digest with direct apply links and cold-email drafts ready to review.

**Status:** Phase 0 · Validate (design done, build not started)

| Doc | What's in it |
| --- | --- |
| [High-level design](docs/HLD.md) | Goals, architecture, tiers, adapters, agents, data model, stack, milestones |
| [TODO](TODO.md) | Every task by phase, with the gate that ends each phase |

## Architecture

A deterministic pipeline, with LLM agents (purple) only where the next step can't be known in advance.

```mermaid
flowchart TD
    REG["Company registry<br/><small>~50 Tier 1, YC, VC lists</small>"] --> SCH["Scheduler + fan-out<br/><small>GitHub Actions cron, asyncio</small>"]
    DISC["Discovery agent<br/><small>Finds and repairs feeds</small>"] -- saves configs --> REG

    SCH --> PA["Platform adapters<br/><small>Greenhouse, Lever, Ashby, Eightfold</small>"]
    SCH --> CA["Custom adapters<br/><small>Amazon, Google, Apple, Uber</small>"]
    SCH --> EA["Email alerts + YC<br/><small>Meta alerts, YC listings</small>"]

    PA --> NORM
    CA --> NORM
    EA --> NORM
    PA -. health check fails .-> DISC
    CA -. health check fails .-> DISC

    NORM["Normalize + filter<br/><small>Dedupe, location, level</small>"] --> RANK["LLM ranker<br/><small>Score, fit, one-line reason</small>"]
    RANK --> DB[("Postgres store<br/><small>Companies, jobs, signals</small>")]
    RANK --> DIG["Digest email<br/><small>Tiered, capped</small>"]
    DB --> SUG["Take-your-shot agent<br/><small>3–5 startups per run</small>"]
    SUG --> DIG
    RES["Research agent<br/><small>Briefs + Gmail drafts</small>"] --> DIG
    DIG --> ME["My inbox<br/><small>Reply commands</small>"]
    ME -- next run --> SUG
    ME -- next run --> RES

    classDef agent fill:#EEEDFE,stroke:#534AB7,color:#26215C
    classDef inbox fill:#E1F5EE,stroke:#0F6E56,color:#04342C
    class DISC,RANK,SUG,RES agent
    class ME inbox
```

## Three tiers

| Tier | Companies | Locations |
| --- | --- | --- |
| Tier 1 | ~50 hand-picked (FAANG+, big tech, infra, fintech, AI labs, Indian top product) | India + Europe hubs |
| Tier 1 Startups | YC + top VC portfolios, tracked even with no openings (cold outreach) | Anywhere |
| Other | Fortune 500 India engineering centres + remaining startups | India |

## Stack

Python 3.12 · httpx · Pydantic · Playwright · PostgreSQL (Neon free tier) · OpenAI API · Gmail API · GitHub Actions (cron). Hosting cost: $0.

## Milestones

```mermaid
flowchart LR
    P0["Phase 0 · Validate<br/>Week 1"] -->|"useful 5 of 7 days"| P1["Phase 1 · MVP<br/>Weeks 2–3"]
    P1 -->|"7 runs, 0 duplicates"| P2["Phase 2 · Startups<br/>Weeks 4–5"]
    P2 -->|"first cold email sent"| P3["Phase 3 · Agents<br/>Weeks 6–8"]
    P3 -->|"1 source auto-repaired"| V2["v2"]
```

## License

See [LICENSE](LICENSE).
