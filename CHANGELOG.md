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
| 2026-10-13 | Stream live cycle lines in the cockpit header | [C-20261013-2](logs/2026-10-13.md#c-20261013-2) |
| 2026-10-13 | Run every live account and switch the viewed book | [C-20261013-3](logs/2026-10-13.md#c-20261013-3) |
| 2026-10-13 | Show blocked drafts and count only the waiting queue | [C-20261013-4](logs/2026-10-13.md#c-20261013-4) |
| 2026-10-13 | Show budget results as two short badges | [C-20261013-5](logs/2026-10-13.md#c-20261013-5) |
| 2026-10-13 | Load blocked drafts as the list scrolls | [C-20261013-6](logs/2026-10-13.md#c-20261013-6) |
| 2026-10-13 | List blocked drafts with the other decisions | [C-20261013-7](logs/2026-10-13.md#c-20261013-7) |
| 2026-10-13 | Filter recent decisions by status and text | [C-20261013-8](logs/2026-10-13.md#c-20261013-8) |
| 2026-10-13 | Filter history like drafts and stop drafting rolls | [C-20261013-9](logs/2026-10-13.md#c-20261013-9) |
| 2026-10-13 | Move Accounts into View and add an account switcher | [C-20261013-10](logs/2026-10-13.md#c-20261013-10) |
| 2026-10-13 | Move Accounts and paper-only Simulation into Configs | [C-20261013-11](logs/2026-10-13.md#c-20261013-11) |
| 2026-10-13 | Store IB portfolio marks and pulse the enabled universe | [C-20261013-12](logs/2026-10-13.md#c-20261013-12) |
| 2026-10-13 | Cycle only each account's enabled universe | [C-20261013-13](logs/2026-10-13.md#c-20261013-13) |
| 2026-10-13 | Document four risk grades before changing the form | [C-20261013-14](logs/2026-10-13.md#c-20261013-14) |
| 2026-10-13 | Fill each risk grade from the appetite template | [C-20261013-15](logs/2026-10-13.md#c-20261013-15) |
| 2026-10-13 | Add a reset card for the selected risk grade | [C-20261013-16](logs/2026-10-13.md#c-20261013-16) |
| 2026-10-13 | Mark the console version 0.2.0 | [C-20261013-17](logs/2026-10-13.md#c-20261013-17) |
| 2026-10-13 | Mark the console version 1.0.0 | [C-20261013-18](logs/2026-10-13.md#c-20261013-18) |
| 2026-10-13 | Add a Universe trade panel that sends a stock order | [C-20261013-19](logs/2026-10-13.md#c-20261013-19) |
| 2026-10-13 | Show a spinner while a universe row saves | [C-20261013-20](logs/2026-10-13.md#c-20261013-20) |
| 2026-10-13 | Add KULR and MRAM to the universe seed | [C-20261013-21](logs/2026-10-13.md#c-20261013-21) |
| 2026-10-13 | Price an unmarked stock from its stored close | [C-20261013-22](logs/2026-10-13.md#c-20261013-22) |
| 2026-10-13 | Move the audit refresh onto the table | [C-20261013-23](logs/2026-10-13.md#c-20261013-23) |
| 2026-10-13 | Cycle each name only when its toggle is on | [C-20261013-24](logs/2026-10-13.md#c-20261013-24) |
| 2026-10-13 | Check a name with the tactic union | [C-20261013-25](logs/2026-10-13.md#c-20261013-25) |
| 2026-10-13 | Check each tactic only on its account | [C-20261013-26](logs/2026-10-13.md#c-20261013-26) |

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
