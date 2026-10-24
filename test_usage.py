"""Offline quota checks. No real Claude process, credentials, or network is allowed."""

import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import usage

PANEL = """Claude Max
Current session
████ 37% used
Resets 2:30pm (Europe/Bratislava)
Current week (all models)
████ 51% used
Resets Oct 20 at 10pm (Europe/Bratislava)
Current week (Sonnet only)
██ 80% remaining
What's contributing to your limits usage?
Skills 99% used
"""


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        for name, value in {
            "STATE_DIR": root,
            "LAST_OK_FILE": root / "last_ok.json",
            "CLAUDE_RETRY_FILE": root / "claude_retry.json",
        }.items():
            patcher = patch.object(usage, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in (
            "usage.subprocess.run",
            "usage.urllib.request.urlopen",
            "usage.subprocess.Popen",
        ):
            patcher = patch(target, side_effect=AssertionError("Live access forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_labeled_account_windows_ignore_insights_and_context_meter(self):
        gauges = usage.claude_parse("Opus | 0% context\n" + PANEL)
        self.assertEqual([g["percent"] for g in gauges], [37, 51, 20])
        self.assertEqual(
            [g["id"] for g in gauges],
            ["claude:session", "claude:weekly_all", "claude:weekly_scoped:Sonnet only"],
        )
        self.assertTrue(all(not g["stale"] for g in gauges))

    def test_cursor_positioned_words_and_loading_do_not_invent_values(self):
        ansi = "Current\x1b[12Gsession\n██\x1b[55G3%\x1b[58Gused\nCurrent\x1b[12Gweek\x1b[17G(all\x1b[22Gmodels)\n27%\x1b[59Gused"
        self.assertEqual([g["percent"] for g in usage.claude_parse(ansi)], [3, 27])
        for text in (
            "Loading usage data…\nOpus | 0% context",
            "Current session\nLoading usage data…",
            "What's contributing to your limits usage?\n42% used",
        ):
            with self.assertRaises(ValueError):
                usage.claude_parse(text)

    def test_reset_dates_keep_timezone_and_unknown_resets_unavailable(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz)

        with patch.object(usage, "datetime", Clock):
            self.assertEqual(
                usage.claude_reset("2:30pm (Europe/Bratislava)"),
                "2026-10-15T12:30:00+00:00",
            )
            self.assertEqual(
                usage.claude_reset("Oct 20 at 10pm (Europe/Bratislava)"),
                "2026-10-20T20:00:00+00:00",
            )
            self.assertIsNone(usage.claude_reset("Oct 14 at 10pm (Europe/Bratislava)"))
            self.assertIsNone(usage.claude_reset("something new"))

    def test_rate_limit_persists_and_other_sources_still_update(self):
        old = usage.gauge("claude:session", "C", "Claude · 5 h", 91, None, "claude")
        usage.write_private(usage.LAST_OK_FILE, {"claude": [old]})
        usage.write_private(
            usage.CLAUDE_RETRY_FILE,
            {"retry_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        )
        self.assertEqual(usage.claude_backoff(), {})
        with (
            patch.object(usage, "find_binary", return_value="mock-claude"),
            patch.object(
                usage,
                "claude_capture",
                return_value="Failed to load usage data: rate_limit_error",
            ),
        ):
            first, errors = usage.collect(["claude"])
        self.assertEqual(first[0]["percent"], 91)
        self.assertTrue(first[0]["stale"])
        self.assertIn("rate limited", errors[0])
        other = usage.gauge("codex:test", "X", "Codex", 2, None, "codex")
        with (
            patch.object(
                usage,
                "claude_capture",
                side_effect=AssertionError("No CLI during backoff"),
            ),
            patch.dict(usage.SOURCE_FUNCS, {"codex": lambda: [other]}),
        ):
            second, errors = usage.collect(["claude", "codex"])
        self.assertEqual(second, first + [other])
        self.assertTrue(errors)
        self.assertEqual(usage.CLAUDE_RETRY_FILE.stat().st_mode & 0o777, 0o600)

    def test_success_uses_cli_and_plan_never_reads_credentials(self):
        with (
            patch.object(usage, "find_binary", return_value="mock-claude"),
            patch.object(usage, "claude_capture", return_value=PANEL) as capture,
        ):
            self.assertEqual(usage.claude_gauges()[0]["percent"], 37)
            self.assertEqual(usage.claude_plan(), "Max")
        capture.assert_called_once_with("mock-claude")
        self.assertNotIn("retry_at", usage.claude_state())

    def test_recent_snapshot_from_expired_window_is_refetched(self):
        reset = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        old = usage.gauge("claude:session", "C", "Claude · 5 h", 91, reset, "claude")
        usage.write_private(usage.LAST_OK_FILE, {"claude": [old]})
        fresh = {**old, "percent": 4, "reset": None}
        with patch.dict(usage.SOURCE_FUNCS, {"claude": lambda: [fresh]}):
            gauges, errors = usage.collect(["claude"], max_age=300)
        self.assertEqual(gauges[0]["percent"], 4)
        self.assertEqual(errors, [])

    def test_cli_explains_why_cached_values_are_stale(self):
        old = usage.gauge("claude:session", "C", "Claude · 5 h", 91, None, "claude")
        old["stale"] = True
        views = usage.source_views(
            [old], ["claude: Rate limited; retry after later"], ["claude"]
        )
        report = usage.format_report(views, one=True)
        self.assertIn("stale", report)
        self.assertIn("Rate limited; retry after later", report)

    def fake_cli(self, body):
        executable = Path(self.directory.name) / "fake-claude"
        executable.write_text("#!/usr/bin/python3\nimport json, os, sys, time\n" + body)
        executable.chmod(0o700)
        return str(executable)

    def test_trust_selection_requires_cursor_redraw_before_enter(self):
        first = "\x1b[HQuick safety check:\r\n❯ No, exit\r\n  Yes, I trust this folder"
        self.assertEqual(usage.claude_trust_key(usage.claude_screen(first)), b"\x1b[B")
        redraw = "\x1b[2;1H\x1b[2K  No, exit\x1b[3;1H\x1b[2K❯ Yes, I trust this folder"
        self.assertEqual(
            usage.claude_trust_key(usage.claude_screen(first + redraw)), b"\r"
        )
        self.assertIsNone(
            usage.claude_trust_key("Quick safety check:\n  Yes, I trust this folder")
        )

    def test_real_pty_retries_only_usage_after_consumed_startup_command(self):
        executable = self.fake_cli(
            "assert sys.argv[1:5] == ['--allowed-tools', '', '--strict-mcp-config', '--settings']\n"
            "assert json.loads(sys.argv[5]) == {'disableAllHooks': True, 'remoteControlAtStartup': False}\n"
            "assert os.path.basename(os.getcwd()) == 'ClaudeProbe'\n"
            "assert os.tcgetpgrp(0) == os.getpgrp(), 'CLI needs a controlling terminal'\n"
            "assert not any(k.startswith('ANTHROPIC_') for k in os.environ)\n"
            "assert 'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ\n"
            "import tty\n"
            "tty.setraw(0)\n"
            "print('\\x1b[HQuick safety check:\\n❯ No, exit\\n  Yes, I trust this folder', flush=True)\n"
            "assert os.read(0, 3) == b'\\x1b[B'\n"
            "print('\\x1b[2;1H\\x1b[2K  No, exit\\x1b[3;1H\\x1b[2K❯ Yes, I trust this folder', end='', flush=True)\n"
            "assert os.read(0, 1) == b'\\r'\n"
            "print('\\x1b[2J\\x1b[HPress Enter to continue', flush=True)\n"
            "assert os.read(0, 1) == b'\\r'\n"
            "print('\\x1b[2J\\x1b[H❯ ', flush=True)\n"
            "time.sleep(0.5)\n"
            "import select\n"
            "assert not select.select([sys.stdin], [], [], 0)[0], 'Command arrived before input handlers were ready'\n"
            "assert os.read(0, 7) == b'/usage\\r'\n"
            "print('\\x1b[2J\\x1b[HClaude Code v2.1.299\\n❯ ', flush=True)\n"
            "assert os.read(0, 7) == b'/usage\\r'\n"
            "print('❯ /usage    Show session cost, plan usage, and activity stats', flush=True)\n"
            "assert os.read(0, 1) == b'\\r'\n"
            f"print('\\x1b[2J\\x1b[H' + {PANEL!r}, flush=True)\n"
            "time.sleep(10)\n"
        )
        # Restore Popen only for our local fixture executable, never the installed Claude CLI.
        with patch.object(usage.subprocess, "Popen", REAL_POPEN):
            text = usage.claude_capture(executable, timeout=8)
        self.assertEqual(usage.claude_parse(text)[0]["percent"], 37)

    def test_real_pty_fake_login_prompt_stops_without_submitting_input(self):
        executable = self.fake_cli(
            "print('Select login method', flush=True)\ntime.sleep(10)\n"
        )
        with (
            patch.object(usage.subprocess, "Popen", REAL_POPEN),
            self.assertRaisesRegex(RuntimeError, "needs interactive login"),
        ):
            usage.claude_capture(executable, timeout=3)


class AntigravityUsageTests(unittest.TestCase):
    def test_new_quota_group_keeps_its_name_and_only_reported_window(self):
        summary = {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.75}
                    ],
                },
                {
                    "name": "Gemini 4 Argon",
                    "buckets": [
                        {
                            "id": "gemini-argon-wtu-weekly",
                            "window": "weekly",
                            "remaining_fraction": 0.9393116235733032,
                            "reset_time": "2026-10-26T16:50:15Z",
                        }
                    ],
                },
            ]
        }
        with patch.object(usage, "antigravity_local_summary", return_value=summary):
            gauges = usage.gemini_gauges()
        self.assertEqual(len(gauges), 2)
        self.assertEqual(gauges[0]["label"], "Gemini · 5 h")
        self.assertEqual(gauges[1]["label"], "Gemini 4 Argon · týždeň")
        report = usage.format_report(
            usage.source_views(gauges, [], ["gemini"]), one=True
        )
        self.assertIn("Gemini 4 Argon week", report)
        self.assertEqual(gauges[1]["id"], "antigravity:gemini-argon-wtu-weekly")
        self.assertEqual(gauges[1]["percent"], 6)
        self.assertEqual(gauges[1]["reset"], "2026-10-26T16:50:15Z")
        for field, invalid in (("remaining_fraction", None), ("window", "unknown")):
            with self.subTest(field=field):
                bucket = summary["groups"][1]["buckets"][0]
                previous = bucket[field]
                bucket[field] = invalid
                with self.assertRaises(ValueError):
                    usage.antigravity_buckets(summary)
                bucket[field] = previous


REAL_POPEN = subprocess.Popen

if __name__ == "__main__":
    unittest.main()
