# macOS launchd runbook — R6 outbound dead-man watchdog

Booklet 14 [§22](../../docs/plan/14-guardian-escalation.md) · ratified **D95–D101**
(12th round, 2026-11-23, `C-20261123-2`) · delivered 2026-11-24 (§23).

This directory is a **deployment unit, not a workspace package**. It adds zero
Python packages, zero `uv.lock` churn, zero migrations, and zero changes to
runtime code (D99). Everything runs on the dedicated Mac mini decided in D64.

---

## 1. What this delivers

| Decision | What lands here |
|---|---|
| **D95** | An **outbound** dead-man check-in. `com.zeptun.athena.watchdog.plist` runs `athena_watchdog.py` every 300 s; the script sends one HTTP request to an operator-configured external dead-man service. No inbound port, no tunnel change, no second observer machine. If the whole box loses power, the *external service* sees the silence and alerts. |
| **D96** | The check-in is an **independent launchd job**, not a hook inside `nag_tick()`. R4's `cycle_progress.updated_at` stays the in-process heartbeat. The two signals are parallel and never substitute for each other. |
| **D97** | Exactly **one `down`** and **one `recovered`** one-shot Telegram push, gated by `is_us_session()` (ET 09:30–16:00, DST-safe, weekdays only) + a **10-minute grace**. No 5-min nag, no `guardian_alerts` row, no inline Resolve, no second ack state machine. De-dup state lives in `var/athena-watchdog-state.json`. |
| **D98** | `launchd` `KeepAlive` / `RunAtLoad` for **Postgres + `athena-orchestrator`**; an optional, **disabled-by-default** cloudflared job. IB Gateway 2FA is a one-time **manual** operator step (§4). |
| **D99** | `athena_watchdog.py` is **stdlib-only** (urllib / zoneinfo / json / dataclasses). It runs under macOS `/usr/bin/python3` 3.9, so it does not need `uv`, the venv, or any `athena_*` package. The ET rule is copied verbatim and locked by a parity test against `guardian_schedule.is_us_session()`. |
| **D100** | The §6 carve-out: **outbound** check-in only; alerts use the **existing** Telegram channel; **no new notification channel**, no Feishu, no Tailscale, Cloudflare quick-tunnel behaviour unchanged. Bot token is **env-only, never in this repo**. |
| **D101** | Mac mini uses a **different `IB_CLIENT_ID` base** in its own `.env` (§3). Zero code change; `apps/exec-ib/src/athena_exec/clients.py` per-role offsets (`0/1/2/3/20`) are reused as-is. |

### Topology correction (honest, per §22.2 rows 11–12)

`athena-orchestrator` is the **only** console script. `athena-exec` has no
`[project.scripts]` entry; `pulse.py` `_refresh_loop`, the R3 scheduler, the R4
nag daemon, R5 trigger evaluation, Telegram polling, the console/control HTTP
servers and `tunnel.py:cloudflared` are all **threads / children of that one
process**. So the three active plists below cover the whole runtime. The
00-booklet §6 R6 row's old "exec-ib as an independent process" wording is a
measurement-level correction; D64's original text is unchanged.

---

## 2. Files

| File | Loaded by default | Purpose |
|---|---|---|
| `athena_watchdog.py` | — | stdlib-only check-in + pure decision core + env-only `sendMessage` |
| `com.zeptun.athena.watchdog.plist` | ✅ | `StartInterval=300`, `RunAtLoad=true` — independent outbound check-in (D96) |
| `com.zeptun.athena.orchestrator.plist` | ✅ | `KeepAlive=true`, `RunAtLoad=true` — supervises the single process (D98) |
| `com.zeptun.athena.postgres.plist` | ✅ | `KeepAlive=true`, `RunAtLoad=true` — `docker start -a athena-pg` (D98) |
| `com.zeptun.athena.cloudflared.plist` | ❌ | Optional standalone tunnel template; `RunAtLoad=false`, `KeepAlive=false`, `Disabled=true` |

`__ATHENA_ROOT__`, `__UV_BIN__`, `__DOCKER_BIN__`, `__CLOUDFLARED_BIN__` are
**placeholders**, not real paths. Substitute them at install time (§3). No
machine-specific absolute path and **no credential** is committed here.

---

## 3. Install (one time, on the Mac mini)

