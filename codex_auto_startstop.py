#!/usr/bin/env python3
"""
codex-auto-startstop — automatic "wrap up" + auto-resume around Codex's usage limits.

How it works
------------
Codex (CLI >= 0.160) has a stable hooks system (Claude-Code style). This tool installs
two hooks plus a tiny resume scheduler:

  * Stop hook          -> at every turn end, reads the session's own rate-limit snapshot
                          from its rollout file. When the 5h window reaches STOP_PERCENT,
                          or the weekly window reaches STOP_PERCENT_WEEKLY (per TRIGGER),
                          it blocks the stop and injects a steering message: "write a
                          handoff report (DONE / NEXT / BLOCKED / RESUME), then stop".
                          The block is applied once per limit window.
  * SessionStart hook  -> if a resume is pending and the sleeping helper died (e.g. the
                          machine rebooted), re-arms it; can add a small model note.
  * Resume sleeper     -> a detached helper process. After the limit window resets it sends
                          the continue message into the session via `codex queue`, which
                          is Codex's own durable, idle-dispatching message queue.

TRIGGER ("5h" | "weekly" | "both") selects which limit drives the stop/resume.

RESET CREDITS (use_reset_credits=true) spend Codex's "usage limit resets" instead
of writing a handoff, for the window named by reset_credit_on ("weekly" | "5h" |
"both"). No credit available -> normal handoff.

Everything is fail-open: if anything goes wrong the hook prints nothing and exits 0,
so it can never break a Codex session.

Subcommands (used by Codex hooks / install / humans):
  stop            hook entry point (stdin: Codex Stop hook JSON)
  session-start   hook entry point (stdin: Codex SessionStart hook JSON)
  sleeper         background helper (detached); one-shot with --once
  status          show rate-limit + auto-startstop state for a session
  credits         show available usage-limit reset credits (app-server RPC)
  install         install hooks.json + seed hook trust; copy this script into CODEX_HOME
  uninstall       remove hooks and trust entries
  fetch-hashes    internal: ask Codex for current hook hashes (app-server RPC)

Env overrides (mostly for tests):
  CODEX_HOME, CODEX_AUTO_STARTSTOP_HOME, CODEX_AUTO_STARTSTOP_SESSIONS_DIR,
  CODEX_AUTO_STARTSTOP_STOP_PERCENT, CODEX_AUTO_STARTSTOP_RESUME_BUFFER_SEC,
  CODEX_AUTO_STARTSTOP_FORCE_RESUME_IN (debug: seconds), CODEX_AUTO_STARTSTOP_CODEX_BIN,
  CODEX_AUTO_STARTSTOP_NO_SPAWN=1 (don't spawn detached helper), CODEX_AUTO_STARTSTOP_POLL_SEC
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

try:  # POSIX only; hook state locking degrades to a no-op elsewhere
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

VERSION = "1.0.0"

DEFAULT_CONFIG = {
    "trigger": "5h",
    "use_reset_credits": False,
    "reset_credit_on": "weekly",
    "stop_percent": 85.0,
    "stop_percent_weekly": 90.0,
    "resume_buffer_sec": 90,
    "handoff_file": "HANDOFF.md",
    "resume": True,
    "codex_bin": "codex",
    "python": sys.executable or "python3",
    "notify_desktop": True,
    "stop_message": (
        "\u26d4 codex-auto-startstop: the {window} is at {percent:.0f}% "
        "(resets {reset_local}, about {minutes_left:.0f} min left). Do not start new work. "
        "Immediately: (1) write a handoff report to `{handoff_file}` in the project root with "
        "four short sections - DONE (what was completed, key files), NEXT (concrete ordered next "
        "steps to finish the task), BLOCKED (anything blocked and why), RESUME (the exact first "
        "actions/commands when work resumes); (2) reply with a 3-line summary; (3) stop. "
        "Work resumes automatically after the limit resets."
    ),
    "resume_message": (
        "\u23f0 codex-auto-startstop: the {window} has reset - resuming. "
        "If `{handoff_file}` exists in the project root, read it and continue the original task "
        "from its NEXT section. If it is missing, first write a brief DONE/NEXT/BLOCKED summary "
        "of where things stand, then continue. If the task is already complete, reply "
        "\"nothing to do\" and stop."
    ),
    "session_start_context": (
        "[codex-auto-startstop] An auto-stop from a previous {window} is pending for this "
        "session (window reset at {reset_local}). A resume message may arrive automatically; "
        "if work resumes, check `{handoff_file}` first."
    ),
}


# --------------------------------------------------------------------------- paths


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def auto_startstop_home() -> Path:
    return Path(os.environ.get("CODEX_AUTO_STARTSTOP_HOME") or (codex_home() / "auto-startstop"))


def sessions_dir() -> Path:
    return Path(os.environ.get("CODEX_AUTO_STARTSTOP_SESSIONS_DIR") or (codex_home() / "sessions"))


def state_path() -> Path:
    return auto_startstop_home() / "state.json"


def config_path() -> Path:
    return auto_startstop_home() / "config.json"


def log_path() -> Path:
    return auto_startstop_home() / "auto-startstop.log"


def hooks_path() -> Path:
    return codex_home() / "hooks.json"


# --------------------------------------------------------------------------- basic io


def log(msg: str) -> None:
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}\n"
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line)
        # keep the log small (512KB cap, truncate to last 256KB)
        try:
            if p.stat().st_size > 512 * 1024:
                data = p.read_bytes()[-256 * 1024:]
                tmp = p.with_name(p.name + ".trunc")
                tmp.write_bytes(data)
                os.replace(tmp, p)
        except OSError:
            pass
    except OSError:
        pass


def _atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    """Durably replace `path` with `text` (temp file + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if config_path().is_file():
            user = json.loads(config_path().read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update(user)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"config: ignoring unreadable config.json: {exc}")
    env_map = {
        "CODEX_AUTO_STARTSTOP_STOP_PERCENT": ("stop_percent", float),
        "CODEX_AUTO_STARTSTOP_RESUME_BUFFER_SEC": ("resume_buffer_sec", float),
        "CODEX_AUTO_STARTSTOP_CODEX_BIN": ("codex_bin", str),
        "CODEX_AUTO_STARTSTOP_POLL_SEC": ("poll_sec", float),
    }
    for env, (key, cast) in env_map.items():
        if env in os.environ:
            try:
                cfg[key] = cast(os.environ[env])
            except ValueError:
                log(f"config: bad {env}={os.environ[env]!r}")
    return cfg


class _SafeDict(dict):
    def __missing__(self, key):  # keep unknown placeholders visible instead of crashing
        return "{" + key + "}"


def render(template: str, **ctx) -> str:
    try:
        return str(template).format_map(_SafeDict(ctx))
    except (ValueError, IndexError, KeyError):
        return str(template)


def fmt_reset(resets_at) -> str:
    try:
        ts = float(resets_at)
    except (TypeError, ValueError):
        return "unknown"
    if ts <= 0:
        return "unknown"
    dt = datetime.fromtimestamp(ts)
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%a %H:%M")

# --------------------------------------------------------------------------- limit windows

_LIMIT_DEFS = {
    # trigger -> (rate_limits key, human label, config threshold key, default)
    "5h": ("primary", "5-hour usage window", "stop_percent", 85.0),
    "weekly": ("secondary", "weekly usage window", "stop_percent_weekly", 90.0),
}


def _limit_label(name: str) -> str:
    return _LIMIT_DEFS.get(str(name), _LIMIT_DEFS["5h"])[1]


def _trigger_windows(cfg: dict) -> list:
    t = str(cfg.get("trigger", "5h")).strip().lower()
    if t in ("both", "auto", "either", "all"):
        names = ["5h", "weekly"]
    elif t in ("weekly", "7d", "secondary"):
        names = ["weekly"]
    else:
        names = ["5h"]
    # A window that can spend a credit is also worth watching, so it can still
    # hand off when no credit is available.
    if cfg.get("use_reset_credits"):
        for name in _credit_windows(cfg):
            if name not in names:
                names.append(name)
    return names


def select_window(cfg: dict, rl: dict):
    """Pick the limit window that drives stop/resume.

    Returns the most urgent window that fired (crossed its threshold or is
    exhausted), or None when the trigger windows carry no usable data.
    """
    reached = bool(rl.get("rate_limit_reached_type"))
    windows = []
    for name in _trigger_windows(cfg):
        key, label, pct_key, default = _LIMIT_DEFS[name]
        w = rl.get(key) or {}
        used = w.get("used_percent")
        if not isinstance(used, (int, float)):
            continue
        thr = float(cfg.get(pct_key, default))
        resets = w.get("resets_at")
        windows.append(
            {
                "name": name,
                "label": label,
                "used": float(used),
                "threshold": thr,
                "resets_at": resets if isinstance(resets, (int, float)) else None,
                "crossed": float(used) >= thr,
                "exhausted": float(used) >= 99.5 or (reached and float(used) >= 95.0),
            }
        )
    fired = [w for w in windows if w["crossed"] or w["exhausted"]]
    if not fired:
        return None
    return max(fired, key=lambda w: (w["used"] - w["threshold"], w["used"]))


# --------------------------------------------------------------------------- state


@contextlib.contextmanager
def state_lock():
    if fcntl is None:
        yield
        return
    p = auto_startstop_home() / "state.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def load_state() -> dict:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
        if isinstance(st, dict) and isinstance(st.get("threads"), dict):
            return st
    except (OSError, json.JSONDecodeError):
        pass
    return {"threads": {}}


def save_state(st: dict) -> None:
    _atomic_write_text(state_path(), json.dumps(st, indent=1, sort_keys=True), mode=0o600)


# --------------------------------------------------------------------------- rollout parsing


def _read_tail(path: Path, nbytes: int = 262144) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - nbytes))
        data = fh.read()
    text = data.decode("utf-8", errors="ignore")
    # drop a possibly partial first line unless we read from the beginning
    if size > nbytes:
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1 :]
    return text


