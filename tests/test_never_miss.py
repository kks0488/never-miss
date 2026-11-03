from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "never_miss.py"
SPEC = importlib.util.spec_from_file_location("never_miss", SCRIPT)
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class NeverMissTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("NEVER_MISS_")}
        self.env["NEVER_MISS_DIR"] = str(self.root)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def cli(self, *args, raw=None, payload=None, env=None):
        if payload is not None:
            raw = json.dumps(payload)
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=raw,
                              text=True, capture_output=True, env=env or self.env, timeout=10)

    def payload(self, said="Write the release notes", **fields):
        return {"hook_event_name": "UserPromptSubmit", "prompt": said,
                "session_id": "session-alpha", "cwd": "/workspace/sample", **fields}

    def hook(self, said="Write the release notes", **fields):
        result = self.cli("hook", payload=self.payload(said, **fields))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        return result

    def inbox(self):
        path = self.root / "inbox.jsonl"
        return [json.loads(line) for line in path.read_text().split("\n") if line] if path.exists() else []

    def config(self, value):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "config.json").write_text(json.dumps(value))

    def test_human_prompt_schema_and_exact_text(self):
        said = "  First instruction\n두 번째 지시\t\"quoted\"  "
        self.hook(said)
        row = self.inbox()[0]
        self.assertEqual(set(row), {"id", "t", "said", "session", "cwd", "key"})
        self.assertEqual(row["said"], said)
        self.assertEqual(row["session"], "session-alpha")
        self.assertEqual(row["cwd"], "/workspace/sample")
        self.assertEqual(row["key"], hashlib.sha1(f"{row['t']}|{said}".encode()).hexdigest()[:12])
        self.assertIsNotNone(datetime.fromisoformat(row["t"]).tzinfo)
        self.assertEqual(len((self.root / "inbox.jsonl").read_text().splitlines()), 1)

    def test_notification_and_schedule_prefixes(self):
        for said in ("<cross-session-message sender='helper'>hello", "cross-session-message: hi",
                     "<task-notification>finished", "task-notification: finished",
                     "SYSTEM NOTIFICATION: alert", "<system-reminder>timer",
                     "<system-notification>timer", "[system notification] timer",
                     "[scheduled task] check", "[cron] check", "[loop] check",
                     "<scheduled-task>check", "Scheduled task: check", "Cron job: check",
                     "<teammate-message>hello", "[session message] hello",
                     "[task notification] done", " \n<TASK-NOTIFICATION>done"):
            with self.subTest(said=said):
                self.hook(said)
        self.assertEqual(self.inbox(), [])

    def test_mentions_in_human_text_are_preserved(self):
        self.hook("Explain <task-notification> and scheduled task: headers")
        self.assertEqual(len(self.inbox()), 1)

    def test_empty_prompts(self):
        for said in ("", " \n\t"):
            self.hook(said)
        self.assertEqual(self.inbox(), [])

    def test_bad_json_and_types_are_silent_and_logged(self):
        for raw in ("{broken", "", "null", "[]", '{"prompt": 7}',
                    '{"prompt":"hi","cwd":{}}'):
            with self.subTest(raw=raw):
                result = self.cli("hook", raw=raw)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.inbox(), [])
        errors = (self.root / "errors.jsonl").read_text().splitlines()
        self.assertEqual(len(errors), 6)
        self.assertTrue(all("error" in json.loads(line) for line in errors))

    def test_wrong_event_is_ignored(self):
        self.hook("A notification", hook_event_name="Notification")
        self.assertEqual(self.inbox(), [])

    def test_immediate_duplicate_without_prompt_id(self):
        self.hook()
        self.hook()
        self.assertEqual(len(self.inbox()), 1)

    def test_prompt_id_retry_survives_later_timestamp(self):
        first = datetime(2031, 5, 6, 12, tzinfo=timezone.utc)
        payload = self.payload(prompt_id="prompt-alpha")
        with patch.object(APP, "now", return_value=first):
            APP.record(payload, self.root)
        with patch.object(APP, "now", return_value=first + timedelta(days=1)):
            APP.record(payload, self.root)
        self.assertEqual(len(self.inbox()), 1)

    def test_distinct_prompt_ids_keep_intentional_repetitions(self):
        self.hook(prompt_id="prompt-alpha")
        self.hook(prompt_id="prompt-beta")
        self.assertEqual(len(self.inbox()), 2)

    def test_legacy_repetition_after_window_is_kept(self):
        first = datetime(2031, 5, 6, 12, tzinfo=timezone.utc)
        with patch.object(APP, "now", return_value=first):
            APP.record(self.payload(), self.root)
        with patch.object(APP, "now", return_value=first + timedelta(seconds=3)):
            APP.record(self.payload(), self.root)
        self.assertEqual(len(self.inbox()), 2)

    def test_concurrent_processes_do_not_mix_or_lose_lines(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.cli("hook", payload=self.payload(
                f"Instruction {i}", prompt_id=f"prompt-{i}")), range(32)))
        self.assertTrue(all((r.returncode, r.stdout, r.stderr) == (0, "", "") for r in results))
        records = self.inbox()
        self.assertEqual(len(records), 32)
        self.assertEqual(len({r["key"] for r in records}), 32)
        self.assertEqual({r["said"] for r in records}, {f"Instruction {i}" for i in range(32)})

    def test_concurrent_duplicate_is_saved_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.cli("hook", payload=self.payload(
                prompt_id="same-submission")), range(16)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertEqual(len(self.inbox()), 1)

    def test_session_allowlist_and_environment_override(self):
        self.config({"sessions": ["session-other"]})
        self.hook()
        self.assertEqual(self.inbox(), [])
        self.env["NEVER_MISS_SESSIONS"] = '["session-alpha"]'
        self.hook()
        self.assertEqual(len(self.inbox()), 1)

    def test_cwd_allowlist_uses_directory_boundaries(self):
        self.env["NEVER_MISS_CWDS"] = '["/workspace/sample"]'
        self.hook("Root", cwd="/workspace/sample")
        self.hook("Child", cwd="/workspace/sample/src")
        self.hook("Sibling", cwd="/workspace/sample-other")
        self.hook("Missing", cwd="")
        self.assertEqual([r["said"] for r in self.inbox()], ["Root", "Child"])

    def test_combined_filters_both_apply(self):
        self.config({"sessions": ["session-alpha"], "cwds": ["/workspace/sample"]})
        self.hook("Wrong session", session_id="session-beta")
        self.hook("Wrong directory", cwd="/workspace/other")
        self.hook("Both match")
        self.assertEqual([r["said"] for r in self.inbox()], ["Both match"])

    def test_custom_prefixes_extend_defaults(self):
        self.config({"skip_prefixes": ["[daily check]"]})
        self.env["NEVER_MISS_SKIP_PREFIXES"] = '["Timer tick:"]'
        self.hook("[daily check] inspect")
        self.hook("Timer tick: inspect")
        self.hook("<task-notification>done")
        self.hook("Please inspect")
        self.assertEqual([r["said"] for r in self.inbox()], ["Please inspect"])

    def test_newline_lists_and_empty_filters(self):
        self.env["NEVER_MISS_SESSIONS"] = "session-alpha\nsession-beta"
        self.hook(session_id="session-beta")
        self.env["NEVER_MISS_SESSIONS"] = ""
        self.hook("Another session", session_id="session-gamma")
        self.assertEqual(len(self.inbox()), 2)

    def test_bracket_prefixed_newline_skip_prefixes(self):
        for index, prefixes in enumerate(("[daily check]", "[daily check]\nTimer tick:")):
            with self.subTest(prefixes=prefixes):
                self.env["NEVER_MISS_SKIP_PREFIXES"] = prefixes
                self.hook("[daily check] inspect")
                if "\n" in prefixes:
                    self.hook("Timer tick: inspect")
                self.hook(f"Please inspect {index}")
                self.assertEqual([r["said"] for r in self.inbox()],
                                 [f"Please inspect {i}" for i in range(index + 1)])
        self.assertFalse((self.root / "errors.jsonl").exists())

    def test_unicode_separators_preserve_listing_and_further_saves(self):
        expected = []
        for separator in ("\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                said = f"first{separator}second"
                self.hook(said)
                expected.append(said)
                result = self.cli("list", "--json")
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual([r["said"] for r in json.loads(result.stdout)], expected)
                followup = f"Additional instruction {ord(separator)}"
                self.hook(followup)
                expected.append(followup)
                result = self.cli("list", "--json")
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual([r["said"] for r in json.loads(result.stdout)], expected)
        self.assertFalse((self.root / "errors.jsonl").exists())

    def test_bad_config_does_not_block(self):
        self.config({"sessions": [123]})
        self.hook()
        self.assertEqual(self.inbox(), [])
        self.assertTrue((self.root / "errors.jsonl").exists())

    def test_write_failure_is_silent_and_logged(self):
        self.root.mkdir()
        (self.root / "inbox.jsonl").mkdir()
        self.hook()
        self.assertTrue((self.root / "errors.jsonl").exists())

    def test_unwritable_error_destination_still_succeeds(self):
        self.root.write_text("not a directory")
        self.hook()

    def test_contended_lock_times_out_without_blocking_prompt(self):
        self.root.mkdir()
        with (self.root / ".lock").open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.hook()
        self.assertEqual(self.inbox(), [])
        self.assertIn("TimeoutError", (self.root / "errors.jsonl").read_text())

    def test_bad_hook_arguments_remain_silent(self):
        result = self.cli("hook", "--unexpected", raw="{}")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_corrupt_existing_log_is_preserved_and_reported(self):
        self.root.mkdir()
        path = self.root / "inbox.jsonl"
        path.write_text('{"incomplete":')
        self.hook()
        self.assertEqual(path.read_text(), '{"incomplete":')
        self.assertTrue((self.root / "errors.jsonl").exists())
        self.assertEqual(self.cli("list").returncode, 1)

    def test_list_done_and_open_preserve_original(self):
        self.hook("First")
        self.hook("Second")
        original = (self.root / "inbox.jsonl").read_bytes()
        key = self.inbox()[0]["key"]
        result = self.cli("done", key, "--note", "Checked docs/release.md")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(key + " done", result.stdout)
        self.assertEqual((self.root / "inbox.jsonl").read_bytes(), original)
        self.assertEqual(len(json.loads(self.cli("list", "--json").stdout)), 2)
        opened = json.loads(self.cli("list", "--open", "--json").stdout)
        self.assertEqual([r["said"] for r in opened], ["Second"])
        self.assertIn("| done |", self.cli("list").stdout)
        self.assertEqual(self.cli("done", key).returncode, 0)
        done = [json.loads(line) for line in (self.root / "done.jsonl").read_text().splitlines()]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["note"], "Checked docs/release.md")

    def test_unknown_done_key_fails(self):
        result = self.cli("done", "missing-key")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / "done.jsonl").exists())

    def test_since_filter_and_invalid_duration(self):
        with patch.object(APP, "now", return_value=datetime.now(timezone.utc) - timedelta(hours=3)):
            APP.record(self.payload("Old"), self.root)
        self.hook("Recent")
        result = self.cli("list", "--since", "2h", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r["said"] for r in json.loads(result.stdout)], ["Recent"])
        self.assertEqual(json.loads(self.cli("list", "--since", "0s", "--json").stdout), [])
        self.assertEqual(self.cli("list", "--since", "yesterday").returncode, 2)

    def test_markdown_export_is_open_only_and_one_line(self):
        self.hook("Completed")
        self.cli("done", self.inbox()[0]["key"])
        self.hook("Review [draft]\nthen <publish>")
        result = self.cli("export", "--format", "markdown")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Completed", result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertIn(r"- [ ] Review \[draft\] then \<publish\>", result.stdout)

    def test_agent_cards_export_quotes_shell_text_and_requires_options(self):
        marker = Path(self.temp.name) / "must-not-exist"
        said = f"--draft 'quoted' $(touch {marker})\nnext line"
        self.hook(said)
        result = self.cli("export", "--format", "agent-cards", "--agent", "helper",
                          "--due", "2031-05-10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(shlex.split(result.stdout), ["python3", "agent_cards.py", "add",
                         "--due", "2031-05-10", "--note", f"never-miss:{self.inbox()[0]['key']}",
                         "--", "helper", " ".join(said.split())])
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(self.cli("export", "--format", "agent-cards").returncode, 2)
        self.assertEqual(self.cli("export", "--format", "agent-cards", "--agent", "helper",
                                  "--due", "bad-date").returncode, 2)

    def test_empty_commands_need_no_state(self):
        self.assertEqual(json.loads(self.cli("list", "--open", "--json").stdout), [])
        self.assertEqual(self.cli("export", "--format", "markdown").stdout, "")
        self.assertFalse(self.root.exists())

    def test_install_only_prints_valid_settings(self):
        for args, target in (([], "~/.claude/settings.json"), (["--project"], ".claude/settings.json")):
            result = self.cli("install", *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(target, result.stdout)
            settings = json.loads(result.stdout[result.stdout.index("{"):])
            hook = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]
            self.assertEqual(hook["type"], "command")
            self.assertEqual(shlex.split(hook["command"]), [sys.executable, str(SCRIPT), "hook"])
        self.assertFalse(self.root.exists())

    def test_example_settings_structure(self):
        settings = json.loads((SCRIPT.parent / "examples" / "settings.json").read_text())
        entry = settings["hooks"]["UserPromptSubmit"][0]
        self.assertNotIn("matcher", entry)
        self.assertEqual(entry["hooks"][0]["type"], "command")

    def test_state_files_are_private(self):
        self.hook()
        self.assertEqual((self.root.stat().st_mode & 0o777), 0o700)
        for name in ("inbox.jsonl", ".lock"):
            self.assertEqual(((self.root / name).stat().st_mode & 0o777), 0o600)


    def test_redaction_is_optional_and_preserves_layout(self):
        secret = "Example" + "Password42!"
        said = f"  password: {secret}\n\tkeep this  "
        self.hook(said, prompt_id="unmasked")
        self.config({"redact_secrets": True})
        self.hook(said, prompt_id="masked")
        self.assertEqual(self.inbox()[0]["said"], said)
        self.assertEqual(self.inbox()[1]["said"], "  password: [REDACTED]\n\tkeep this  ")
        self.assertEqual(len(self.inbox()), 2)

    def test_redaction_patterns_and_luhn_false_positive(self):
        card = "4111 " + "1111 " * 2 + "1111"
        rrn = "900101" + "-1234567"
        key = "sk-" + "a1B2" * 8
        said = f"{card}\n{rrn}\n{key}\n비밀번호='sample value'"
        self.config({"redact_secrets": True})
        self.hook(said)
        self.assertEqual(self.inbox()[0]["said"],
                         "[REDACTED]\n[REDACTED]\n[REDACTED]\n비밀번호=[REDACTED]")
        self.assertEqual(APP.redact("4111 1111 1111 1112"), "4111 1111 1111 1112")
        self.assertEqual(APP.redact("password:\nordinary instruction"),
                         "password:\nordinary instruction")
        self.assertEqual(APP.redact("passwords reviewed"), "passwords reviewed")
        self.assertEqual(APP.redact(APP.redact(said)), APP.redact(said))

    def test_redaction_masks_password_before_hint(self):
        secret = "Mixed" + "Case123!"
        self.assertEqual(APP.redact(f"({secret}) for login"), "([REDACTED]) for login")
        self.assertEqual(APP.redact("ORDER123456 for login"), "ORDER123456 for login")

    def test_redacted_replay_keeps_id_and_has_no_raw_text_in_state(self):
        self.config({"redact_secrets": True})
        secret = "Private" + "Value123!"
        self.hook(f"password={secret}", prompt_id="secret-input")
        self.hook(f"password={secret}", prompt_id="secret-input")
        self.assertEqual(len(self.inbox()), 1)
        for path in self.root.iterdir():
            if path.is_file():
                self.assertNotIn(secret, path.read_text())

    def test_invalid_redaction_config_is_silent_and_logged(self):
        self.config({"redact_secrets": "yes"})
        self.hook()
        self.assertEqual(self.inbox(), [])
        self.assertTrue((self.root / "errors.jsonl").exists())

    def ledger(self, entries):
        path = Path(self.temp.name) / "ledger.json"
        path.write_text(json.dumps({"sessions": entries}))
        return path

    def test_session_ledger_tracks_changed_session(self):
        ledger = self.ledger([{"name": "primary", "session_id": "session-alpha"},
                              {"name": "other", "session_id": "session-other"}])
        self.config({"session_ledger": str(ledger), "session_names": ["primary"]})
        self.hook("First")
        self.hook("Not selected", session_id="session-other")
        self.ledger([{"name": "primary", "session_id": "session-new"}])
        self.hook("Old session")
        self.hook("New session", session_id="session-new")
        self.assertEqual([r["said"] for r in self.inbox()], ["First", "New session"])

    def test_ledger_no_match_missing_and_invalid_fail_closed(self):
        ledger = self.ledger([{"name": "other", "session_id": "session-other"}])
        self.config({"session_ledger": str(ledger), "session_names": ["primary"]})
        self.hook()
        ledger.write_text("{bad")
        self.hook()
        ledger.unlink()
        self.hook()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(len(APP.rows(self.root / "errors.jsonl")), 3)

    def test_channel_body_only_for_explicit_sessions(self):
        self.config({"channel_sessions": ["watch-session"]})
        body = "  Please review\n\tthis  "
        envelope = f'<channel sender="example">{body}</channel>'
        self.hook(envelope, session_id="watch-session", prompt_id="body")
        self.hook("Timer fired", session_id="watch-session")
        self.hook('<channel><task-notification>done</channel>', session_id="watch-session")
        self.hook(envelope, session_id="session-alpha")
        self.assertEqual([r["said"] for r in self.inbox()], [body, envelope])

    def test_channel_body_redaction_and_replay(self):
        self.config({"channel_sessions": ["watch-session"], "redact_secrets": True})
        for _ in range(2):
            self.hook('<channel> password: example-value </channel>',
                      session_id="watch-session", prompt_id="channel-input")
        self.assertEqual([r["said"] for r in self.inbox()], [" password: [REDACTED] "])

    def test_source_scopes_local_retry_and_prompt_identity(self):
        for source in ("desktop", "laptop"):
            self.config({"source": source})
            self.hook(prompt_id="same-id")
            self.hook(prompt_id="same-id")
        self.assertEqual(len(self.inbox()), 2)
        self.assertEqual({r["source"] for r in self.inbox()}, {"desktop", "laptop"})
        self.assertEqual(len({r["id"] for r in self.inbox()}), 2)

    def import_row(self, **changes):
        return {"id": "origin-1", "t": "2031-05-06T12:00:00.000001+00:00",
                "said": "  Keep\nthis\u2028text  ", "session": "remote-session", "cwd": "", **changes}

    def test_collect_replays_preserve_time_text_and_source_scope(self):
        raw = json.dumps(self.import_row()) + "\n"
        for source in ("desktop", "laptop"):
            for _ in range(2):
                result = self.cli("collect", "--source", source, raw=raw)
                self.assertEqual(result.returncode, 0, result.stderr)
        records = self.inbox()
        self.assertEqual(len(records), 2)
        self.assertEqual({r["said"] for r in records}, {self.import_row()["said"]})
        self.assertEqual({r["t"] for r in records}, {self.import_row()["t"]})
        self.assertEqual(len({r["key"] for r in records}), 2)

    def test_collect_native_inbox_jsonl(self):
        self.hook("From another machine", prompt_id="native-input")
        raw = (self.root / "inbox.jsonl").read_text()
        remote = Path(self.temp.name) / "collector"
        env = dict(self.env, NEVER_MISS_DIR=str(remote))
        for _ in range(2):
            result = self.cli("collect", "--source", "laptop", raw=raw, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
        records = APP.rows(remote / "inbox.jsonl")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["said"], "From another machine")

    def test_collect_validates_whole_batch_before_writing(self):
        for bad in ("{broken", "[]", json.dumps(self.import_row(id=2)),
                    json.dumps(self.import_row(t="2031-05-06T12:00:00")),
                    json.dumps(self.import_row(cwd=[]))):
            with self.subTest(bad=bad):
                result = self.cli("collect", "--source", "laptop",
                                  raw=json.dumps(self.import_row()) + "\n" + bad + "\n")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.inbox(), [])
                self.assertNotIn(self.import_row()["said"], result.stderr)
        self.assertEqual(self.cli("collect", "--source", " ", raw="").returncode, 1)

    def test_collect_filters_and_redacts_before_writing(self):
        self.config({"sessions": ["remote-session"], "redact_secrets": True})
        inputs = [self.import_row(said="password=example-value"),
                  self.import_row(id="origin-2", said="<task-notification>done"),
                  self.import_row(id="origin-3", session="unselected")]
        raw = "\n".join(json.dumps(r) for r in inputs) + "\n"
        self.assertEqual(self.cli("collect", "--source", "laptop", raw=raw).returncode, 0)
        self.assertEqual([r["said"] for r in self.inbox()], ["password=[REDACTED]"])

    def test_collect_concurrent_replay_saved_once(self):
        raw = json.dumps(self.import_row()) + "\n"
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.cli("collect", "--source", "laptop", raw=raw), range(8)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertEqual(len(self.inbox()), 1)

    def receiver(self, code, **config):
        path = Path(self.temp.name) / "receiver.py"
        path.write_text(code)
        self.config({"export_command": [sys.executable, str(path)], **config})
        return path

    def test_dispatch_acks_once_and_preserves_inbox_and_done(self):
        received = Path(self.temp.name) / "received.jsonl"
        self.receiver("import json, os, sys\nfrom pathlib import Path\n"
                      "entry = json.load(sys.stdin)\n"
                      "assert entry['idempotency_key'] == os.environ['NEVER_MISS_IDEMPOTENCY_KEY']\n"
                      f"with Path({str(received)!r}).open('a') as f: f.write(json.dumps(entry) + '\\n')\n")
        marker = Path(self.temp.name) / "must-not-exist"
        self.hook(f"Use literal $(touch {marker})\nthen review")
        original = (self.root / "inbox.jsonl").read_bytes()
        for _ in range(2):
            result = self.cli("dispatch")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(APP.rows(received)), 1)
        self.assertFalse(marker.exists())
        self.assertEqual((self.root / "inbox.jsonl").read_bytes(), original)
        self.assertFalse((self.root / "done.jsonl").exists())
        for name in ("outbox.jsonl", "deliveries.jsonl", ".dispatch.lock"):
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)

    def test_dispatch_failure_then_retry_keeps_token(self):
        path = self.receiver("import sys\nsys.exit(7)\n")
        self.hook()
        first = self.cli("dispatch")
        self.assertEqual(first.returncode, 1)
        self.assertIn("pending 1", first.stdout)
        path.write_text("import json, sys\njson.load(sys.stdin)\n")
        self.assertEqual(self.cli("dispatch").returncode, 0)
        events = APP.rows(self.root / "deliveries.jsonl")
        self.assertEqual([r["status"] for r in events], ["failed", "sent"])
        self.assertEqual(len({r["idempotency_key"] for r in events}), 1)
        self.assertEqual(len(APP.rows(self.root / "outbox.jsonl")), 1)

    def test_dispatch_timeout_missing_command_and_no_output_leak(self):
        path = self.receiver("import time\ntime.sleep(5)\n", export_timeout=0.05)
        self.hook()
        self.assertEqual(self.cli("dispatch").returncode, 1)
        self.assertIn("unknown", APP.rows(self.root / "deliveries.jsonl")[0]["detail"])
        path.write_text("import sys\nprint('private receiver output')\n"
                        "print('private error output', file=sys.stderr)\nsys.exit(3)\n")
        result = self.cli("dispatch")
        self.assertNotIn("private", result.stdout + result.stderr)
        self.config({"export_command": [str(Path(self.temp.name) / "missing-command")]})
        self.assertEqual(self.cli("dispatch").returncode, 1)
        self.assertEqual(len(APP.rows(self.root / "deliveries.jsonl")), 3)

    def test_dispatch_configuration_validation(self):
        for value in ({}, {"export_command": "echo unsafe"}, {"export_command": []},
                      {"export_command": ["echo"], "export_timeout": 0},
                      {"export_command": ["echo"], "export_timeout": True}):
            self.config(value)
            self.assertEqual(self.cli("dispatch").returncode, 1)
        self.assertFalse((self.root / "outbox.jsonl").exists())

    def test_dispatch_repairs_missing_queue_from_durable_inbox(self):
        self.hook("Captured before export was configured")
        self.receiver("import json, sys\njson.load(sys.stdin)\n")
        self.assertEqual(self.cli("dispatch").returncode, 0)
        self.assertEqual(len(APP.rows(self.root / "outbox.jsonl")), 1)
        self.assertEqual(len(APP.rows(self.root / "deliveries.jsonl")), 1)

    def test_dispatch_does_not_run_from_capture_or_preview_export(self):
        marker = Path(self.temp.name) / "executed"
        self.receiver(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        self.hook()
        self.cli("export", "--format", "markdown")
        self.assertFalse(marker.exists())
        self.assertEqual(self.cli("dispatch").returncode, 0)
        self.assertTrue(marker.exists())

    def test_dispatch_does_not_hold_capture_lock_and_serializes_senders(self):
        started = Path(self.temp.name) / "started"
        received = Path(self.temp.name) / "received"
        self.receiver("import time\nfrom pathlib import Path\n"
                      f"Path({str(started)!r}).touch()\n"
                      "time.sleep(1.5)\n"
                      f"with Path({str(received)!r}).open('a') as f: f.write('received\\n')\n")
        self.hook("First")
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.cli, "dispatch")
            import time
            limit = time.monotonic() + 3
            while not started.exists() and time.monotonic() < limit:
                time.sleep(0.01)
            self.assertTrue(started.exists())
            self.hook("Captured during dispatch")
            second = self.cli("dispatch")
            self.assertEqual(second.returncode, 1)
            self.assertIn("lock unavailable", second.stderr)
            self.assertEqual(first.result().returncode, 0)
        self.assertEqual(len(self.inbox()), 2)
        self.assertEqual(received.read_text(), "received\n")

    def test_dispatch_crash_after_receiver_success_requires_receiver_dedup(self):
        self.receiver("import json, sys\njson.load(sys.stdin)\n")
        self.hook()
        real_append = APP.append
        seen = set()
        calls = []

        def receive(*args, **kwargs):
            entry = json.loads(kwargs["input"])
            calls.append(entry["idempotency_key"])
            seen.add(entry["idempotency_key"])
            return subprocess.CompletedProcess(args[0], 0)

        def crash(path, row):
            if path.name == "deliveries.jsonl":
                raise OSError("simulated crash before receipt")
            return real_append(path, row)

        with patch.object(APP.subprocess, "run", side_effect=receive):
            with patch.object(APP, "append", side_effect=crash):
                with self.assertRaises(OSError):
                    APP.dispatch(self.root)
            with patch("builtins.print"):
                APP.dispatch(self.root)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(seen), 1)
        self.assertEqual(calls[0], calls[1])


    def test_collect_utc_z_and_partial_storage_failure_replay(self):
        import io
        raw = "\n".join(json.dumps(self.import_row(id=f"input-{i}",
                            t=f"2031-05-06T12:00:0{i}Z")) for i in range(2)) + "\n"
        real_append = APP.append
        count = 0

        def fail_second(path, row):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("storage failure")
            return real_append(path, row)

        with patch.object(APP, "append", side_effect=fail_second):
            with self.assertRaises(OSError):
                APP.collect(io.StringIO(raw), self.root, "laptop")
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.cli("collect", "--source", "laptop", raw=raw).returncode, 0)
        self.assertEqual(len(self.inbox()), 2)
        self.assertEqual(len(json.loads(self.cli("list", "--since", "999w", "--json").stdout)), 2)

    def test_collect_key_conflict_rejects_batch_without_writes(self):
        raw = "\n".join(json.dumps(self.import_row(id=f"input-{i}")) for i in range(2)) + "\n"
        self.assertEqual(self.cli("collect", "--source", "laptop", raw=raw).returncode, 1)
        self.assertEqual(self.inbox(), [])

    def test_dispatch_failure_does_not_block_other_pending_entries(self):
        self.receiver("import json, sys\nentry = json.load(sys.stdin)\n"
                      "sys.exit(4 if entry['record']['said'] == 'Fail' else 0)\n")
        self.hook("Fail")
        self.hook("Succeed")
        self.assertEqual(self.cli("dispatch").returncode, 1)
        events = APP.rows(self.root / "deliveries.jsonl")
        self.assertEqual([r["status"] for r in events], ["failed", "sent"])
        self.assertEqual(self.cli("dispatch").returncode, 1)
        self.assertEqual(len(APP.rows(self.root / "deliveries.jsonl")), 3)

    def test_dispatch_corrupt_outbox_is_preserved_without_execution(self):
        marker = Path(self.temp.name) / "must-not-execute"
        self.receiver(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        self.hook()
        path = self.root / "outbox.jsonl"
        path.write_text("{broken")
        self.assertEqual(self.cli("dispatch").returncode, 1)
        self.assertEqual(path.read_text(), "{broken")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