```bash
# 0) prerequisites
#    - Docker Desktop in Login Items (launchd cannot boot the daemon)
#    - `athena-pg` container already created per README "How to run" step 1
#    - IB Gateway installed, API enabled, 2FA logged in manually (§4)
#    - `uv sync` already run in the repo

# 1) resolve binaries and root
ROOT="$PWD"                       # repo root, e.g. /Users/<you>/zeptun-ai-v4
UV_BIN="$(command -v uv)"
DOCKER_BIN="$(command -v docker)"
CLOUDFLARED_BIN="$(command -v cloudflared || echo /usr/local/bin/cloudflared)"

# 2) render plists into ~/Library/LaunchAgents (never edit the tracked copies)
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$ROOT/var"
for f in com.zeptun.athena.watchdog com.zeptun.athena.orchestrator \
         com.zeptun.athena.postgres com.zeptun.athena.cloudflared; do
  sed -e "s|__ATHENA_ROOT__|$ROOT|g" \
      -e "s|__UV_BIN__|$UV_BIN|g" \
      -e "s|__DOCKER_BIN__|$DOCKER_BIN|g" \
      -e "s|__CLOUDFLARED_BIN__|$CLOUDFLARED_BIN|g" \
      "deploy/macos/$f.plist" > "$AGENTS/$f.plist"
done

# 3) operator-local .env (gitignored; NEVER commit). Reuse the EXISTING bot.
cat >> .env <<'EOF'
# --- R6 outbound dead-man (D95/D100/D101) ---
# External dead-man service check-in URL (e.g. a healthchecks.io ping URL).
# This URL may embed a secret: keep it in .env only, never in the repo.
ATHENA_WATCHDOG_CHECKIN_URL=
ATHENA_WATCHDOG_STATE=var/athena-watchdog-state.json
# D101: Mac mini uses a DIFFERENT base from the Windows box (which uses 1).
# clients.py derives session/orders/probe/pulse/telegram = base+0/1/2/3/20.
IB_CLIENT_ID=11
TELEGRAM_BOT_TOKEN=<same bot as the Windows box>
TELEGRAM_ALLOWLIST_USER_IDS=<comma-separated operator ids>
# TELEGRAM_HTTPS_PROXY=            # optional, only if api.telegram.org is proxied
EOF

# 4) load the three active jobs (do NOT load cloudflared, see §5)
launchctl bootstrap gui/$(id -u) "$AGENTS/com.zeptun.athena.postgres.plist"
launchctl bootstrap gui/$(id -u) "$AGENTS/com.zeptun.athena.orchestrator.plist"
launchctl bootstrap gui/$(id -u) "$AGENTS/com.zeptun.athena.watchdog.plist"

# 5) verify
launchctl list | grep com.zeptun.athena
/usr/bin/python3 deploy/macos/athena_watchdog.py --dry-run --now 2026-11-24T14:35:00+00:00
tail -20 var/athena-watchdog.out.log
```

`ATHENA_WATCHDOG_CHECKIN_URL` is the only new network behaviour. It is a single
outbound HTTP GET; nothing listens on an inbound port, and `tunnel.py` is
untouched. Leave it empty to run in **local-only backfill mode** (the script
still detects a restart gap and emits the down/recovered pair, but no external
service can observe a dead box — this does **not** satisfy the §4 unplug test).

> **D100 credential rule.** If the external dead-man service is configured to
> send the Telegram `down` itself, its bot token lives in the **service's**
> operator config / env, never in this repository. Both paths use the *same*
> existing Telegram bot, so this is not a second channel. Do not commit tokens,
> ping URLs, or plist files containing them.

---

## 4. IB Gateway 2FA — one-time manual step (D98, honest gap)

`README.md:124` requires IB Gateway to be installed, API-enabled and **logged in
with 2FA by a human**. That cannot be automated under the current code. The
plists therefore cover **Postgres + `athena-orchestrator` + optional
cloudflared** only. After a reboot:

1. Log into IB Gateway once (2FA).
2. The orchestrator reconnects on its own via `pulse.py`; no shell needed.

"Reboot, everything self-recovers without a shell" is satisfied **for those
three components**. Gateway authentication is explicitly out of scope (no IBC,
no third-party auto-login dependency — Q4(b) was rejected).

---

## 5. Why `cloudflared` is disabled (tunnel behaviour unchanged)