def _extract_rate_limits(obj) -> dict | None:
    cands = []
    if isinstance(obj, dict):
        cands.append(obj.get("payload"))
        cands.append(obj)
    for cand in cands:
        if isinstance(cand, dict) and isinstance(cand.get("rate_limits"), dict):
            return cand["rate_limits"]
    return None


def read_last_rate_limits(path: Path | None):
    """Return (rate_limits, source_obj) from the tail of a rollout file."""
    if not path or not Path(path).is_file():
        return None, None
    try:
        text = _read_tail(Path(path))
    except OSError:
        return None, None
    for line in reversed(text.splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        rl = _extract_rate_limits(obj)
        if rl:
            return rl, obj
    return None, None


def _parse_ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def read_tail_events(path: Path | None, max_events: int = 400):
    """Return list of (ts, type, payload_type) for the tail of a rollout."""
    if not path or not Path(path).is_file():
        return []
    try:
        text = _read_tail(Path(path), 524288)
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        if '"timestamp"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        ts = _parse_ts(obj.get("timestamp"))
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        events.append((ts, obj.get("type"), payload.get("type")))
    return events[-max_events:]


_START_EVENTS = {"task_started", "user_message", "user_turn"}
_END_EVENTS = {"task_complete", "turn_aborted", "user_message_interrupt"}


def session_active(events, resume_at: float) -> tuple[bool, str]:
    """True when the user/session is active so an auto-resume should be skipped."""
    lifecycle = []
    for ts, etype, ptype in events:
        if etype != "event_msg" or ts is None:
            continue
        if ptype in _START_EVENTS:
            lifecycle.append(("start", ts))
        elif ptype in _END_EVENTS:
            lifecycle.append(("end", ts))
    if any(kind == "start" and ts > resume_at for kind, ts in lifecycle):
        return True, "session got user activity after the limit reset"
    if lifecycle and lifecycle[-1][0] == "start":
        return True, "session is mid-turn at resume time"
    return False, ""


def find_rollout(session_id: str, hint=None) -> Path | None:
    if hint:
        p = Path(str(hint))
        if p.is_file() and p.name.startswith("rollout-") and p.suffix == ".jsonl":
            return p
    root = sessions_dir()
    if not root.is_dir() or not session_id:
        return None
    sid = re.sub(r"[^A-Za-z0-9._-]", "", session_id)
    if not sid:
        return None
    for pattern in (f"*/*/*/rollout-*{sid}*.jsonl", f"**/rollout-*{sid}*.jsonl"):
        try:
            matches = glob.glob(str(root / pattern), recursive=True)
        except OSError:
            matches = []
        if matches:
            def _mtime(m):
                try:
                    return os.path.getmtime(m)
                except OSError:
                    return 0.0

            matches.sort(key=_mtime, reverse=True)
            return Path(matches[0])
    return None


# --------------------------------------------------------------------------- notifications


def notify_desktop(cfg: dict, title: str, body: str) -> None:
    if not cfg.get("notify_desktop", True):
        return
    try:
        if sys.platform == "darwin":
            script = "display notification {} with title {}".format(
                json.dumps(body), json.dumps(title)
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
            return
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return
        exe = shutil.which("notify-send")
        if not exe:
            return
        subprocess.run([exe, title, body], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# --------------------------------------------------------------------------- codex queue


def codex_queue(cfg: dict, session_id: str, message: str, cwd=None):
    base = shlex.split(str(cfg.get("codex_bin") or "codex"))
    cmd = base + ["queue", "--thread", session_id, "--message", message]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(cwd) if cwd and Path(cwd).is_dir() else None,
        )
        ok = proc.returncode == 0
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return ok, (out or err)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


# --------------------------------------------------------------------------- sleeper


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name != "posix":
        # Windows: probe with OpenProcess; if the probe itself fails, prefer
        # respawning over silently leaving a dead resume helper behind.
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
                return True
            return False
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


def ensure_sleeper(session_id: str) -> bool:
    """Spawn the detached resume helper unless one is already running."""
    # Hold the lock across the check-and-spawn so two concurrent hooks cannot
    # both observe "no live sleeper" and spawn two helpers.
    with state_lock():
        st = load_state()
        t = st["threads"].setdefault(session_id, {})
        if not t.get("resume_at"):
            return False
        if t.get("resume_sent_at") or t.get("resume_skipped") or t.get("resume_failed"):
            return False
        if os.environ.get("CODEX_AUTO_STARTSTOP_NO_SPAWN"):
            log(f"sleeper: spawn suppressed (NO_SPAWN) for {session_id}")
            return False
        pid = t.get("sleeper_pid")
        if pid and t.get("sleeper_for") == t.get("resume_at") and _pid_alive(pid):
            return True

        script = Path(__file__).resolve()
        cfg = load_config()
        python = str(cfg.get("python") or sys.executable or "python3")
        detach: dict = {}
        if os.name == "posix":
            detach["start_new_session"] = True
        else:  # Windows: closest equivalent of setsid + no console window
            detach["creationflags"] = 0x00000008 | 0x00000200
        try:
            logf = open(log_path(), "a", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    [python, str(script), "sleeper", "--thread", session_id],
                    stdin=subprocess.DEVNULL,
                    stdout=logf,
                    stderr=logf,
                    cwd=str(auto_startstop_home()),
                    **detach,
                )
            finally:
                logf.close()
        except OSError as exc:
            log(f"sleeper: spawn failed for {session_id}: {exc}")
            return False

        t["sleeper_pid"] = proc.pid
        t["sleeper_for"] = t.get("resume_at")
        save_state(st)
        log(f"sleeper: spawned pid={proc.pid} for {session_id} (resume_at={t.get('resume_at')})")
        return True


def cmd_sleeper(args) -> int:
    cfg = load_config()
    sid = args.thread
    poll = float(os.environ.get("CODEX_AUTO_STARTSTOP_POLL_SEC", cfg.get("poll_sec", 30)) or 30)
    deadline = time.time() + (args.max_hours * 3600 if args.max_hours else 8 * 3600)
    log(f"sleeper: start for {sid} (once={args.once})")
    while True:
        if time.time() > deadline:
            log(f"sleeper: max lifetime reached for {sid}; exit")
            return 0
        with state_lock():
            st = load_state()
            t = dict(st["threads"].get(sid) or {})
        if not t.get("resume_at"):
            log(f"sleeper: nothing pending for {sid}; exit")
            return 0
        if t.get("resume_sent_at") or t.get("resume_skipped") or t.get("resume_failed"):
            log(f"sleeper: already finished for {sid}; exit")
            return 0
        now = time.time()
        resume_at = float(t["resume_at"])
        if now < resume_at:
            if args.once:
                log(f"sleeper: not due yet for {sid} (in {resume_at - now:.0f}s); exit")
                return 0
            time.sleep(max(1.0, min(poll, resume_at - now)))
            continue

        rollout = Path(t["rollout"]) if t.get("rollout") else find_rollout(sid)
        if rollout and rollout.is_file():
            events = read_tail_events(rollout)
            active, why = session_active(events, resume_at)
            if active:
                with state_lock():
                    st = load_state()
                    tt = st["threads"].setdefault(sid, {})
                    tt["resume_skipped"] = why
                    tt["resume_skipped_at"] = now
                    save_state(st)
                log(f"sleeper: skip resume for {sid}: {why}")
                notify_desktop(cfg, "codex-auto-startstop", f"Auto-resume skipped: {why}")
                return 0
            rl, _ = read_last_rate_limits(rollout)
            sel = select_window(cfg, rl) if rl else None
            resets_at = sel.get("resets_at") if sel else None
            if (
                sel
                and sel["exhausted"]
                and isinstance(resets_at, (int, float))
                and resets_at > now + 60
            ):
                with state_lock():
                    st = load_state()
                    tt = st["threads"].setdefault(sid, {})
                    tt["resume_at"] = resets_at + float(cfg.get("resume_buffer_sec", 90))
                    tt["sleeper_for"] = tt["resume_at"]
                    save_state(st)
                log(f"sleeper: {sel['label']} still exhausted for {sid}; new resume_at={tt['resume_at']}")
                if args.once:
                    return 0
                continue

        if not cfg.get("resume", True):
            log(f"sleeper: resume disabled by config for {sid}")
            return 0
        msg = render(
            cfg["resume_message"],
            window=_limit_label(t.get("steered_limit", "5h")),
            handoff_file=cfg.get("handoff_file", "HANDOFF.md"),
            reset_local=fmt_reset(t.get("window_resets_at")),
            percent=float(t.get("steered_used") or 0.0),
        )
        ok, out = codex_queue(cfg, sid, msg, cwd=t.get("cwd"))
        if ok:
            with state_lock():
                st = load_state()
                tt = st["threads"].setdefault(sid, {})
                tt["resume_sent_at"] = time.time()
                tt["resume_result"] = out[:400]
                save_state(st)
            log(f"sleeper: resume message queued for {sid}: {out[:200]}")
            notify_desktop(cfg, "codex-auto-startstop", f"Resume message sent to session {sid[:8]}")
            return 0
        log(f"sleeper: queue failed for {sid}: {out[:300]}")
        attempts = int(t.get("resume_attempts") or 0) + 1
        with state_lock():
            st = load_state()
            tt = st["threads"].setdefault(sid, {})
            tt["resume_attempts"] = attempts
            tt["resume_result"] = out[:400]
            if attempts >= 24:
                tt["resume_failed"] = True
            save_state(st)
        if attempts >= 24:
            log(f"sleeper: giving up on {sid} after {attempts} attempts")
            notify_desktop(cfg, "codex-auto-startstop", "Resume failed - see auto-startstop.log")
            return 1
        if args.once:
            return 1
        time.sleep(300)


def cmd_resume_check(args) -> int:
    """One-shot due-check (used by tests / manual)."""
    args.once = True
    if args.max_hours is None:
        args.max_hours = 0
    return cmd_sleeper(args)


# --------------------------------------------------------------------------- hooks


def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        log(f"hook: bad stdin json: {raw[:200]!r}")
        return {}


def _print_hook_output(obj) -> None:
    if obj:
        sys.stdout.write(json.dumps(obj))
        sys.stdout.write("\n")


def cmd_stop(_args) -> int:
    inp = _read_stdin_json()
    sid = str(inp.get("session_id") or "").strip()
    if not sid:
        return 0
    cwd = str(inp.get("cwd") or os.getcwd())
    stop_hook_active = bool(inp.get("stop_hook_active"))
    cfg = load_config()
    now = time.time()

    with state_lock():
        st = load_state()
        t = st["threads"].setdefault(sid, {})
        t["cwd"] = cwd
        t["last_seen"] = now
        rollout = find_rollout(sid, inp.get("transcript_path"))
        if rollout:
            t["rollout"] = str(rollout)
        existing = Path(t["rollout"]) if t.get("rollout") else None
        save_state(st)

    if not rollout:
        rollout = existing
    rl, _src = read_last_rate_limits(rollout)
    if not rl:
        log(f"stop: no rate-limit data for {sid} (rollout={rollout})")
        return 0

    # Reset credits run before the trigger check, because the credit window can
    # differ from the trigger window. A redeemed credit clears the window, so
    # there is nothing to hand off.
    if cfg.get("use_reset_credits") and maybe_redeem_reset_credit(cfg, rl, sid, now):
        return 0

    sel = select_window(cfg, rl)
    if not sel:
        log(f"stop: no usable rate-limit window for {sid} (rollout={rollout})")
        return 0
    limit = sel["name"]
    used = sel["used"]
    window = sel["resets_at"]
    crossed = sel["crossed"]
    exhausted = sel["exhausted"]

    resume_in = os.environ.get("CODEX_AUTO_STARTSTOP_FORCE_RESUME_IN")
    try:
        resume_in = float(resume_in) if resume_in else None
    except ValueError:
        resume_in = None

    with state_lock():
        st = load_state()
        t = st["threads"].setdefault(sid, {})
        already_steered = (
            window is not None
            and t.get("steered_window") == window
            and t.get("steered_limit", "5h") == limit
        )
        # If another Stop hook already blocked this turn, wait for a fresh stop
        # instead of consuming this window's single steering slot -- unless the
        # window is exhausted, in which case there is nothing left to steer.
        defer = stop_hook_active and not exhausted

        if (exhausted or crossed) and not already_steered and not defer:
            t["steered_window"] = window if window is not None else int(now)
            t["steered_at"] = now
            t["steered_used"] = used
            t["steered_limit"] = limit
            t["window_resets_at"] = window
            if resume_in is not None:
                t["resume_at"] = now + resume_in
            elif window is not None and window > now:
                t["resume_at"] = window + float(cfg.get("resume_buffer_sec", 90))
            else:
                t["resume_at"] = now + 5 * 3600
            t.pop("resume_sent_at", None)
            t.pop("resume_skipped", None)
            t.pop("resume_failed", None)
            t.pop("resume_attempts", None)

        must_block = crossed and not exhausted and not already_steered and not stop_hook_active
        handoff = str(cwd) + "/" + str(cfg.get("handoff_file", "HANDOFF.md"))
        save_state(st)
        state_snapshot = dict(t)

    if state_snapshot.get("resume_at"):
        ensure_sleeper(sid)

    if must_block:
        reason = render(
            cfg["stop_message"],
            percent=used,
            window=sel["label"],
            handoff_file=cfg.get("handoff_file", "HANDOFF.md"),
            handoff_path=handoff,
            reset_local=fmt_reset(window),
            minutes_left=max(0.0, ((window or now) - now) / 60.0),
        )
        log(f"stop: STEER session={sid} limit={limit} used={used:.1f}% resets_at={window} -> block")
        notify_desktop(
            cfg,
            "codex-auto-startstop",
            f"{sel['label']} at {used:.0f}% - asking Codex to write a handoff report",
        )
        _print_hook_output({"decision": "block", "reason": reason})
    else:
        log(
            f"stop: allow session={sid} limit={limit} used={used:.1f}% window={window} "
            f"steered={already_steered} exhausted={exhausted} active={stop_hook_active}"
        )
    return 0


def cmd_session_start(_args) -> int:
    inp = _read_stdin_json()
    sid = str(inp.get("session_id") or "").strip()
    if not sid:
        return 0
    cfg = load_config()
    with state_lock():
        st = load_state()
        t = dict(st["threads"].get(sid) or {})
    if not t.get("resume_at"):
        return 0
    if t.get("resume_sent_at") or t.get("resume_skipped") or t.get("resume_failed"):
        return 0
    ensure_sleeper(sid)
    log(f"session-start: pending resume for {sid} (resume_at={t.get('resume_at')})")
    if time.time() >= float(t["resume_at"]) - 60:
        ctx = render(
            cfg["session_start_context"],
            window=_limit_label(t.get("steered_limit", "5h")),
            handoff_file=cfg.get("handoff_file", "HANDOFF.md"),
            reset_local=fmt_reset(t.get("window_resets_at")),
        )
        _print_hook_output(
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}
        )
    return 0


# --------------------------------------------------------------------------- status


def cmd_status(args) -> int:
    cfg = load_config()
    with state_lock():
        st = load_state()
    sid = args.thread
    if not sid:
        # newest thread by last_seen or newest rollout
        threads = st.get("threads") or {}
        if threads:
            sid = max(threads, key=lambda k: threads[k].get("last_seen") or 0)
    t = dict((st.get("threads") or {}).get(sid) or {}) if sid else {}
    print(f"codex-auto-startstop v{VERSION}  (state: {state_path()})")
    if not sid:
        print("no session found - pass --thread <session-uuid>")
        return 0
    print(f"session: {sid}")
    print(f"cwd:     {t.get('cwd') or '?'}")
    rollout = Path(t["rollout"]) if t.get("rollout") else find_rollout(sid)
    print(f"rollout: {rollout or '?'}")
    rl, _ = read_last_rate_limits(rollout)
    if rl:
        primary = rl.get("primary") or {}
        secondary = rl.get("secondary") or {}
        used = primary.get("used_percent")
        resets = primary.get("resets_at")
        print(
            f"5h window:   {used}% used, resets {fmt_reset(resets)}"
            f"  (limit reached: {rl.get('rate_limit_reached_type') or 'no'})"
        )
        if secondary:
            print(
                f"weekly:      {secondary.get('used_percent')}% used, "
                f"resets {fmt_reset(secondary.get('resets_at'))}"
            )
        print(f"plan:        {rl.get('plan_type')}")
    else:
        print("5h window:   no rate-limit data found")
        print(
            f"auto-startstop:    steered at {t.get('steered_used')}% "
            f"({t.get('steered_limit', '5h')}; window resets {fmt_reset(t.get('window_resets_at'))})"
        )
    if t.get("resume_at"):
        print(f"resume_at:   {fmt_reset(t['resume_at'])} (epoch {t['resume_at']:.0f})")
        print(f"sleeper pid: {t.get('sleeper_pid')} (alive: {_pid_alive(t.get('sleeper_pid'))})")
        if t.get("resume_sent_at"):
            print(f"resume sent: {fmt_reset(t['resume_sent_at'])} -> {t.get('resume_result', '')[:120]}")
        elif t.get("resume_skipped"):
            print(f"resume skip: {t['resume_skipped']}")
        elif t.get("resume_failed"):
            print("resume:      FAILED (see log)")
    print(
        f"thresholds:  stop_percent={cfg.get('stop_percent')} "
        f"weekly={cfg.get('stop_percent_weekly')} trigger={cfg.get('trigger')} resume={cfg.get('resume')}"
    )

    print(
        f"credits:     use_reset_credits={cfg.get('use_reset_credits')} "
        f"reset_credit_on={cfg.get('reset_credit_on', 'weekly')}"
    )
    if t.get("reset_credit_at"):
        print(
            f"             last reset credit: {fmt_reset(t['reset_credit_at'])} "
            f"({t.get('reset_credit_limit')})"
        )
    lp = log_path()
    if lp.is_file():
        tail = lp.read_text(encoding="utf-8", errors="ignore").splitlines()[-6:]
        if tail:
            print("--- log tail ---")
            for line in tail:
                print(line)
    return 0


# --------------------------------------------------------------------------- install


def _hooks_json_command(cfg: dict, subcommand: str) -> str:
    script = Path(__file__).resolve()
    python = str(cfg.get("python") or sys.executable or "python3")
    installed = auto_startstop_home() / "codex_auto_startstop.py"
    if installed.is_file():
        script = installed
    return f"{shlex.quote(python)} {shlex.quote(str(script))} {subcommand}"


def _merge_hook_entry(data: dict, event: str, command: str) -> bool:
    """Add/refresh our command entry under hooks.<event>. Returns True if changed."""
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault(event, [])
    for group in groups:
        for handler in group.get("hooks", []):
            if handler.get("type") == "command" and handler.get("command") == command:
                handler["timeout"] = 60
                return False
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 60}]})
    return True


