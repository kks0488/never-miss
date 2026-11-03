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
import subprocess
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
    result["channel_sessions"] = string_list(config.get("channel_sessions", []))
    result["source"] = config.get("source", "")
    result["redact_secrets"] = config.get("redact_secrets", False)
    if not isinstance(result["source"], str) or not isinstance(result["redact_secrets"], bool):
        raise ValueError("source must be a string and redact_secrets a boolean")
    ledger = config.get("session_ledger")
    if ledger is not None:
        if not isinstance(ledger, str) or not ledger:
            raise ValueError("session_ledger must be a nonempty path")
        names = string_list(config.get("session_names", []))
        entries = json.loads(Path(ledger).expanduser().read_text(encoding="utf-8"))
        if not isinstance(entries, dict) or not isinstance(entries.get("sessions"), list):
            raise ValueError("session ledger requires a sessions array")
        for entry in entries["sessions"]:
            if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str)
                                                 for k in ("name", "session_id")):
                raise ValueError("ledger entries require name and session_id strings")
            if entry["session_id"] and (not names or entry["name"] in names):
                result["sessions"].append(entry["session_id"])
        if not result["sessions"]:
            raise ValueError("session ledger selected no sessions")
    return result


def redact(text):
    """Best-effort patterns only; no vault lookup or secret fingerprints."""
    text = re.sub(r"(?<!\d)(?:\d{6}(?:[ \t]*-[ \t]*|[ \t]+)[1-4]\d{6}|"
                  r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[1-4]\d{6})(?!\d)",
                  "[REDACTED]", text)

    def card(match):
        digits = re.sub(r"\D", "", match[0])
        values = [int(c) for c in digits[::-1]]
        total = sum(v if i % 2 == 0 else (2 * v // 10 + 2 * v % 10)
                    for i, v in enumerate(values))
        return "[REDACTED]" if 13 <= len(digits) <= 19 and total % 10 == 0 else match[0]

    text = re.sub(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])", card, text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{30,}|"
                  r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|"
                  r"AIza[0-9A-Za-z_-]{30,}|xox[bpa]-[A-Za-z0-9-]{10,})",
                  "[REDACTED]", text)
    # Do not consume a following line as the value of an empty label.
    text = re.sub(r"((?<!\w)(?:password|passwd|pw|pin|otp|api[_ -]?key|token|"
                  r"비밀번호|비번|암호|인증번호|인증 번호)(?![A-Za-z_])[ \t]*(?:은|는|이|가|[:=：])?[ \t]*)"
                  r"(\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s,;:=：][^\s,;]*)",
                  lambda m: m[1] + "[REDACTED]",
                  text, flags=re.I)
    parts = re.split(r"(\s+)", text)
    indices = [i for i, part in enumerate(parts) if part and not part.isspace()]
    words = [parts[i] for i in indices]
    for n, i in enumerate(indices):
        core = words[n].strip("\"'()[]{}<>,.;:")
        nearby = " ".join(words[max(0, n - 3):n] + words[n + 1:n + 4])
        if (len(core) >= 8 and re.fullmatch(r"[A-Za-z0-9!#$%^&*+=~?-]+", core)
                and re.search(r"[A-Za-z]", core) and re.search(r"\d", core)
                and (re.search(r"[^A-Za-z0-9-]", core) or
                     (re.search(r"[a-z]", core) and re.search(r"[A-Z]", core)))
                and re.search(r"password|passwd|login|비번|비밀번호|암호|로그인", nearby, re.I)):
            parts[i] = words[n].replace(core, "[REDACTED]")
    return "".join(parts)


@contextmanager
def locked(root, exclusive=True, name=".lock"):
    import fcntl
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / name, os.O_CREAT | os.O_RDWR, 0o600)
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
    config = configuration(root)
    if payload.get("session_id") in config["channel_sessions"]:
        bodies = re.findall(r"<channel\b[^>]*>(.*?)</channel>", payload["prompt"], re.S)
        if not bodies:
            return
        payload = dict(payload, prompt="\n".join(bodies))
    if not allowed(payload, config):
        return
    said, session, cwd = payload["prompt"], payload.get("session_id", ""), payload.get("cwd", "")
    if config["redact_secrets"]:
        said = redact(said)
    prompt_id = payload.get("prompt_id")
    source = config["source"]
    identity_text = (json.dumps([source, session, prompt_id]) if source else f"{session}|{prompt_id}")
    identity = ("prompt-" + hashlib.sha256(identity_text.encode()).hexdigest()
                if prompt_id else "local-" + uuid.uuid4().hex)
    with locked(root):
        stamp = now()
        t = stamp.isoformat(timespec="microseconds")
        key = record_key(t, said, source)
        for previous in rows(root / "inbox.jsonl"):
            if previous["id"] == identity or previous["key"] == key:
                return
            # Older clients lack prompt_id. Collapse immediate identical retries only.
            if not prompt_id and previous["id"].startswith("local-") and (
                    previous["said"], previous["session"], previous["cwd"],
                    previous.get("source", "")) == (said, session, cwd, source):
                age = (stamp - datetime.fromisoformat(previous["t"].replace("Z", "+00:00"))).total_seconds()
                if 0 <= age <= RETRY_WINDOW:
                    return
        row = {"id": identity, "t": t, "said": said,
               "session": session, "cwd": cwd, "key": key}
        if source:
            row["source"] = source
        append(root / "inbox.jsonl", row)


