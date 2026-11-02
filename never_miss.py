#!/usr/bin/env python3
"""Record submitted instructions and track their completion. Python 3.9+, POSIX."""

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid


DEFAULT_SKIP_PREFIXES = (
    "<cross-session-message", "cross-session-message",
    "<task-notification", "task-notification",
    "<system-reminder", "<system-notification", "system notification",
    "[system notification", "[scheduled", "[cron", "[loop",
    "<scheduled-task", "scheduled task:", "cron job:",
    "<teammate-message", "[session message", "[task notification",
)
RETRY_WINDOW = 2


def now():
    return datetime.now(timezone.utc)


def state_dir():
    return Path(os.environ.get("NEVER_MISS_DIR") or
                "~/.local/state/never-miss").expanduser()


def string_list(value):
    if isinstance(value, str):
        if value.lstrip().startswith("["):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value.splitlines()
        else:
            value = value.splitlines()
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError("configuration lists must contain strings")
    return [x.strip() for x in value if x.strip()]


def configuration(root):
    path = root / "config.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("config.json must be an object")
    result = {}
    for field in ("sessions", "cwds"):
        result[field] = string_list(os.environ.get("NEVER_MISS_" + field.upper(),
                                                   config.get(field, [])))
    # Prefix additions never disable the built-in exclusions.
    result["skip_prefixes"] = (string_list(config.get("skip_prefixes", [])) +
                               string_list(os.environ.get("NEVER_MISS_SKIP_PREFIXES", "")))
    return result


@contextmanager
def locked(root, exclusive=True):
    import fcntl
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as handle:
        deadline = time.monotonic() + 1
        while True:
            try:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(handle, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("state lock unavailable after one second")
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def rows(path):
    if not path.exists():
        return []
    result = []
    with path.open(encoding="utf-8", newline="\n") as handle:
        for number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("record must be an object")
            except ValueError as exc:
                raise ValueError(f"{path.name} line {number}: invalid record") from exc
            result.append(row)
    return result


def append(path, row):
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def error_log(exc):
    try:
        root = state_dir()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # One append write, independent of the main lock so lock failures can be logged.
        data = (json.dumps({"t": now().isoformat(), "error": type(exc).__name__,
                            "detail": str(exc)}, ensure_ascii=True) + "\n").encode()
        fd = os.open(root / "errors.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except BaseException:
        pass


def allowed(payload, config):
    said = payload["prompt"]
    if not said.strip():
        return False
    start = said.lstrip().casefold()
    if any(start.startswith(prefix.casefold()) for prefix in
           DEFAULT_SKIP_PREFIXES + tuple(config["skip_prefixes"])):
        return False
    if config["sessions"] and payload.get("session_id", "") not in config["sessions"]:
        return False
    if config["cwds"]:
        if not payload.get("cwd"):
            return False
        cwd = Path(payload["cwd"]).expanduser().resolve()
        roots = [Path(p).expanduser().resolve() for p in config["cwds"]]
        if not any(cwd == root or root in cwd.parents for root in roots):
            return False
    return True


def record(payload, root):
    if not isinstance(payload, dict) or not isinstance(payload.get("prompt"), str):
        raise ValueError("hook input must be an object with a string prompt")
    if payload.get("hook_event_name", "UserPromptSubmit") != "UserPromptSubmit":
        return
    for field in ("session_id", "cwd", "prompt_id"):
        if field in payload and not isinstance(payload[field], str):
            raise ValueError(f"{field} must be a string")
    if not allowed(payload, configuration(root)):
        return
    said, session, cwd = payload["prompt"], payload.get("session_id", ""), payload.get("cwd", "")
    prompt_id = payload.get("prompt_id")
    identity = ("prompt-" + hashlib.sha256(f"{session}|{prompt_id}".encode()).hexdigest()
                if prompt_id else "local-" + uuid.uuid4().hex)
    with locked(root):
        stamp = now()
        t = stamp.isoformat(timespec="microseconds")
        key = hashlib.sha1(f"{t}|{said}".encode()).hexdigest()[:12]
        for previous in rows(root / "inbox.jsonl"):
            if previous["id"] == identity or previous["key"] == key:
                return
            # Older clients lack prompt_id. Collapse immediate identical retries only.
            if not prompt_id and previous["id"].startswith("local-") and (
                    previous["said"], previous["session"], previous["cwd"]) == (said, session, cwd):
                age = (stamp - datetime.fromisoformat(previous["t"])).total_seconds()
                if 0 <= age <= RETRY_WINDOW:
                    return
        append(root / "inbox.jsonl", {"id": identity, "t": t, "said": said,
                                      "session": session, "cwd": cwd, "key": key})


def hook():
    try:
        if len(sys.argv) != 2:
            raise ValueError("hook takes no arguments")
        record(json.load(sys.stdin), state_dir())
    except BaseException as exc:
        error_log(exc)
    return 0


def snapshot(root):
    if not root.exists():
        return [], set()
    with locked(root, exclusive=False):
        return rows(root / "inbox.jsonl"), {row["key"] for row in rows(root / "done.jsonl")}


def duration(value):
    match = re.fullmatch(r"(\d+)([smhdw])", value)
    if not match:
        raise argparse.ArgumentTypeError("use a duration such as 30m, 2h, 1d, or 1w")
    try:
        return timedelta(seconds=int(match[1]) * {"s": 1, "m": 60, "h": 3600,
                                                  "d": 86400, "w": 604800}[match[2]])
    except OverflowError as exc:
        raise argparse.ArgumentTypeError("duration is too large") from exc


def deadline(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use a date in YYYY-MM-DD format") from exc


def single_line(value):
    return " ".join(value.split())


def markdown(value):
    return re.sub(r"([\\`*_{}\[\]<>()#!|~])", r"\\\1", single_line(value))


def install(project):
    target = ".claude/settings.json" if project else "~/.claude/settings.json"
    script = Path(__file__).resolve()
    command = shlex.join([sys.executable, str(script), "hook"])
    print(f"Merge this snippet into {target}; keep existing settings and hooks.")
    print("Replace the script path if you move this file. No files have been changed.")
    print(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": command, "timeout": 3}
    ]}]}}, indent=2))


