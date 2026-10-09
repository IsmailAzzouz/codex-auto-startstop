#!/usr/bin/env python3
"""Synthetic end-to-end tests for codex-auto-startstop — no real Codex involved.

Covers: threshold decisions, once-per-window dedupe, new-window re-steer,
resume scheduling + dispatch via a fake `codex queue`, activity-based skip,
session-start behavior, and status output.

Run:  python3 tests/synthetic_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "codex_auto_startstop.py"
PY = sys.executable
SID = "01a12233-a8f7-7cc2-842d-e1f79c5028e3"
CWD = "/tmp/project-alpha"
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def rollout_path(sessions: Path) -> Path:
    return sessions / "2026" / "10" / "09" / f"rollout-2026-10-09T21-46-13-{SID}.jsonl"


def write_rollout(
    sessions: Path,
    used: float,
    resets_at: float,
    extra_events=None,
    weekly_used: float = 12.0,
    weekly_resets: float | None = None,
) -> Path:
    p = rollout_path(sessions)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    lines = [
        {
            "timestamp": iso(now - 600),
            "type": "session_meta",
            "payload": {"id": SID, "cwd": CWD, "originator": "codex_tui"},
        },
        {
            "timestamp": iso(now - 500),
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": iso(now - 480),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {},
                "rate_limits": {
                    "primary": {"used_percent": used, "window_minutes": 300, "resets_at": int(resets_at)},
                    "secondary": {
                        "used_percent": weekly_used,
                        "window_minutes": 10080,
                        "resets_at": int(weekly_resets if weekly_resets is not None else now + 500000),
                    },
                    "plan_type": "plus",
                    "rate_limit_reached_type": None,
                },
            },
        },
        {
            "timestamp": iso(now - 400),
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        },
    ]
    lines.extend(extra_events or [])
    p.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return p


def make_sandbox(tmp: Path):
    home = tmp / "codex_home"
    sessions = home / "sessions"
    fake_log = tmp / "fake_codex.log"
    fake = tmp / "fake_codex.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "def _log(args):\n"
        "    path = os.environ.get('FAKE_CODEX_LOG')\n"
        "    if path:\n"
        "        open(path, 'a').write(json.dumps(args) + '\\n')\n"
        "if 'app-server' in sys.argv:\n"
        "    outcome = os.environ.get('FAKE_RESET_OUTCOME', 'reset')\n"
        "    count = os.environ.get('FAKE_RESET_CREDITS', '1')\n"
        "    while True:\n"
        "        line = sys.stdin.readline()\n"
        "        if not line:\n"
        "            break\n"
        "        line = line.strip()\n"
        "        if not line:\n"
        "            continue\n"
        "        try:\n"
        "            msg = json.loads(line)\n"
        "        except ValueError:\n"
        "            continue\n"
        "        if msg.get('id') is None:\n"
        "            continue\n"
        "        method = msg.get('method')\n"
        "        if method == 'account/rateLimitResetCredit/consume':\n"
        "            _log(['consume', (msg.get('params') or {}).get('idempotencyKey', '')])\n"
        "            result = {'outcome': outcome}\n"
        "        elif method == 'account/rateLimits/read':\n"
        "            result = {'rateLimitResetCredits': {'availableCount': int(count)}}\n"
        "        else:\n"
        "            result = {}\n"
        "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "else:\n"
        "    _log(sys.argv[1:])\n"
        "    print('Queued message 123 for thread abc')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "CODEX_AUTO_STARTSTOP_HOME": str(home / "auto-startstop"),
            "CODEX_AUTO_STARTSTOP_SESSIONS_DIR": str(sessions),
            "CODEX_AUTO_STARTSTOP_STOP_PERCENT": "80",
            "CODEX_AUTO_STARTSTOP_CODEX_BIN": f"{PY} {fake}",
            "CODEX_AUTO_STARTSTOP_NO_SPAWN": "1",
            "FAKE_CODEX_LOG": str(fake_log),
            "FAKE_RESET_OUTCOME": "reset",
            "FAKE_RESET_CREDITS": "1",
        }
    )
    return home, sessions, env, fake_log


def run_cli(args, env, stdin_obj=None):
    proc = subprocess.run(
        [PY, str(SCRIPT), *args],
        input=json.dumps(stdin_obj) if stdin_obj is not None else None,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return proc


def state(home: Path) -> dict:
    p = home / "auto-startstop" / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"threads": {}}


def hook_input(stop_hook_active=False, transcript=None):
    d = {
        "session_id": SID,
        "turn_id": "t-1",
        "cwd": CWD,
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
        "last_assistant_message": "done",
        "transcript_path": transcript,
    }
    return d


def fake_calls(fake_log: Path) -> list:
    if not fake_log.is_file():
        return []
    return [json.loads(x) for x in fake_log.read_text().splitlines() if x.strip()]
def write_config(home: Path, **overrides) -> None:
    d = home / "auto-startstop"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "config.json"
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    cfg.update(overrides)
    p.write_text(json.dumps(cfg), encoding="utf-8")


def _json(s: str) -> dict:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}


def test_below_threshold(tmp: Path) -> None:
    print("test: below threshold -> allow stop, no state")
    home, sessions, env, _ = make_sandbox(tmp)
    write_rollout(sessions, used=12.0, resets_at=time.time() + 3600)
    proc = run_cli(["stop"], env, hook_input())
    check("exit 0", proc.returncode == 0, proc.stderr[-200:])
    check("no output", proc.stdout.strip() == "", proc.stdout[:200])


def test_steer(tmp: Path) -> None:
    print("test: above threshold -> block with handoff instruction")
    home, sessions, env, _ = make_sandbox(tmp)
    resets = time.time() + 3600
    write_rollout(sessions, used=92.0, resets_at=resets)
    proc = run_cli(["stop"], env, hook_input())
    check("exit 0", proc.returncode == 0, proc.stderr[-200:])
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {}
    check("decision=block", out.get("decision") == "block", proc.stdout[:200])
    reason = out.get("reason", "")
    check("reason mentions HANDOFF.md", "HANDOFF.md" in reason)
    check("reason mentions DONE/NEXT", "NEXT" in reason)
    st = state(home)["threads"].get(SID, {})
    check("state steered_window set", st.get("steered_window") == int(resets), str(st))
    check(
        "resume_at = resets_at + 90",
        abs(float(st.get("resume_at", 0)) - (int(resets) + 90)) < 2,
        str(st.get("resume_at")),
    )


def test_dedupe_and_new_window(tmp: Path) -> None:
    print("test: dedupe per window + re-steer on new window")
    home, sessions, env, _ = make_sandbox(tmp)
    resets = time.time() + 3600
    write_rollout(sessions, used=92.0, resets_at=resets)
    run_cli(["stop"], env, hook_input())
    proc2 = run_cli(["stop"], env, hook_input(stop_hook_active=True))
    check("second stop same window: no output", proc2.stdout.strip() == "", proc2.stdout[:200])
    write_rollout(sessions, used=95.0, resets_at=resets + 18000)
    proc3 = run_cli(["stop"], env, hook_input())
    try:
        out3 = json.loads(proc3.stdout)
    except json.JSONDecodeError:
        out3 = {}
    check("new window re-steers", out3.get("decision") == "block", proc3.stdout[:200])


def test_defer_when_stop_hook_active(tmp: Path) -> None:
    print("test: stop_hook_active defers steering instead of burning the window")
    home, sessions, env, _ = make_sandbox(tmp)
    write_rollout(sessions, used=92.0, resets_at=time.time() + 3600)
    proc = run_cli(["stop"], env, hook_input(stop_hook_active=True))
    check("deferred: no output", proc.stdout.strip() == "", proc.stdout[:200])
    st = state(home)["threads"].get(SID, {})
    check("deferred: window not consumed", st.get("steered_window") is None, str(st))
    proc2 = run_cli(["stop"], env, hook_input())
    try:
        out2 = json.loads(proc2.stdout)
    except json.JSONDecodeError:
        out2 = {}
    check("steers on fresh stop", out2.get("decision") == "block", proc2.stdout[:200])


def test_resume_dispatch(tmp: Path) -> None:
    print("test: resume due -> codex queue invoked")
    home, sessions, env, fake_log = make_sandbox(tmp)
    env["CODEX_AUTO_STARTSTOP_FORCE_RESUME_IN"] = "0"
    write_rollout(sessions, used=92.0, resets_at=time.time() + 3600)
    run_cli(["stop"], env, hook_input())
    proc = run_cli(["resume-check", "--thread", SID], env)
    check("exit 0", proc.returncode == 0, proc.stderr[-300:])
    calls = fake_calls(fake_log)
    check("fake codex called once", len(calls) == 1, str(calls))
    if calls:
        argv = calls[0]
        check("queue --thread <sid>", argv[:3] == ["queue", "--thread", SID], str(argv))
        check("resume message present", len(argv) >= 5 and argv[3] == "--message" and "reset" in argv[4])
    st = state(home)["threads"].get(SID, {})
    check("resume_sent_at recorded", bool(st.get("resume_sent_at")), str(st))


def test_resume_skips_on_activity(tmp: Path) -> None:
    print("test: user activity after reset -> resume skipped")
    home, sessions, env, fake_log = make_sandbox(tmp)
    env["CODEX_AUTO_STARTSTOP_FORCE_RESUME_IN"] = "0"
    resid = time.time() + 3600
    write_rollout(sessions, used=92.0, resets_at=resid)
    run_cli(["stop"], env, hook_input())
    # simulate the user prompting right after the reset
    write_rollout(
        sessions,
        used=1.0,
        resets_at=resid,
        extra_events=[
            {"timestamp": iso(time.time() + 1), "type": "event_msg", "payload": {"type": "task_started"}}
        ],
    )
    proc = run_cli(["resume-check", "--thread", SID], env)
    check("exit 0", proc.returncode == 0, proc.stderr[-300:])
    check("fake codex NOT called", fake_calls(fake_log) == [], str(fake_calls(fake_log)))
    st = state(home)["threads"].get(SID, {})
    check("resume_skipped recorded", bool(st.get("resume_skipped")), str(st))


def test_session_start(tmp: Path) -> None:
    print("test: session-start re-arms sleeper + context when due")
    home, sessions, env, _ = make_sandbox(tmp)
    write_rollout(sessions, used=92.0, resets_at=time.time() + 60)
    env["CODEX_AUTO_STARTSTOP_FORCE_RESUME_IN"] = "0"
    run_cli(["stop"], env, hook_input())
    proc = run_cli(
        ["session-start"], env, {"session_id": SID, "cwd": CWD, "hook_event_name": "SessionStart"}
    )
    check("exit 0", proc.returncode == 0, proc.stderr[-300:])
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {}
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    check("additionalContext present", bool(ctx), proc.stdout[:200])
    check("context mentions handoff", "HANDOFF.md" in ctx)


def test_status(tmp: Path) -> None:
    print("test: status prints limits + state")
    home, sessions, env, _ = make_sandbox(tmp)
    write_rollout(sessions, used=42.5, resets_at=time.time() + 3600)
    run_cli(["stop"], env, hook_input())
    proc = run_cli(["status", "--thread", SID], env)
    check("exit 0", proc.returncode == 0, proc.stderr[-300:])
    check("shows 5h window", "5h window" in proc.stdout and "42.5" in proc.stdout, proc.stdout[:300])


def test_weekly_trigger(tmp: Path) -> None:
    print("test: trigger=weekly steers on the weekly window")
    home, sessions, env, _ = make_sandbox(tmp)
    write_config(home, trigger="weekly")
    resets = time.time() + 500000
    write_rollout(
        sessions, used=10.0, resets_at=time.time() + 3600, weekly_used=95.0, weekly_resets=resets
    )
    proc = run_cli(["stop"], env, hook_input())
    out = _json(proc.stdout)
    check("decision=block", out.get("decision") == "block", proc.stdout[:200])
    check("reason mentions weekly", "weekly" in out.get("reason", "").lower(), out.get("reason", "")[:120])
    st = state(home)["threads"].get(SID, {})
    check("steered_limit=weekly", st.get("steered_limit") == "weekly", str(st))
    check(
        "resume_at = weekly resets + 90",
        abs(float(st.get("resume_at", 0)) - (int(resets) + 90)) < 2,
        str(st.get("resume_at")),
    )


def test_weekly_ignored_when_trigger_5h(tmp: Path) -> None:
    print("test: trigger=5h ignores a maxed weekly window")
    home, sessions, env, _ = make_sandbox(tmp)
    write_rollout(
        sessions,
        used=10.0,
        resets_at=time.time() + 3600,
        weekly_used=99.9,
        weekly_resets=time.time() + 500000,
    )
    proc = run_cli(["stop"], env, hook_input())
    check("no output", proc.stdout.strip() == "", proc.stdout[:200])


def test_both_trigger_picks_5h(tmp: Path) -> None:
    print("test: trigger=both steers on 5h when the weekly window is low")
    home, sessions, env, _ = make_sandbox(tmp)
    write_config(home, trigger="both")
    write_rollout(
        sessions,
        used=92.0,
        resets_at=time.time() + 3600,
        weekly_used=20.0,
        weekly_resets=time.time() + 500000,
    )
    proc = run_cli(["stop"], env, hook_input())
    out = _json(proc.stdout)
    check("decision=block", out.get("decision") == "block", proc.stdout[:200])
    st = state(home)["threads"].get(SID, {})
    check("steered_limit=5h", st.get("steered_limit") == "5h", str(st))


def test_both_trigger_picks_weekly(tmp: Path) -> None:
    print("test: trigger=both steers on weekly when it is the urgent window")
    home, sessions, env, _ = make_sandbox(tmp)
    write_config(home, trigger="both")
    resets = time.time() + 500000
    write_rollout(
        sessions,
        used=30.0,
        resets_at=time.time() + 3600,
        weekly_used=95.0,
        weekly_resets=resets,
    )
    proc = run_cli(["stop"], env, hook_input())
    out = _json(proc.stdout)
    check("decision=block", out.get("decision") == "block", proc.stdout[:200])
    st = state(home)["threads"].get(SID, {})
    check("steered_limit=weekly", st.get("steered_limit") == "weekly", str(st))



def _credit_calls(fake_log: Path) -> list:
    return [c for c in fake_calls(fake_log) if c and c[0] == "consume"]


def test_credit_weekly_resets(tmp: Path) -> None:
    print("test: reset credit clears a near-limit weekly window (no handoff)")
    home, sessions, env, fake_log = make_sandbox(tmp)
    write_config(home, use_reset_credits=True, reset_credit_on="weekly")
    write_rollout(
        sessions,
        used=20.0,
        resets_at=time.time() + 3600,
        weekly_used=95.0,
        weekly_resets=time.time() + 500000,
    )
    proc = run_cli(["stop"], env, hook_input())
    check("exit 0", proc.returncode == 0, proc.stderr[-200:])
    check("no handoff (no output)", proc.stdout.strip() == "", proc.stdout[:200])
    check("credit redeemed once", len(_credit_calls(fake_log)) == 1, str(_credit_calls(fake_log)))
    st = state(home)["threads"].get(SID, {})
    check("reset_credit_for recorded", bool(st.get("reset_credit_for")), str(st))
    check("no resume scheduled", not st.get("resume_at"), str(st))


def test_credit_not_spent_on_5h_alone(tmp: Path) -> None:
    print("test: 5h near limit with weekly far -> handoff, no credit")
    home, sessions, env, fake_log = make_sandbox(tmp)
    write_config(home, use_reset_credits=True, reset_credit_on="weekly")
    write_rollout(
        sessions,
        used=92.0,
        resets_at=time.time() + 3600,
        weekly_used=20.0,
        weekly_resets=time.time() + 500000,
    )
    proc = run_cli(["stop"], env, hook_input())
    out = _json(proc.stdout)
    check("decision=block (handoff)", out.get("decision") == "block", proc.stdout[:200])
    check("no credit redeemed", _credit_calls(fake_log) == [], str(_credit_calls(fake_log)))


def test_credit_unavailable_falls_back(tmp: Path) -> None:
    print("test: no credit available -> handoff")
    home, sessions, env, fake_log = make_sandbox(tmp)
    env["FAKE_RESET_OUTCOME"] = "noCredit"
    write_config(home, use_reset_credits=True, reset_credit_on="weekly")
    write_rollout(
        sessions,
        used=20.0,
        resets_at=time.time() + 3600,
        weekly_used=95.0,
        weekly_resets=time.time() + 500000,
    )
    proc = run_cli(["stop"], env, hook_input())
    out = _json(proc.stdout)
    check("decision=block (handoff)", out.get("decision") == "block", proc.stdout[:200])
    check("credit attempted once", len(_credit_calls(fake_log)) == 1, str(_credit_calls(fake_log)))


def test_credit_on_5h(tmp: Path) -> None:
    print("test: reset_credit_on=5h redeems on the 5h threshold")
    home, sessions, env, fake_log = make_sandbox(tmp)
    write_config(home, use_reset_credits=True, reset_credit_on="5h")
    write_rollout(
        sessions,
        used=92.0,
        resets_at=time.time() + 3600,
        weekly_used=20.0,
        weekly_resets=time.time() + 500000,
    )
    proc = run_cli(["stop"], env, hook_input())
    check("no handoff (no output)", proc.stdout.strip() == "", proc.stdout[:200])
    check("credit redeemed once", len(_credit_calls(fake_log)) == 1, str(_credit_calls(fake_log)))


def test_credit_once_per_window(tmp: Path) -> None:
    print("test: credit is redeemed once per window (idempotent)")
    home, sessions, env, fake_log = make_sandbox(tmp)
    write_config(home, use_reset_credits=True, reset_credit_on="weekly")
    resets = time.time() + 500000
    write_rollout(
        sessions, used=20.0, resets_at=time.time() + 3600, weekly_used=95.0, weekly_resets=resets
    )
    run_cli(["stop"], env, hook_input())
    # the reset takes effect: the same window now reads low
    write_rollout(
        sessions, used=5.0, resets_at=time.time() + 3600, weekly_used=5.0, weekly_resets=resets
    )
    proc = run_cli(["stop"], env, hook_input())
    check("second run: no output", proc.stdout.strip() == "", proc.stdout[:200])
    check("credit redeemed exactly once", len(_credit_calls(fake_log)) == 1, str(_credit_calls(fake_log)))

def main() -> int:
    tests = [
        test_below_threshold,
        test_steer,
        test_dedupe_and_new_window,
        test_defer_when_stop_hook_active,
        test_resume_dispatch,
        test_resume_skips_on_activity,
        test_session_start,
        test_status,
        test_weekly_trigger,
        test_weekly_ignored_when_trigger_5h,
        test_both_trigger_picks_5h,
        test_both_trigger_picks_5h,
        test_both_trigger_picks_weekly,
        test_credit_weekly_resets,
        test_credit_not_spent_on_5h_alone,
        test_credit_unavailable_falls_back,
        test_credit_on_5h,
        test_credit_once_per_window,
    ]
    for fn in tests:
        with tempfile.TemporaryDirectory(prefix="auto-startstop-test-") as td:
            fn(Path(td))
        print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:", ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