def _remove_hook_entry(data: dict, command: str) -> bool:
    changed = False
    hooks = data.get("hooks") or {}
    for event, groups in list(hooks.items()):
        new_groups = []
        for group in groups:
            handlers = [h for h in group.get("hooks", []) if h.get("command") != command]
            if len(handlers) != len(group.get("hooks", [])):
                changed = True
            if handlers:
                group["hooks"] = handlers
                new_groups.append(group)
            elif group.get("hooks"):
                changed = True
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    return changed


def _toml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _seed_trust(toml_path: Path, key: str, trusted_hash: str) -> bool:
    text = toml_path.read_text(encoding="utf-8") if toml_path.is_file() else ""
    header = f"[hooks.state.{_toml_quote(key)}]"
    lines = text.splitlines()
    out = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith("[") and stripped.endswith("]")
        if is_header:
            skipping = stripped == header
            if skipping:
                continue
        if not skipping:
            out.append(line)
    body = "\n".join(out).rstrip("\n")
    block = f"{header}\ntrusted_hash = {_toml_quote(trusted_hash)}"
    new_text = (body + "\n\n" if body else "") + block + "\n"
    if new_text == text:
        return False
    if toml_path.is_file():
        shutil.copy2(toml_path, toml_path.with_suffix(".toml.auto-startstop.bak"))
    _atomic_write_text(toml_path, new_text)
    return True


