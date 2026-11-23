#!/usr/bin/env python3
"""R6 outbound dead-man watchdog (booklet 14, section 22; D95-D101).

Runs on the dedicated Mac mini as an independent launchd job every 300s. It is
deliberately **stdlib-only** (D99): it must never import ``athena_*`` packages,
because that would drag bots/exec/store/strategy into the deployment unit and
break the "zero new workspace package / zero uv.lock churn" constraint.

Layering (section 22.4):

* pure core (zero IO): ``is_us_session`` / ``decide`` / ``WatchState`` / ``Plan``
  -- an exact copy of the ET session rule (D99 parity test), a 10-minute grace
  (D65/D97) and down/recovered de-duplication.
* thin IO: ``check_in`` (outbound HTTP check-in, D95/D96), ``load_state`` /
  ``save_state`` (local ``var/`` JSON, D97), ``send_once`` (env-only Telegram
  ``sendMessage``, D97/D100).
* send adapter: ``send_once`` is the env-only mirror of D71's
  ``notify_guardian_once()`` -- same ``TELEGRAM_BOT_TOKEN`` / allowlist /
  ``api.telegram.org`` endpoint, **no import of athena_bots**, one push per
  allowlisted chat, no markup, no inline Resolve, not fed into the 5-min nag.
* launchd lifecycle: the sibling ``*.plist`` files (D98).

Honest boundaries: no DB, no ``guardian_alerts``, no ``cycle_progress`` write,
no tunnel change, no inbound port, no second notification channel, no market
data source, no migration. US market holidays are NOT filtered (section 14.5).

Python 3.9 compatible (macOS /usr/bin/python3). Never contains a bot token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from zoneinfo import ZoneInfo

# --- pure core: exact copy of athena_orchestrator.guardian_schedule (D99) ----
ET = ZoneInfo("America/New_York")
US_OPEN = time(9, 30)
US_CLOSE = time(16, 0)

GRACE = timedelta(minutes=10)          # D65 / D97: silence >= 10 minutes
CHECKIN_INTERVAL = 300                 # D95 / D96: one outbound check-in / 5 min
CHECKIN_TIMEOUT = 10
TELEGRAM_TIMEOUT = 35
DEFAULT_STATE = "var/athena-watchdog-state.json"
DOWN_TAG = "Guardian host DOWN"
RECOVERED_TAG = "Guardian host RECOVERED"


def is_us_weekday(day) -> bool:
    """Mon-Fri in ET. US market holidays are NOT filtered (section 14.5)."""
    return day.weekday() < 5


def is_us_session(now: datetime) -> bool:
    """ET 09:30-16:00 on a weekday, DST-safe, zero I/O.

    ``now`` must be timezone-aware; naive datetimes are rejected rather than
    silently read as local wall clock. US market holidays remain an honest gap
    (section 14.5): a holiday is treated as an ordinary weekday.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("is_us_session needs an aware datetime")
    local = now.astimezone(ET)
    if not is_us_weekday(local.date()):
        return False
    return US_OPEN <= local.time() < US_CLOSE


@dataclass(frozen=True)
class WatchState:
    """D97 de-dup state, persisted in a local ``var/`` JSON file."""

    last_checkin: str | None = None   # ISO-8601 UTC of last successful check-in
    down_sent: bool = False
    recovered_sent: bool = False


