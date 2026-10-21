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

def test_handoff_prompt_file(tmp: Path) -> None:
    print("test: handoff-prompt.md supplies the handoff prompt")
    home, sessions, env, _ = make_sandbox(tmp)
    d = home / "auto-startstop"
    d.mkdir(parents=True, exist_ok=True)
    (d / "handoff-prompt.md").write_text(
        "CUSTOM PROMPT {percent:.0f}% -> write {handoff_file} now (NEXT)", encoding="utf-8"
    )
    write_rollout(sessions, used=92.0, resets_at=time.time() + 3600)
    proc = run_cli(["stop"], env, hook_input())
    out = _json(proc.stdout)
    reason = out.get("reason", "")
    check("decision=block", out.get("decision") == "block", proc.stdout[:200])
    check("prompt read from file", reason.startswith("CUSTOM PROMPT"), reason[:120])
    check("placeholders rendered", "92% -> write HANDOFF.md now" in reason, reason[:120])


def test_config_stop_message_overrides_file(tmp: Path) -> None:
    print("test: stop_message in config.json beats the prompt file")
    home, sessions, env, _ = make_sandbox(tmp)
    write_config(home, stop_message="JSON PROMPT for {handoff_file}")
    (home / "auto-startstop" / "handoff-prompt.md").write_text("FILE PROMPT", encoding="utf-8")
    write_rollout(sessions, used=92.0, resets_at=time.time() + 3600)
    proc = run_cli(["stop"], env, hook_input())
    out = _json(proc.stdout)
    reason = out.get("reason", "")
    check("decision=block", out.get("decision") == "block", proc.stdout[:200])
    check("legacy JSON override wins", reason.startswith("JSON PROMPT"), reason[:120])


def test_handoff_prompt_file_edges(tmp: Path) -> None:
    print("test: unusable prompt files fall back to a usable prompt")
    cases = {
        "empty": b"",
        "whitespace": b"   \n\n\t",
        "binary": b"\xff\xfe\x00 not utf-8 \x80",
        "oversize": b"x" * (64 * 1024 + 1),
    }
    for name, raw in cases.items():
        case = tmp / name
        case.mkdir()
        home, sessions, env, _ = make_sandbox(case)
        prompt = home / "auto-startstop" / "handoff-prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_bytes(raw)
        write_rollout(sessions, used=92.0, resets_at=time.time() + 3600)
        proc = run_cli(["stop"], env, hook_input())
        out = _json(proc.stdout)
        reason = out.get("reason", "")
        check(f"{name}: exit 0", proc.returncode == 0, proc.stderr[-160:])
        check(f"{name}: still blocks", out.get("decision") == "block", proc.stdout[:160])
        check(
            f"{name}: usable fallback prompt",
            "HANDOFF.md" in reason and "NEXT" in reason,
            reason[:120],
        )


def test_handoff_prompt_bom_stripped(tmp: Path) -> None:
    print("test: a UTF-8 BOM on the prompt file is stripped")
    home, sessions, env, _ = make_sandbox(tmp)
    prompt = home / "auto-startstop" / "handoff-prompt.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_bytes("BOM PROMPT for {handoff_file}".encode("utf-8-sig"))
    write_rollout(sessions, used=92.0, resets_at=time.time() + 3600)
    reason = _json(run_cli(["stop"], env, hook_input()).stdout).get("reason", "")
    check("prompt used", reason.startswith("BOM PROMPT"), repr(reason[:24]))
    check("BOM dropped", "\ufeff" not in reason, repr(reason[:24]))


def test_handoff_prompt_stray_braces(tmp: Path) -> None:
    print("test: literal braces in the prompt still render placeholders")
    home, sessions, env, _ = make_sandbox(tmp)
    prompt = home / "auto-startstop" / "handoff-prompt.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text('json {"a": 1} -> write {handoff_file} ({percent:.0f}%)', encoding="utf-8")
    write_rollout(sessions, used=92.0, resets_at=time.time() + 3600)
    reason = _json(run_cli(["stop"], env, hook_input()).stdout).get("reason", "")
    check("literal brace kept", '{"a": 1}' in reason, reason[:120])
    check("placeholder rendered", "write HANDOFF.md (92%)" in reason, reason[:120])