def _rpc_request(codex_bin: str, requests: list, timeout: float = 30.0):
    """Talk to `codex app-server --listen stdio://` and return id->result map."""
    argv = shlex.split(codex_bin) + ["app-server", "--listen", "stdio://"]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    results = {}
    pending = [req for req in requests if req.get("id") is not None]
    try:
        for req in requests:
            proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()

        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.time() + timeout
        while pending and time.time() < deadline:
            if not sel.select(max(0.1, deadline - time.time())):
                continue
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") is not None and "result" in msg:
                results[msg["id"]] = msg["result"]
                pending = [r for r in pending if r.get("id") != msg["id"]]
                if not pending:
                    break
        return results
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()



def _rpc_init():
    """First JSON-RPC request for a codex app-server session."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "codex-auto-startstop",
                "title": "codex-auto-startstop",
                "version": VERSION,
            },
            "capabilities": {"experimentalApi": True},
        },
    }


def _credit_windows(cfg: dict) -> list:
    """Limit windows whose threshold justifies spending a reset credit."""
    on = str(cfg.get("reset_credit_on", "weekly")).strip().lower()
    if on in ("5h", "5-hour", "primary"):
        return ["5h"]
    if on in ("both", "auto", "either", "all"):
        return ["5h", "weekly"]
    return ["weekly"]


def credit_due(cfg: dict, rl: dict):
    """Return (window_resets_at, label, used) when a reset credit should be spent.

    The window named by `reset_credit_on` must reach its threshold. With
    "weekly" (the default), a near-limit 5-hour window alone never spends a
    credit: it hands off instead. A near-limit weekly window always does.
    """
    near = []
    for name in _credit_windows(cfg):
        key, label, pct_key, default = _LIMIT_DEFS[name]
        w = rl.get(key) or {}
        used = w.get("used_percent")
        if not isinstance(used, (int, float)):
            continue
        threshold = float(cfg.get(pct_key, default))
        if float(used) >= threshold:
            resets = w.get("resets_at")
            near.append(
                (
                    float(used) - threshold,
                    resets if isinstance(resets, (int, float)) else None,
                    label,
                    float(used),
                )
            )
    if not near:
        return None
    near.sort(key=lambda row: row[0], reverse=True)
    _overshoot, resets, label, used = near[0]
    return resets, label, used


def try_redeem_reset_credit(cfg: dict) -> str:
    """Redeem one usage-limit reset credit.

    Returns the backend outcome: reset | nothingToReset | noCredit |
    alreadyRedeemed | unreachable.
    """
    requests = [
        _rpc_init(),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "account/rateLimitResetCredit/consume",
            "params": {"idempotencyKey": str(uuid.uuid4())},
        },
    ]
    try:
        results = _rpc_request(str(cfg.get("codex_bin") or "codex"), requests)
    except Exception as exc:  # noqa: BLE001 - hooks must never raise
        log(f"reset-credit: rpc failed: {exc}")
        return "unreachable"
    return str((results.get(2) or {}).get("outcome") or "unreachable")


def maybe_redeem_reset_credit(cfg: dict, rl: dict, sid: str, now: float) -> bool:
    """Spend a reset credit when the configured window reaches its threshold.

    Returns True when a credit cleared the window (skip the handoff). Records
    one attempt per window, so the RPC never repeats for the same window.
    """
    due = credit_due(cfg, rl)
    with state_lock():
        st = load_state()
        t = st["threads"].setdefault(sid, {})
        if not due:
            # Below every credit threshold: retire markers for a fresh window.
            changed = t.pop("reset_credit_tried", None) is not None
            changed = t.pop("reset_credit_for", None) is not None or changed
            if changed:
                save_state(st)
            return False
        credit_resets, credit_label, credit_used = due
        key = f"{credit_label}:{credit_resets}"
        if t.get("reset_credit_for") == key:
            save_state(st)
            return True
        if t.get("reset_credit_tried") == key:
            save_state(st)
            return False
        t["reset_credit_tried"] = key
        save_state(st)

    outcome = try_redeem_reset_credit(cfg)
    log(
        f"stop: reset-credit outcome={outcome} limit={credit_label} "
        f"used={credit_used:.1f}% window={key}"
    )
    if outcome != "reset":
        if outcome != "unreachable":
            notify_desktop(
                cfg,
                "codex-auto-startstop",
                f"Reset credit not applied ({outcome}) - writing handoff",
            )
        return False
    with state_lock():
        st = load_state()
        t = st["threads"].setdefault(sid, {})
        t["reset_credit_for"] = key
        t["reset_credit_at"] = now
        t["reset_credit_limit"] = credit_label
        for stale in (
            "resume_at",
            "resume_sent_at",
            "resume_skipped",
            "resume_failed",
            "resume_attempts",
            "sleeper_pid",
            "sleeper_for",
            "steered_window",
            "window_resets_at",
        ):
            t.pop(stale, None)
        save_state(st)
    notify_desktop(
        cfg,
        "codex-auto-startstop",
        f"Usage limit reset via credit ({credit_label}) - continuing",
    )
    return True


def fetch_reset_credits(cfg: dict):
    """Read the account's usage-limit reset credits (app-server RPC)."""
    requests = [
        _rpc_init(),
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
    ]
    results = _rpc_request(str(cfg.get("codex_bin") or "codex"), requests)
    return (results.get(2) or {}).get("rateLimitResetCredits")