def record_key(t, said, source=""):
    value = json.dumps([source, t, said], ensure_ascii=False) if source else f"{t}|{said}"
    return hashlib.sha1(value.encode()).hexdigest()[:12]


def collect(stream, root, source):
    """Import JSONL snapshots transported by the caller, without remote execution."""
    if not source.strip():
        raise ValueError("source must be nonempty")
    config = configuration(root)
    incoming = []
    for number, line in enumerate(stream, 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k]
                                               for k in ("id", "t", "said")):
                raise ValueError()
            if any(not isinstance(row.get(k, ""), str) for k in ("session", "cwd")):
                raise ValueError()
            if datetime.fromisoformat(row["t"].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError(f"import line {number}: expected id, timezone timestamp, said strings") from None
        payload = {"prompt": row["said"], "session_id": row.get("session", ""),
                   "cwd": row.get("cwd", "")}
        if not allowed(payload, config):
            continue
        said = redact(row["said"]) if config["redact_secrets"] else row["said"]
        # Namespace the stable originating ID, never generate a new time during a replay.
        identity = "import-" + hashlib.sha256(json.dumps([source, row["id"]]).encode()).hexdigest()
        incoming.append({"id": identity, "t": row["t"], "said": said,
                         "session": payload["session_id"], "cwd": payload["cwd"],
                         "source": source, "key": record_key(row["t"], said, source)})
    with locked(root):
        existing = rows(root / "inbox.jsonl")
        identities = {row["id"] for row in existing}
        keys = {row["key"] for row in existing}
        pending = []
        for row in incoming:
            if row["id"] in identities:
                continue
            if row["key"] in keys:
                raise ValueError("import key collision; original inbox left intact")
            pending.append(row)
            identities.add(row["id"])
            keys.add(row["key"])
        for row in pending:
            append(root / "inbox.jsonl", row)
    return len(pending)


def dispatch(root):
    """Reconcile the durable inbox into an outbox; retry only unacknowledged records."""
    path = root / "config.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    command = config.get("export_command") if isinstance(config, dict) else None
    if (not isinstance(command, list) or not command or
            any(not isinstance(arg, str) or not arg for arg in command)):
        raise ValueError("export_command must be a nonempty argument array")
    timeout = config.get("export_timeout", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise ValueError("export_timeout must be greater than zero and at most 300 seconds")
    sent = failed = 0
    # Separate lock: a slow receiver never holds up prompt capture or listing.
    with locked(root, name=".dispatch.lock"):
        with locked(root):
            records = rows(root / "inbox.jsonl")
            queued = rows(root / "outbox.jsonl")
            delivered = {row["idempotency_key"] for row in rows(root / "deliveries.jsonl")
                         if row["status"] == "sent"}
            known = {row["record"]["id"] for row in queued}
            for row in records:
                if row["id"] not in known:
                    entry = {"idempotency_key": "never-miss:" + row["id"], "record": row}
                    append(root / "outbox.jsonl", entry)
                    queued.append(entry)
                    known.add(row["id"])
        for entry in queued:
            token = entry["idempotency_key"]
            if token in delivered:
                continue
            env = dict(os.environ, NEVER_MISS_IDEMPOTENCY_KEY=token)
            try:
                result = subprocess.run(command, input=json.dumps(entry, ensure_ascii=False).encode(),
                                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=timeout, check=False)
                status = "sent" if result.returncode == 0 else "failed"
                detail = "exit:" + str(result.returncode)
            except subprocess.TimeoutExpired:
                status, detail = "failed", "timeout; receiver outcome unknown"
            except OSError:
                status, detail = "failed", "command could not start"
            with locked(root):
                append(root / "deliveries.jsonl", {"idempotency_key": token, "t": now().isoformat(),
                                                   "status": status, "detail": detail})
            if status == "sent":
                delivered.add(token)
                sent += 1
            else:
                failed += 1
    print(f"sent {sent}, pending {failed}")
    return 1 if failed else 0


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
    collection = commands.add_parser("collect", help="merge JSONL records from stdin")
    collection.add_argument("--source", required=True, help="stable unique origin label")
    commands.add_parser("dispatch", help="deliver pending records through configured command")
    args = parser.parse_args()
    if args.command == "export" and args.format == "agent-cards" and not (args.agent and args.due):
        parser.error("agent-cards export requires --agent and --due")
    try:
        if args.command == "install":
            install(args.project)
            return 0
        root = state_dir()
        if args.command == "collect":
            print(f"collected {collect(sys.stdin, root, args.source)}")
            return 0
        if args.command == "dispatch":
            return dispatch(root)
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
                   and (cutoff is None or datetime.fromisoformat(row["t"].replace("Z", "+00:00")) >= cutoff)]
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
