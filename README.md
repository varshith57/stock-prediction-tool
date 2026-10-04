# stockapp

Personal weekly decision-support app for Nifty 500 stocks: a quality-gated data lake, calibrated
signal models held to a 90% bar (with "no signal" when it isn't met), a portfolio page, and an
honest monthly audit.

**This is a personal research tool, not investment advice.** It places no orders and never logs in
to a broker.

- What and why: [docs/PRD.md](docs/PRD.md); build order and agreed amendments: [docs/PLAN.md](docs/PLAN.md)
- Accounts and secrets: [docs/SETUP.md](docs/SETUP.md)

## Quick start

```bash
uv sync                      # Python 3.12 + dependencies
cp .env.example .env         # then fill in values (docs/SETUP.md)
docker compose up -d db      # local Postgres
uv run stockapp migrate      # create tables, sync the source registry
uv run stockapp calendar refresh
uv run stockapp ingest nse-udiff --start 2026-09-29 --end 2026-10-01
uv run pytest                # tests (live ones skip without secrets)
uv run stockapp config       # show product config
uv run stockapp telegram-test
```

## Status

| Milestone | State |
|---|---|
| M0 Setup | Done |
| M1 Data platform | Done: lake, app DB schema, source registry, connector framework, NSE calendar, NSE daily prices (current format) |
| M2 History backfill | In progress: connectors, backfill, universe and coverage built; full 2016+ load running |