def fetch_hook_hashes(cfg: dict, cwd: str) -> list:
    codex_bin = str(cfg.get("codex_bin") or "codex")
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "codex-auto-startstop", "title": "codex-auto-startstop", "version": VERSION},
                "capabilities": {"experimentalApi": True},
            },
        },
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {"cwds": [cwd]}},
    ]
    results = _rpc_request(codex_bin, requests)
    result = results.get(2) or {}
    entries = result.get("data") or []
    hooks = []
    for entry in entries:
        if isinstance(entry, dict):
            hooks.extend(entry.get("hooks") or [])
    return hooks


def cmd_credits(_args) -> int:
    cfg = load_config()
    try:
        summary = fetch_reset_credits(cfg)
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(summary, indent=2))
    return 0


def cmd_fetch_hashes(args) -> int:
    cfg = load_config()
    try:
        hooks = fetch_hook_hashes(cfg, args.cwd or os.getcwd())
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        print(json.dumps({"error": str(exc)}))
        return 1
    slim = []
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        slim.append(
            {
                "key": hook.get("key"),
                "eventName": hook.get("event_name") or hook.get("eventName"),
                "currentHash": hook.get("current_hash") or hook.get("currentHash"),
                "trustStatus": hook.get("trust_status") or hook.get("trustStatus"),
                "enabled": hook.get("enabled"),
                "sourcePath": hook.get("source_path") or hook.get("sourcePath"),
                "command": hook.get("command"),
            }
        )
    print(json.dumps(slim, indent=2))
    return 0


