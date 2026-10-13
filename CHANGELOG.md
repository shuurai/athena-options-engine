# CHANGELOG

Index of development changes. One row per change: date ｜ short description ｜ link.
Details live in `logs/YYYY-MM-DD.md`. Conventions and the entry template: [logs/README.md](logs/README.md).
Machine check: `node scripts/check-changelog.mjs` (index ↔ log must close both ways).

| 日期 | 描述 | 链接 |
| --- | --- | --- |
| 2026-10-10 | Add DeepSeek strategy review for the signaled entry path | [C-20261010-1](logs/2026-10-10.md#c-20261010-1) |
| 2026-10-10 | Fix changelog word count for space-separated ASCII | [C-20261010-2](logs/2026-10-10.md#c-20261010-2) |
| 2026-10-10 | Add the seven-document Athena plan set and decision list | [C-20261010-3](logs/2026-10-10.md#c-20261010-3) |
| 2026-10-10 | Add portfolio diagnostics plan rejecting MPT optimisation | [C-20261010-4](logs/2026-10-10.md#c-20261010-4) |
| 2026-10-10 | Add step 9 risk-control UI plan; falsify straddle IV fix; split D4 | [C-20261010-5](logs/2026-10-10.md#c-20261010-5) |
| 2026-10-10 | Add strike-band calibration plan from strangle width practice | [C-20261010-6](logs/2026-10-10.md#c-20261010-6) |
| 2026-10-10 | 落地 D36 D37 D38 D39 裁定并提前 4b 采集 | [C-20261010-7](logs/2026-10-10.md#c-20261010-7) |
| 2026-10-11 | Draft a two-leg strangle and close the plan 7 fixtures | [C-20261011-1](logs/2026-10-11.md#c-20261011-1) |
| 2026-10-11 | Add cycle budget checks and a read-only book page | [C-20261011-2](logs/2026-10-11.md#c-20261011-2) |
| 2026-10-12 | Close strategy holes for marks, rails, openings, and option spreads | [C-20261012-1](logs/2026-10-12.md#c-20261012-1) |
| 2026-10-12 | Read the US session as standard time from 2 Nov 2026 | [C-20261012-2](logs/2026-10-12.md#c-20261012-2) |
| 2026-10-13 | Cap IB quotes, skip a closed market, and stop after empty option history | [C-20261013-1](logs/2026-10-13.md#c-20261013-1) |

## Notes

- **Ledger history starts 2026-10-10.** Nothing before this date is indexed; the three commits on
  `main` predate it and are not back-filled.
- **Uncommitted working-tree changes are not covered by any entry here.** At the time this ledger was
  created, `git status` showed two tracked files modified with no index row and no commit to reference:
  `apps/orchestrator/src/athena_orchestrator/console.py` and
  `apps/orchestrator/src/athena_orchestrator/pages.py`. They were **not** authored in the same session
  as this ledger and their intent is unknown. Until an entry exists for them, their revert path is
  `git checkout -- <file>`. This is the case the ledger exists to make visible: `git log` cannot see it.
- **Two further unindexed changes appeared on 2026-10-10 at 22:28:52 / 22:28:54** (after this ledger's
  earlier entries were written, and **not** authored by the assistant — the assistant's work on this
  date touched `docs/` and this ledger only): `apps/exec-ib/src/athena_exec/broker.py` (+2 lines,
  `ib.RequestTimeout = 30` with the comment *"0 means wait forever. A hung quote must not pin the probe
  lock."*) and `apps/orchestrator/src/athena_orchestrator/tasks.py` (+3 lines, wrapping
  `_pull_ib`'s quote calls in `except Exception` and returning `None, [], "IB quote timed out."`).
  The pair reads as one targeted fix for a live hang: `var/athena.log` records
  `reqHistoricalData: Timeout for Stock(conId=323608988, symbol='1810', exchange='SEHK' ...)`, and the
  regenerated `__pycache__` timestamps show the edited modules were executed afterwards. **Still
  unindexed** — no entry, no commit. Revert path: `git checkout -- <file>`.