@dataclass(frozen=True)
class Plan:
    """Pure decision: which one-shot messages to send, and the next state."""

    messages: tuple = field(default_factory=tuple)
    state: WatchState = field(default_factory=WatchState)
    reason: str = ""


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(raw: str) -> datetime:
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("watchdog timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc)


def _session_open_utc(moment: datetime) -> datetime:
    """09:30 ET of ``moment``'s ET trading day, in UTC. DST-safe.

    D65/D97 "US 时段外静默不告警、也不累计": only silence INSIDE the current
    session counts. A box that is off all weekend must not fire a down on
    Monday 09:31 just because ``last_checkin`` is Friday's close.
    """
    et_day = moment.astimezone(ET).date()
    return datetime.combine(et_day, US_OPEN, tzinfo=ET).astimezone(timezone.utc)


def down_text(now: datetime, silent_seconds: float) -> str:
    """One-shot, non-ackable fact notice. Never carries inline Resolve (D97)."""
    minutes = int(silent_seconds // 60)
    stamp = now.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    return (
        f"{DOWN_TAG}: no check-in for {minutes} min during the US session "
        f"(as of {stamp}). Outbound dead-man backfill; this is a fact notice, "
        "not an ack alert -- no /guard action needed."
    )


def recovered_text(now: datetime) -> str:
    stamp = now.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    return (
        f"{RECOVERED_TAG}: check-ins resumed at {stamp}. "
        "No /guard action needed."
    )


def decide(state: WatchState, now: datetime, checkin_ok: bool) -> Plan:
    """Pure, zero IO. Returns the one-shot messages and the next state.

    * Outside the US session: no alert AND no accumulation (D65/D97) -- the
      baseline is refreshed so an overnight/weekend gap cannot fire at the open.
    * First run: baseline only.
    * In-session silence >= 10 min: exactly one ``down`` (a late backfill on
      restart; the real-time down is the out-of-box dead-man service, D95).
    * De-dup persists across process restarts via ``var/`` (D97), so a reboot
      cannot re-bombard the operator.
    * Back to a healthy interval: exactly one ``recovered``, then re-arm.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("decide needs an aware datetime")
    moment = now.astimezone(timezone.utc)

    if not is_us_session(moment):
        # No alert and no accumulation outside US hours.
        return Plan((), WatchState(_iso(moment), False, False),
                    "outside_us_session_no_accumulation")

    if state.last_checkin is None:
        return Plan((), WatchState(_iso(moment), False, False), "baseline")

    gap = moment - max(_parse(state.last_checkin), _session_open_utc(moment))
    gap = max(gap, timedelta(0)).total_seconds()
    new_last = _iso(moment) if checkin_ok else state.last_checkin

    if gap >= GRACE.total_seconds():
        if not state.down_sent:
            # First backfill after a restart/silence: exactly one down.
            return Plan((down_text(moment, gap),),
                        WatchState(new_last, True, False), "silence_down")
        if checkin_ok:
            # Still a long gap but the very first successful check-in after a
            # down means the box is back -- emit recovered without waiting a
            # whole extra 300 s tick.
            return Plan((recovered_text(moment),),
                        WatchState(new_last, False, False),
                        "recovered_after_silence")
        return Plan((), WatchState(new_last, True, state.recovered_sent),
                    "silence_dedup")

    # Steady state (gap < grace): one recovered after a reported down, then
    # re-arm. A still-failing check-in must NOT re-arm, or a flapping box could
    # emit a second down later in the same incident.
    if state.down_sent and not state.recovered_sent:
        if checkin_ok:
            return Plan((recovered_text(moment),),
                        WatchState(new_last, False, False), "recovered")
        return Plan((), WatchState(new_last, True, False),
                    "checkin_still_failing")
    return Plan((), WatchState(new_last, False, False), "healthy")


# --- thin IO -----------------------------------------------------------------
def load_state(path) -> WatchState:
    """Tolerate a missing or corrupt file: start from a clean baseline."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WatchState()
    if not isinstance(raw, dict):
        return WatchState()
    last = raw.get("last_checkin")
    if not isinstance(last, str):
        last = None
    else:
        try:
            _parse(last)
        except (TypeError, ValueError):
            last = None
    return WatchState(
        last_checkin=last,
        down_sent=bool(raw.get("down_sent", False)),
        recovered_sent=bool(raw.get("recovered_sent", False)),
    )


def save_state(path, state: WatchState) -> None:
    """Atomic replace, so a crash mid-write cannot corrupt the de-dup state."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=False, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, target)


def check_in(url: str, timeout: int = CHECKIN_TIMEOUT, opener=None) -> bool:
    """Outbound-only HTTP check-in (D95). Zero inbound ports, zero tunnel change.

    The URL is an operator-configured dead-man service endpoint. A transient
    failure is a legitimate silence signal, not an exception to propagate.
    """
    clean = (url or "").strip()
    if not clean:
        return False
    op = opener or urlopen
    request = Request(clean, headers={"User-Agent": "athena-watchdog/1.0"})
    try:
        with op(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            return 200 <= int(status) < 300
    except Exception as exc:                       # noqa: BLE001 - watchdog must not die
        print(f"watchdog check-in failed: {exc}", file=sys.stderr)
        return False


def send_once(text: str, env=None, opener=None) -> int:
    """Env-only mirror of D71 ``notify_guardian_once()`` (stdlib-only, D99).

    Uses the SAME existing Telegram channel (bot token + allowlist + optional
    HTTPS proxy) as ``athena_bots.telegram._live_send()``'s env fallback, so it
    is not a second channel (D100). One ``sendMessage`` per allowlisted chat,
    no ``reply_markup`` (D97: no inline Resolve), no nag, no DB, no store.
    Returns the number of delivered chats.
    """
    source = env if env is not None else os.environ
    token = (source.get("TELEGRAM_BOT_TOKEN") or "").strip()
    allow = {part.strip() for part in
             (source.get("TELEGRAM_ALLOWLIST_USER_IDS") or "").split(",")
             if part.strip()}
    if not token or not allow:
        return 0
    proxy = (source.get("TELEGRAM_HTTPS_PROXY") or "").strip()
    if opener is not None:
        op = opener
    elif proxy:
        op = build_opener(ProxyHandler({"http": proxy, "https": proxy})).open
    else:
        op = urlopen
    sent = 0
    for chat_id in sorted(allow):
        payload = {"chat_id": chat_id, "text": text}
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with op(request, timeout=TELEGRAM_TIMEOUT) as response:
                body = json.loads(response.read().decode("utf-8"))
            if body.get("ok"):
                sent += 1
        except Exception as exc:                   # noqa: BLE001 - one chat must not block others
            print(f"watchdog send failed chat={chat_id}: {exc}", file=sys.stderr)
    return sent


def load_env_file(path) -> None:
    """Stdlib mirror of ``athena_orchestrator.load_env_file`` (setdefault)."""
    target = Path(path)
    if not target.is_file():
        return
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


# --- entrypoint --------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="R6 outbound dead-man watchdog")
    parser.add_argument("--state", default=None, help="var/ de-dup JSON path")
    parser.add_argument("--env-file", default=".env", help="operator .env path")
    parser.add_argument("--now", default=None, help="aware ISO-8601 override (tests)")
    parser.add_argument("--checkin-url", default=None, help="override check-in URL")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute the plan without network or disk writes")
    args = parser.parse_args(argv)

    load_env_file(args.env_file)
    state_path = Path(args.state or os.environ.get(
        "ATHENA_WATCHDOG_STATE", DEFAULT_STATE))
    now = _parse(args.now) if args.now else datetime.now(timezone.utc)
    url = args.checkin_url if args.checkin_url is not None else os.environ.get(
        "ATHENA_WATCHDOG_CHECKIN_URL", "")

    state = load_state(state_path)
    if args.dry_run:
        checkin_ok = True
    elif (url or "").strip():
        checkin_ok = check_in(url)
    else:
        # No external service configured: local-only backfill mode. The script
        # is alive, so it still detects a restart gap and emits backfill/recovered.
        checkin_ok = True
        print("watchdog: no check-in URL configured; local-only backfill mode",
              file=sys.stderr)

    plan = decide(state, now, checkin_ok)
    sent = 0
    if not args.dry_run:
        for text in plan.messages:
            sent += send_once(text)
        save_state(state_path, plan.state)

    print(json.dumps({
        "at": _iso(now),
        "reason": plan.reason,
        "checkin_ok": checkin_ok,
        "messages": len(plan.messages),
        "sent": sent,
        "down_sent": plan.state.down_sent,
        "recovered_sent": plan.state.recovered_sent,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