`tunnel.py:20-56` already launches `cloudflared tunnel --url ... --no-autoupdate`
**inside** the orchestrator process and writes a fresh random
`*.trycloudflare.com` URL into `ATHENA_PUBLIC_BASE_URL` on every start. Loading
`com.zeptun.athena.cloudflared.plist` at the same time would open a **second**
quick tunnel with a different URL — a tunnel behaviour change forbidden by §6
and §22.5. It is shipped only as a template for the case where an operator
deliberately sets `ATHENA_TUNNEL=off` and wants a standalone tunnel. Keep it
unloaded by default.

---

## 6. Alert semantics (D97 / D65)

* **US session gate**: `athena_watchdog.py:is_us_session()` is a verbatim copy
  of `guardian_schedule.is_us_session()` — ET 09:30–16:00, Mon–Fri, DST-safe,
  naive datetimes rejected. `apps/orchestrator/tests/test_guardian_r6.py`
  asserts parity over a DST/weekend/boundary matrix.
* **No US market-holiday calendar** (§14.5 honest gap): a holiday is treated as
  an ordinary weekday.
* **Outside US hours**: no alert **and no accumulation** — the baseline is
  refreshed each tick so an overnight/weekend gap cannot fire at the open.
* **In-session silence ≥ 10 min**: one `down` one-shot. On a dead box this is
  necessarily a **late backfill on restart**; the *real-time* down is the
  out-of-box dead-man service (D95), since a machine that is off cannot send.
* **Recovery**: one `recovered` one-shot on the next healthy tick after a
  reported down, then the state re-arms.
* **De-dup**: `var/athena-watchdog-state.json` survives restarts (atomic
  `os.replace`), so a flapping box cannot bombard the operator. Corrupt or
  missing state degrades to a clean baseline rather than crashing.
* Messages carry **no inline Resolve / `reply_markup`**, are never written to
  `guardian_alerts`, and never enter the 5-minute nag (§6 maximum intrusion).

### Send adapter (implementation note, not a new decision)

D97 names `notify_guardian_once()` (D71) as the send adapter, but D99 forbids
importing `athena_bots` (that would drag bots/exec/store/strategy into the
deployment unit). `athena_watchdog.py:send_once()` is therefore the **stdlib,
env-only mirror** of `_live_send()`'s env fallback (`telegram.py:949-957`):
same `TELEGRAM_BOT_TOKEN`, same `TELEGRAM_ALLOWLIST_USER_IDS`, same
`api.telegram.org/bot<token>/sendMessage`, same optional
`TELEGRAM_HTTPS_PROXY`, one push per allowlisted chat, no markup. Same channel,
not a new one (D100). `athena_bots.telegram` itself is unchanged.

---

## 7. Verify / operate / roll back

```bash
# dry-run a decision (no network, no disk write)
/usr/bin/python3 deploy/macos/athena_watchdog.py --dry-run --now 2026-11-24T14:35:00+00:00
/usr/bin/python3 deploy/macos/athena_watchdog.py --dry-run --now 2026-11-24T19:35:00+00:00  # outside session

# parity + boundary tests
uv run python -m unittest apps.orchestrator.tests.test_guardian_r6 -q

# logs
tail -f var/athena-watchdog.out.log var/athena-watchdog.err.log

# unplug test (§4 acceptance): power off the Mac mini during US hours.
#   -> external dead-man service alerts after 10 min of missing check-ins.
#   -> on power restore, launchd RunAtLoad brings Postgres + orchestrator back;
#      the watchdog emits one recovered push. Log in to IB Gateway 2FA once.

# unload / roll back
AGENTS="$HOME/Library/LaunchAgents"
for f in watchdog orchestrator postgres; do
  launchctl bootout gui/$(id -u) "$AGENTS/com.zeptun.athena.$f.plist"
done
rm -f "$AGENTS"/com.zeptun.athena.*.plist
git revert <delivery-sha>     # see §23 for the commit
```

---

## 8. Known gaps (deliberately not closed)

1. **Gateway 2FA** is manual (§4). No IBC / auto-login dependency.
2. **No holiday calendar** (§14.5); holidays behave like weekdays.
3. **The external dead-man service is operator-provisioned.** This repo ships
   the check-in client and its config surface, not the service. With an empty
   `ATHENA_WATCHDOG_CHECKIN_URL`, whole-box death during the outage is not
   observed in real time (only backfilled on restart).
4. **Docker Desktop** must itself be in Login Items; launchd cannot boot it.
5. **`athena-exec` is not a process** (topology correction, §1).
6. No `guardian_alerts` write, no new Settings field, no new migration, no
   new data source, no order/draft/gate, no second channel (§22.8).
