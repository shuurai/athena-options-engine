# Athena Options Engine

Windows-first options engine. One strategy: News Front-Running. Develop on a Mac. Run on the Windows trading box.

Proposal and task list: [docs/athena/README.md](docs/athena/README.md), [docs/athena/10-implementation-tracker.md](docs/athena/10-implementation-tracker.md).

Screen mockups in `design/` are reference only. They are not this app.

## Run on a Mac

Requires [uv](https://docs.astral.sh/uv/) and local Postgres. The workspace pins Python 3.12. uv downloads it if needed.

Start Postgres with the same user, password, and database as [`.env.example`](.env.example):

```bash
docker run -d --name athena-pg \
  -e POSTGRES_USER=athena \
  -e POSTGRES_PASSWORD=athena \
  -e POSTGRES_DB=athena \
  -p 55432:5432 \
  postgres:16
```

Then:

```bash
cp .env.example .env
uv sync
uv run athena-orchestrator
```

The process logs `state=INIT`, applies migrations, seeds Settings version 1 (Standard profile) if none is active, logs `state=IDLE` and a health line, then sleeps for the Settings cycle (default 5 minutes). It does not send orders.

One pass, for a check:

```bash
ATHENA_LOOP_CYCLES=1 ATHENA_LOOP_SECONDS=1 uv run athena-orchestrator
```

Copy [`.env.example`](.env.example) to `.env` on the machine that will run Athena. Do not commit `.env`.

`ATHENA_TRADING_MODE=paper` uses `ATHENA_IB_PAPER_ACCOUNT_ID`. `live` uses each id in `ATHENA_IB_ACCOUNT_IDS`. Both modes send a real IB order after Approve. Each account keeps its own drafts, orders, fills, and settings. Set `ATHENA_CONTROL_PORT` to serve the Approve page. `ATHENA_TUNNEL=cloudflared` publishes it on a public `trycloudflare.com` URL and logs `control_url=`.