def cmd_install(args) -> int:
    cfg = load_config()
    home = codex_home()
    ah = auto_startstop_home()
    ah.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)

    # 1. copy this script into CODEX_HOME/auto-startstop/
    src = Path(__file__).resolve()
    dst = ah / "codex_auto_startstop.py"
    if src != dst:
        shutil.copy2(src, dst)
    dst.chmod(0o755)

    # 2. default config
    if not config_path().exists():
        _atomic_write_text(config_path(), json.dumps(DEFAULT_CONFIG, indent=2))
        print(f"wrote default config      {config_path()}")
    else:
        print(f"kept existing config      {config_path()}")

    # 3. hooks.json
    hj = hooks_path()
    data = {}
    if hj.is_file():
        try:
            data = json.loads(hj.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
            shutil.copy2(hj, hj.with_suffix(".json.auto-startstop.bak"))
        except json.JSONDecodeError:
            print(f"ERROR: {hj} is not valid JSON; aborting install")
            return 1
    data.setdefault("description", "codex-auto-startstop: 5h-limit auto stop/handoff + auto resume")
    stop_cmd = _hooks_json_command(cfg, "stop")
    ss_cmd = _hooks_json_command(cfg, "session-start")
    changed = _merge_hook_entry(data, "Stop", stop_cmd)
    changed = _merge_hook_entry(data, "SessionStart", ss_cmd) or changed
    _atomic_write_text(hj, json.dumps(data, indent=2) + "\n")
    print(f"hooks {'updated' if changed else 'ok'}          {hj}")
    print(f"  Stop         -> {stop_cmd}")
    print(f"  SessionStart -> {ss_cmd}")

    # 4. trust seeding (untrusted hooks do not run)
    print("fetching hook hashes from codex (app-server)...")
    seeded = []
    try:
        hooks = fetch_hook_hashes(cfg, args.cwd or os.getcwd())
    except Exception as exc:  # noqa: BLE001
        hooks = []
        print(f"  rpc failed: {exc}")
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = str(hook.get("command") or "")
        key = hook.get("key")
        current_hash = hook.get("current_hash") or hook.get("currentHash")
        source = str(hook.get("source_path") or hook.get("sourcePath") or "")
        if not key or not current_hash:
            continue
        if str(dst) in command or (str(hj) in source and ("auto_startstop" in command or "auto-startstop" in command)):
            if _seed_trust(home / "config.toml", key, current_hash):
                seeded.append(key)
            print(f"  trusted: {key}")
    if not seeded:
        print(
            "  NOTE: could not seed trust automatically. Hooks will be offered for review\n"
            "  on the next interactive Codex start -> choose \"Trust all and continue\" once.\n"
            "  (Headless runs can use --dangerously-bypass-hook-trust.)"
        )

    print()
    print("Installed. Next steps:")
    print("  - use Codex normally:  codex   (interactive)  /  codex exec ...")
    print(f"  - default steer threshold: {cfg.get('stop_percent')}% of the 5h window")
    print(f"  - tune:  edit {config_path()}")
    print(f"  - check: python3 {dst} status")
    print(f"  - remove: python3 {dst} uninstall")
    return 0


def cmd_uninstall(args) -> int:
    cfg = load_config()
    ah = auto_startstop_home()
    hj = hooks_path()
    if hj.is_file():
        try:
            data = json.loads(hj.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            command = _hooks_json_command(cfg, "stop")
            changed = _remove_hook_entry(data, command)
            changed = _remove_hook_entry(data, _hooks_json_command(cfg, "session-start")) or changed
            if changed:
                shutil.copy2(hj, hj.with_suffix(".json.auto-startstop.bak"))
                _atomic_write_text(hj, json.dumps(data, indent=2) + "\n")
                print(f"removed auto-startstop hooks from {hj}")
            else:
                print(f"no auto-startstop hooks found in {hj}")
    if args.purge and ah.is_dir():
        shutil.rmtree(ah)
        print(f"removed {ah}")
    else:
        print(f"left state in {ah} (use --purge to remove)")
    print("note: trust entries in config.toml are harmless; delete [hooks.state] blocks to tidy up.")
    return 0


# --------------------------------------------------------------------------- main


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="codex-auto-startstop", description=__doc__.splitlines()[1])
    parser.add_argument("--version", action="version", version=f"codex-auto-startstop {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stop", help="Codex Stop hook entry point (stdin JSON)").set_defaults(func=cmd_stop)
    sub.add_parser("session-start", help="Codex SessionStart hook entry point (stdin JSON)").set_defaults(
        func=cmd_session_start
    )

    p_sleep = sub.add_parser("sleeper", help="background resume helper")
    p_sleep.add_argument("--thread", required=True)
    p_sleep.add_argument("--once", action="store_true")
    p_sleep.add_argument("--max-hours", type=float, default=0)
    p_sleep.set_defaults(func=cmd_sleeper)

    p_check = sub.add_parser("resume-check", help="one-shot resume due-check")
    p_check.add_argument("--thread", required=True)
    p_check.set_defaults(func=cmd_resume_check, once=True, max_hours=0)

    p_status = sub.add_parser("status", help="show rate limits and auto-startstop state")
    p_status.add_argument("--thread")
    p_status.set_defaults(func=cmd_status)

    p_fh = sub.add_parser("fetch-hashes", help="ask Codex for hook hashes (RPC)")
    p_fh.add_argument("--cwd")
    p_fh.set_defaults(func=cmd_fetch_hashes)

    p_cred = sub.add_parser("credits", help="show available usage-limit reset credits (RPC)")
    p_cred.set_defaults(func=cmd_credits)

    p_inst = sub.add_parser("install", help="install hooks.json + trust")
    p_inst.add_argument("--cwd")
    p_inst.set_defaults(func=cmd_install)

    p_un = sub.add_parser("uninstall", help="remove hooks")
    p_un.add_argument("--purge", action="store_true")
    p_un.set_defaults(func=cmd_uninstall)

    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except Exception:  # noqa: BLE001 - hooks must never break a session
        log("FATAL: " + traceback.format_exc())
        if args.command in ("stop", "session-start"):
            return 0
        raise


if __name__ == "__main__":
    sys.exit(main())