def main():
    # Handle hook before argparse so even bad hook arguments stay silent and succeed.
    if sys.argv[1:2] == ["hook"]:
        return hook()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("hook", help="read hook JSON silently from stdin")
    listing = commands.add_parser("list", help="show captured instructions")
    listing.add_argument("--since", type=duration)
    listing.add_argument("--open", action="store_true")
    listing.add_argument("--json", action="store_true")
    done = commands.add_parser("done", help="append a completion record")
    done.add_argument("key")
    done.add_argument("--note", default="")
    export = commands.add_parser("export", help="print open instructions without executing them")
    export.add_argument("--format", choices=("markdown", "agent-cards"), required=True)
    export.add_argument("--agent", help="agent-cards assignee")
    export.add_argument("--due", type=deadline, help="agent-cards deadline, YYYY-MM-DD")
    installation = commands.add_parser("install", help="print settings; never edit them")
    installation.add_argument("--project", action="store_true")
    args = parser.parse_args()
    if args.command == "export" and args.format == "agent-cards" and not (args.agent and args.due):
        parser.error("agent-cards export requires --agent and --due")
    try:
        if args.command == "install":
            install(args.project)
            return 0
        root = state_dir()
        if args.command == "done":
            with locked(root):
                records = rows(root / "inbox.jsonl")
                completed = {row["key"] for row in rows(root / "done.jsonl")}
                if not any(row["key"] == args.key for row in records):
                    raise ValueError("unknown key; copy the full key from list")
                if args.key not in completed:
                    append(root / "done.jsonl", {"key": args.key, "t": now().isoformat(),
                                               "note": args.note})
            print(f"{args.key} done")
            return 0
        records, completed = snapshot(root)
        cutoff = (now() - args.since
                  if args.command == "list" and args.since is not None else None)
        records = [row for row in records
                   if not ((args.command == "export" or args.open) and row["key"] in completed)
                   and (cutoff is None or datetime.fromisoformat(row["t"]) >= cutoff)]
        if args.command == "list":
            if args.json:
                print(json.dumps(records, ensure_ascii=False, indent=2))
            else:
                for row in records:
                    status = "done" if row["key"] in completed else "open"
                    print(f"{row['key']} | {status} | {row['t']} | {single_line(row['said'])}")
        else:
            for row in records:
                if args.format == "markdown":
                    print(f"- [ ] {markdown(row['said'])} ({row['key']})")
                else:
                    print(shlex.join(["python3", "agent_cards.py", "add",
                                      "--due", args.due, "--note", f"never-miss:{row['key']}",
                                      "--", args.agent, single_line(row["said"])]))
        return 0
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        print(f"never-miss: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
