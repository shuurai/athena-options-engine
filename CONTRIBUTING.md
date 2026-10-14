# Contributing

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
cp .env.example .env
uv sync --group dev
```

Optional local Postgres (same defaults as `.env.example`):

```bash
docker run -d --name athena-pg \
  -e POSTGRES_USER=athena \
  -e POSTGRES_PASSWORD=athena \
  -e POSTGRES_DB=athena \
  -p 55432:5432 \
  postgres:16
```

## Checks before a PR

Pure unit tests (no IB, no database):

```bash
uv run --package athena-orchestrator python -m unittest apps.orchestrator.tests.test_rules -q
```

Compile and import the orchestrator entrypoint:

```bash
uv run python -m compileall -q apps packages
uv run python -c "from athena_orchestrator import main; assert callable(main)"
```

Changelog ledger (required when you add a `logs/` entry):

```bash
node scripts/check-changelog.mjs
```

## Guidelines

- Do not commit `.env`, tokens, or personal broker account ids.
- Prefer fixture accounts such as `ZZTEST`, `PAPER1`, and `U1` in docs and tests.
- Keep changes focused; update `CHANGELOG.md` and `logs/YYYY-MM-DD.md` for
  behavior changes that belong in the project ledger.