def test_builtin_prompt_when_no_file(tmp: Path) -> None:
    print("test: no prompt file anywhere -> built-in default is used")
    _home, _sessions, env, _ = make_sandbox(tmp)
    lone = tmp / "lone"
    lone.mkdir()
    script = lone / "codex_auto_startstop.py"
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    env = dict(env)
    env["CODEX_AUTO_STARTSTOP_HOME"] = str(tmp / "lone-home")
    code = (
        "import gc, importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('cas', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "cfg = m.load_config()\n"
        "found = m.load_handoff_prompt()\n"
        "gc.collect()\n"
        "print(json.dumps({'builtin': cfg['stop_message'] == m.DEFAULT_CONFIG['stop_message'],\n"
        "                  'found': found}))\n"
    )
    proc = subprocess.run(
        [PY, "-W", "error::ResourceWarning", "-c", code, str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    data = _json(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    check("no prompt file found", data.get("found") is None, (str(data) + proc.stderr)[:200])
    check("built-in prompt used", data.get("builtin") is True, str(data)[:200])


def test_credit_deduplicated_across_sessions(tmp: Path) -> None:
    print("test: two sessions at the same limit window redeem only one credit")
    home, sessions, env, fake_log = make_sandbox(tmp)
    write_config(home, use_reset_credits=True, reset_credit_on="weekly")
    resets = time.time() + 500000
    first_rollout = write_rollout(
        sessions, used=20.0, resets_at=time.time() + 3600,
        weekly_used=95.0, weekly_resets=resets
    )
    second_sid = "01a12233-a8f7-7cc2-842d-e1f79c5028e4"
    second_rollout = first_rollout.with_name(first_rollout.name.replace(SID, second_sid))
    second_rollout.write_text(
        first_rollout.read_text(encoding="utf-8").replace(SID, second_sid),
        encoding="utf-8"
    )
    first = run_cli(["stop"], env, hook_input())
    second_input = dict(hook_input())
    second_input["session_id"] = second_sid
    second_input["transcript_path"] = str(second_rollout)
    second = run_cli(["stop"], env, second_input)
    check("both sessions exit successfully",
          first.returncode == 0 and second.returncode == 0)
    check("second session skips unnecessary handoff", second.stdout.strip() == "")
    check("at most one account-wide redemption",
          len(_credit_calls(fake_log)) == 1, str(_credit_calls(fake_log)))
    check("second session records shared credit",
          state(home)["threads"][second_sid].get("reset_credit_for")
          == state(home)["threads"][SID].get("reset_credit_for"))


def test_install_requires_hook_approval(tmp: Path) -> None:
    print("test: install never trusts hooks and preserves unrelated hook definitions")
    home, _sessions, env, _ = make_sandbox(tmp)
    home.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    env["CODEX_HOME"] = str(home)
    toml = home / "config.toml"
    original_toml = '[features]\ncodex_hooks = true\n'
    toml.write_text(original_toml, encoding="utf-8")
    hj = home / "hooks.json"
    foreign = {"type": "command", "command": "echo unrelated", "timeout": 5}
    hj.write_text(json.dumps({"description": "user hooks", "hooks": {
        "Stop": [{"hooks": [foreign]}]}}), encoding="utf-8")
    first = run_cli(["install"], env)
    check("install exits 0", first.returncode == 0, first.stderr[-200:])
    check("installer says NOT trusted", "NOT trusted" in first.stdout, first.stdout[:200])
    check("config.toml untouched", toml.read_text(encoding="utf-8") == original_toml)
    data = json.loads(hj.read_text(encoding="utf-8"))
    check("existing Stop hook preserved", any(
        foreign in group["hooks"] for group in data["hooks"]["Stop"]))
    for event in ("Stop", "SessionStart"):
        ours = [h for g in data["hooks"][event] for h in g["hooks"]
                if "codex_auto_startstop.py" in h.get("command", "")]
        check(f"{event}: exactly one command", len(ours) == 1)
        check(f"{event}: clear status", bool(ours and ours[0].get(
            "statusMessage", "").startswith("codex-auto-startstop:")))
    initial = hj.read_bytes()
    initial_mtime = hj.stat().st_mtime_ns
    second = run_cli(["install"], env)
    check("reinstall exits 0", second.returncode == 0)
    check("reinstall reports unchanged", "hooks unchanged" in second.stdout)
    check("reinstall preserves hook contents", hj.read_bytes() == initial)
    check("reinstall does not rewrite hook file", hj.stat().st_mtime_ns == initial_mtime)
    check("reinstall does not trust hooks", toml.read_text(encoding="utf-8") == original_toml)

    # Uninstall recognizes older commands even if config's Python setting changed.
    write_config(home, python="/opt/other-python")
    removed = run_cli(["uninstall"], env)
    check("uninstall exits 0", removed.returncode == 0, removed.stderr[-200:])
    remaining = json.loads(hj.read_text(encoding="utf-8"))
    check("uninstall preserves unrelated hook", any(
        foreign in group["hooks"] for group in remaining["hooks"]["Stop"]))
    check("uninstall removes Stop handler", not any(
        "codex_auto_startstop.py" in h.get("command", "")
        for g in remaining["hooks"]["Stop"] for h in g["hooks"]))
    check("uninstall removes SessionStart handler",
          "SessionStart" not in remaining["hooks"])


def test_install_refuses_broken_hooks(tmp: Path) -> None:
    print("test: malformed hooks fail safely without copying or overwriting")
    home, _sessions, env, _ = make_sandbox(tmp)
    home.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    env["CODEX_HOME"] = str(home)
    hj = home / "hooks.json"
    for malformed in ('[]', '{"hooks": {"Stop": "bad"}}', '{"hooks":'):
        hj.write_text(malformed, encoding="utf-8")
        proc = run_cli(["install"], env)
        check("invalid hook config rejected", proc.returncode == 1)
        check("invalid hook config unchanged", hj.read_text(encoding="utf-8") == malformed)
        check("no script copied on invalid config",
              not (home / "auto-startstop" / "codex_auto_startstop.py").exists())


def test_reinstall_consolidates_old_hooks(tmp: Path) -> None:
    print("test: stale standalone commands are replaced without duplicates")
    home, _sessions, env, _ = make_sandbox(tmp)
    home.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    env["CODEX_HOME"] = str(home)
    command = "python3 /older/codex-auto-startstop/codex_auto_startstop.py stop"
    hj = home / "hooks.json"
    hj.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": command}]},
        {"hooks": [{"type": "command", "command": command}]},
    ]}}), encoding="utf-8")
    installed = run_cli(["install"], env)
    check("upgrade installs", installed.returncode == 0, installed.stderr[-200:])
    data = json.loads(hj.read_text(encoding="utf-8"))
    commands = [h["command"] for group in data["hooks"]["Stop"]
                for h in group["hooks"]]
    check("no duplicate Stop hooks", len(commands) == 1, str(commands))
    check("old script path replaced", "/older/" not in commands[0], str(commands))


def main() -> int:
    tests = [
        test_install_requires_hook_approval,
        test_install_refuses_broken_hooks,
        test_reinstall_consolidates_old_hooks,
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
        test_credit_deduplicated_across_sessions,
        test_handoff_prompt_file,
        test_config_stop_message_overrides_file,
        test_handoff_prompt_file_edges,
        test_handoff_prompt_bom_stripped,
        test_handoff_prompt_stray_braces,
        test_builtin_prompt_when_no_file,
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
