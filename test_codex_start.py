#!/usr/bin/env python3

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from datetime import datetime
from unittest import mock

import codex_start


_DEC_PRIVATE_MODE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")


def vte_mouse_button_input(
    terminal_output: bytes,
    *,
    button: int,
    column: int = 1,
    row: int = 1,
) -> bytes | None:
    """Model whether VTE gives a button press to the app or its own UI."""
    mouse_tracking: set[int] = set()
    sgr_mouse = False
    for match in _DEC_PRIVATE_MODE.finditer(terminal_output):
        enabled = match.group(2) == b"h"
        for raw_mode in match.group(1).split(b";"):
            mode = int(raw_mode)
            if mode in (9, 1000, 1001, 1002, 1003):
                if enabled:
                    mouse_tracking.add(mode)
                else:
                    mouse_tracking.discard(mode)
            elif mode == 1006:
                sgr_mouse = enabled
    if not mouse_tracking:
        return None
    code = {1: 0, 2: 1, 3: 2, 4: 64, 5: 65}[button]
    if sgr_mouse:
        return f"\x1b[<{code};{column};{row}M".encode()
    return b"\x1b[M" + bytes((code + 32, column + 32, row + 32))


def vte_wheel_up_input(
    terminal_output: bytes,
    *,
    rows: int,
    column: int = 1,
    row: int = 1,
) -> bytes:
    """Model VTE 0.76's input bytes for one upward wheel notch."""
    alternate_screen = False
    alternate_scroll = True
    application_cursor_keys = False
    mouse_tracking: set[int] = set()
    sgr_mouse = False
    for match in _DEC_PRIVATE_MODE.finditer(terminal_output):
        enabled = match.group(2) == b"h"
        for raw_mode in match.group(1).split(b";"):
            mode = int(raw_mode)
            if mode in (47, 1047, 1049):
                alternate_screen = enabled
            elif mode == 1:
                application_cursor_keys = enabled
            elif mode == 1007:
                alternate_scroll = enabled
            elif mode in (1000, 1002, 1003):
                if enabled:
                    mouse_tracking.add(mode)
                else:
                    mouse_tracking.discard(mode)
            elif mode == 1006:
                sgr_mouse = enabled

    if mouse_tracking:
        if sgr_mouse:
            return f"\x1b[<64;{column};{row}M".encode()
        return b"\x1b[M" + bytes(
            (96, min(255, column + 32), min(255, row + 32))
        )
    if alternate_screen and alternate_scroll:
        up = b"\x1bOA" if application_cursor_keys else b"\x1b[A"
        return up * max(1, (rows + 9) // 10)
    return b""


class CodexStartTests(unittest.TestCase):
    def make_account(self, root: Path, name: str = "alpha"):
        home = root / f".codex-{name}"
        home.mkdir(parents=True)
        return codex_start.Account(name, home)

    def make_accounts(self, root: Path):
        return tuple(
            self.make_account(root, name)
            for name in ("alpha", "beta", "account-two")
        )

    @staticmethod
    def limits(now: float = 1.0):
        return {
            "limit_id": "codex",
            "plan_type": "plus",
            "primary": {
                "used_percent": 27,
                "window_minutes": 300,
                "resets_at": int(now + 4 * 3600),
            },
            "secondary": {
                "used_percent": 4,
                "window_minutes": 10080,
                "resets_at": int(now + 6 * 86400),
            },
        }

    def snapshot(self, root: Path, now: float = 1.0):
        account = self.make_account(root)
        return codex_start.StatusSnapshot(
            account,
            "Plus",
            codex_start.ModelSettings("gpt-5.6-sol", "xhigh"),
            root / "project",
            self.limits(now),
            now,
        )

    def test_public_defaults_contain_no_account_identities(self):
        home = Path("/home/example")
        self.assertEqual(codex_start.default_accounts(home), ())

    def test_missing_account_config_stays_missing_on_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(codex_start.load_accounts(root, create=True), ())
            self.assertFalse((root / "accounts.json").exists())

    def test_existing_account_config_is_read_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            document = {
                "version": 1,
                "accounts": [
                    {"name": "alpha", "codex_home": "homes/alpha"}
                ],
                "future_field": {"preserve": True},
            }
            path = root / "accounts.json"
            path.write_text(json.dumps(document, indent=3), encoding="utf-8")
            before = path.read_bytes()
            self.assertEqual(
                codex_start.load_accounts(root),
                (codex_start.Account("alpha", root / "homes" / "alpha"),),
            )
            self.assertEqual(path.read_bytes(), before)

    def test_empty_first_run_adds_local_account_and_optional_preset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account_home = root / "codex-home"
            output = io.StringIO()
            with mock.patch.object(
                codex_start, "config_dir", return_value=root
            ), mock.patch(
                "builtins.input",
                side_effect=["a", "alpha", str(account_home), "Crimson"],
            ), contextlib.redirect_stdout(output):
                account = codex_start.choose_account(())
            self.assertEqual(account, codex_start.Account("alpha", account_home))
            self.assertEqual(codex_start.load_accounts(root), (account,))
            self.assertEqual(
                codex_start.ThemeStore(root).preset_for("alpha"), "crimson"
            )
            self.assertIn("No Codex accounts configured", output.getvalue())
            self.assertIn("+ Add account", output.getvalue())

    def test_empty_and_configured_selector_frames_show_primary_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = codex_start.ThemeStore(root)
            empty = codex_start._selector_frame(
                (), 0, 100, 14, store, "none"
            )
            self.assertIn("No Codex accounts configured", empty)
            self.assertIn("+ Add account", empty)

            account = self.make_account(root)
            store.set_preset(account.name, "cobalt")
            configured = codex_start._selector_frame(
                (account,), 0, 100, 14, store, "none"
            )
            self.assertIn("[Cobalt]", configured)
            self.assertIn("t theme", configured)

    def test_account_definitions_round_trip_and_are_extensible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entries = (
                codex_start.Account("alpha", root / "wonder"),
                codex_start.Account("fourth", root / "fourth"),
            )
            path = codex_start.save_accounts(entries, root)
            loaded = codex_start.load_accounts(root)
            self.assertEqual(loaded, entries)
            self.assertEqual(path.name, "accounts.json")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_account_config_validates_root_and_resolves_relative_homes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "accounts.json"
            path.write_text("[]")
            with self.assertRaisesRegex(SystemExit, "document must be an object"):
                codex_start.load_accounts(root)

            path.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {"name": "local", "codex_home": "homes/local"}
                        ]
                    }
                )
            )
            self.assertEqual(
                codex_start.load_accounts(root),
                (codex_start.Account("local", root / "homes" / "local"),),
            )

    def test_account_resolution_supports_name_and_number(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            entries = self.make_accounts(Path(temp_dir))
        self.assertEqual(codex_start.resolve_account("2", entries).name, "beta")
        self.assertEqual(
            codex_start.resolve_account("account-two", entries).name, "account-two"
        )
        self.assertIsNone(codex_start.resolve_account("missing", entries))

    def test_exact_numeric_account_name_precedes_positional_lookup(self):
        entries = (
            codex_start.Account("2", Path("/tmp/named-two")),
            codex_start.Account("other", Path("/tmp/other")),
        )
        self.assertEqual(
            codex_start.resolve_account("2", entries),
            entries[0],
        )

    def test_model_and_reasoning_are_read_for_each_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index, name in enumerate(("alpha", "beta", "account-two")):
                account = self.make_account(root, name)
                effort = ("xhigh", "max", "high")[index]
                (account.home / "config.toml").write_text(
                    f'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "{effort}"\n'
                )
                self.assertEqual(
                    codex_start.load_model_settings(account.home),
                    codex_start.ModelSettings("gpt-5.6-sol", effort),
                )

    def test_reads_latest_codex_bucket_and_ignores_newer_premium_bucket(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = self.make_account(Path(temp_dir))
            session = account.home / "sessions" / "2026" / "08" / "27" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            events = [
                {
                    "timestamp": "2026-08-27T10:00:00Z",
                    "payload": {
                        "type": "token_count",
                        "rate_limits": {
                            **self.limits(),
                            "primary": {
                                "used_percent": 42,
                                "window_minutes": 300,
                                "resets_at": 2_000_000_000,
                            },
                        },
                    },
                },
                {
                    "timestamp": "2026-08-27T10:00:01Z",
                    "payload": {
                        "type": "token_count",
                        "rate_limits": {"limit_id": "premium", "plan_type": "plus"},
                    },
                },
            ]
            session.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n"
            )
            limits = codex_start.cached_rate_limits(account.home)
            self.assertEqual(limits["limit_id"], "codex")
            self.assertEqual(limits["primary"]["used_percent"], 42)

    def test_cached_usage_ignores_non_codex_buckets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = self.make_account(Path(temp_dir))
            session = account.home / "sessions" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-08-27T10:00:00Z",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "limit_id": "premium",
                                "primary": {
                                    "used_percent": 50,
                                    "window_minutes": 300,
                                },
                            },
                        },
                    }
                )
                + "\n"
            )
            self.assertIsNone(codex_start.cached_rate_limits(account.home))

    def test_newer_live_snapshot_atomically_replaces_stale_cached_windows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = self.snapshot(root)
            stale_limits = {
                **self.limits(),
                "primary": {
                    "used_percent": 100,
                    "window_minutes": 300,
                    "resets_at": 150,
                },
                "secondary": {
                    "used_percent": 16,
                    "window_minutes": 10080,
                    "resets_at": 700,
                },
            }
            cached = codex_start.StatusSnapshot(
                base.account,
                base.plan,
                base.settings,
                base.cwd,
                stale_limits,
                100,
            )
            live_limits = {
                "limitId": "codex",
                "planType": "plus",
                "primary": {
                    "usedPercent": 0,
                    "windowDurationMins": 300,
                    "resetsAt": 600,
                },
                "secondary": {
                    "usedPercent": 0,
                    "windowDurationMins": 10080,
                    "resetsAt": 900,
                },
            }

            updated = codex_start.snapshot_with_rate_limits(
                cached, live_limits, observed_at=200
            )

            five_hour, weekly = codex_start.limit_windows(updated.limits)
            self.assertEqual(codex_start.window_status(five_hour, now=200), (100, 600))
            self.assertEqual(codex_start.window_status(weekly, now=200), (100, 900))
            self.assertEqual(updated.updated_at, 200)
            self.assertEqual(updated.limits["_seen_at"], 200)

    def test_delayed_stale_read_cannot_override_newer_session_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = self.snapshot(Path(temp_dir))
            newer_limits = {
                "limitId": "codex",
                "planType": "plus",
                "primary": {
                    "usedPercent": 7,
                    "windowDurationMins": 300,
                    "resetsAt": 900,
                },
                "secondary": {
                    "usedPercent": 4,
                    "windowDurationMins": 10080,
                    "resetsAt": 1000,
                },
            }
            current = codex_start.snapshot_with_rate_limits(
                base, newer_limits, observed_at=300
            )
            delayed_stale_limits = {
                **self.limits(),
                "primary": {
                    "used_percent": 100,
                    "window_minutes": 300,
                    "resets_at": 150,
                },
                "secondary": {
                    "used_percent": 16,
                    "window_minutes": 10080,
                    "resets_at": 700,
                },
            }

            unchanged = codex_start.snapshot_with_rate_limits(
                current, delayed_stale_limits, observed_at=250
            )

            self.assertIs(unchanged, current)
            self.assertEqual(unchanged.limits["primary"]["usedPercent"], 7)
            self.assertEqual(unchanged.limits["secondary"]["usedPercent"], 4)

    def test_app_server_result_prefers_codex_bucket_over_other_top_level_bucket(self):
        codex_limits = {
            "limitId": "codex",
            "primary": {"usedPercent": 0, "windowDurationMins": 300},
            "secondary": {"usedPercent": 0, "windowDurationMins": 10080},
        }
        result = {
            "rateLimits": {"limitId": "premium"},
            "rateLimitsByLimitId": {
                "premium": {"limitId": "premium"},
                "codex": codex_limits,
            },
        }
        self.assertEqual(
            codex_start.app_server_codex_rate_limits(result), codex_limits
        )

    def test_app_server_bucket_map_does_not_guess_missing_ids(self):
        result = {
            "rateLimitsByLimitId": {
                "premium": {
                    "primary": {
                        "usedPercent": 50,
                        "windowDurationMins": 300,
                    }
                }
            }
        }
        self.assertIsNone(codex_start.app_server_codex_rate_limits(result))

    def test_app_server_reader_completes_handshake_before_rate_limit_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fake_codex = root / "fake-codex"
            response = json.dumps(
                {
                    "id": 1,
                    "result": {
                        "rateLimitsByLimitId": {
                            "codex": {
                                "limitId": "codex",
                                "planType": "plus",
                                "primary": {
                                    "usedPercent": 25,
                                    "windowDurationMins": 300,
                                    "resetsAt": 2_000_000_000,
                                },
                                "secondary": {
                                    "usedPercent": 10,
                                    "windowDurationMins": 10080,
                                    "resetsAt": 2_000_100_000,
                                },
                            }
                        }
                    },
                }
            )
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "initialize = json.loads(sys.stdin.readline())\n"
                "if initialize.get('method') != 'initialize':\n"
                "    raise SystemExit(2)\n"
                "print(json.dumps({'id': 0, 'result': {'userAgent': 'fake'}}), "
                "flush=True)\n"
                "initialized = json.loads(sys.stdin.readline())\n"
                "if initialized != {'method': 'initialized', 'params': {}}:\n"
                "    raise SystemExit(3)\n"
                "request = json.loads(sys.stdin.readline())\n"
                "if request.get('method') != 'account/rateLimits/read':\n"
                "    raise SystemExit(4)\n"
                f"print({response!r}, flush=True)\n"
                "sys.stdin.read()\n"
            )
            fake_codex.chmod(0o755)
            reader = codex_start.AppServerRateLimitReader(
                str(fake_codex),
                os.environ.copy(),
                poll_seconds=60,
                retry_seconds=0.05,
                request_timeout_seconds=0.5,
            )
            observations = []
            try:
                deadline = time.monotonic() + 3
                while not observations and time.monotonic() < deadline:
                    observations.extend(reader.poll())
                    time.sleep(0.01)
            finally:
                reader.close()

            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].limits["limitId"], "codex")
            self.assertFalse(observations[0].sparse)

    def test_window_mapping_uses_duration_not_primary_order(self):
        weekly = {"windowDurationMins": 10080, "usedPercent": 20}
        five_hour = {"windowDurationMins": 300, "usedPercent": 30}
        actual_five, actual_week = codex_start.limit_windows(
            {"primary": weekly, "secondary": five_hour}
        )
        self.assertIs(actual_five, five_hour)
        self.assertIs(actual_week, weekly)

    def test_expired_and_missing_cached_data_degrade_cleanly(self):
        self.assertEqual(
            codex_start.window_status(
                {"used_percent": 95, "resets_at": 100}, now=101
            ),
            (None, 100),
        )
        self.assertEqual(codex_start.window_status(None, now=101), (None, None))
        with tempfile.TemporaryDirectory() as temp_dir:
            account = self.make_account(Path(temp_dir))
            text = codex_start.plain_status(
                codex_start.initial_snapshot(account), now=101
            )
            self.assertIn("account: alpha • --", text)
            self.assertIn("5h: --%  --", text)
            self.assertIn("week: --%  --", text)
            self.assertNotIn("reset:", text)
            for ugly_state in ("refreshing", "loading", "None", "null", "unknown"):
                self.assertNotIn(ugly_state, text)

    def test_theme_colors_persist_independently_per_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = codex_start.ThemeStore(Path(temp_dir))
            store.set_color("alpha", "account", "red")
            store.set_color("beta", "account", "blue")
            self.assertEqual(
                store.theme_for("alpha")["account"], "#cd3131"
            )
            self.assertEqual(store.theme_for("beta")["account"], "#2472c8")
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_theme_default_override_and_account_reset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = codex_start.ThemeStore(Path(temp_dir))
            store.set_color("default", "plan", "orange")
            store.set_color("alpha", "plan", "cyan")
            self.assertEqual(store.theme_for("alpha")["plan"], "#11a8cd")
            store.reset("alpha")
            self.assertEqual(store.theme_for("alpha")["plan"], "#ffb000")
            store.reset("default")
            self.assertEqual(
                store.theme_for("alpha")["plan"],
                codex_start.DEFAULT_THEME["plan"],
            )

    def test_theme_rejects_invalid_field_and_color(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = codex_start.ThemeStore(Path(temp_dir))
            with self.assertRaises(ValueError):
                store.set_color("alpha", "bogus", "red")
            with self.assertRaises(ValueError):
                store.set_color("alpha", "plan", "not-a-color")

    def test_malformed_preferences_are_not_overwritten_by_updates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = codex_start.ThemeStore(root)
            store.path.write_text("{not-json")
            self.assertEqual(
                store.theme_for("alpha"),
                codex_start.DEFAULT_THEME,
            )
            with self.assertRaisesRegex(ValueError, "invalid"):
                store.set_color("alpha", "plan", "green")
            self.assertEqual(store.path.read_text(), "{not-json")

    def test_every_status_color_field_is_independently_configurable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = codex_start.ThemeStore(Path(temp_dir))
            expected = {}
            for index, field in enumerate(codex_start.THEME_FIELDS):
                color = f"#{index + 1:06x}"
                store.set_color("account-two", field, color)
                expected[field] = color
            self.assertEqual(store.theme_for("account-two"), expected)
            self.assertEqual(
                store.theme_for("alpha"),
                codex_start.DEFAULT_THEME,
            )

    def test_theme_field_reset_inheritance_and_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = codex_start.ThemeStore(Path(temp_dir))
            store.set_color("default", "plan", "green")
            store.set_color("account-two", "plan", "purple")
            store.set_color("account-two", "model", "#123456")
            store.reset("account-two", "plan")
            self.assertEqual(store.theme_for("account-two")["plan"], "#0dbc79")
            self.assertEqual(store.theme_for("account-two")["model"], "#123456")
            store.copy_from("beta", "account-two")
            self.assertEqual(
                store.theme_for("beta"),
                store.theme_for("account-two"),
            )
            store.set_color("account-two", "model", "red")
            self.assertEqual(store.theme_for("beta")["model"], "#123456")

    def test_named_ansi256_rgb_and_truecolor_fallbacks(self):
        self.assertEqual(codex_start.normalize_color("purple"), "#b26cff")
        self.assertEqual(codex_start.normalize_color("rgb(50, 199, 255)"), "#32c7ff")
        self.assertEqual(codex_start.normalize_color("50,199,255"), "#32c7ff")
        self.assertEqual(
            codex_start.color_sequence("214", "256"),
            "\x1b[38;5;214m",
        )
        self.assertEqual(
            codex_start.color_sequence("#32c7ff", "truecolor"),
            "\x1b[38;2;50;199;255m",
        )
        self.assertIn(";5;", codex_start.color_sequence("#32c7ff", "256"))
        fallback = codex_start.color_sequence("#32c7ff", "16")
        self.assertTrue(fallback.startswith("\x1b["))
        self.assertNotIn(";2;", fallback)
        self.assertNotIn(";5;", codex_start.color_sequence("214", "16"))

        self.assertFalse(codex_start.valid_color("+1"))
        self.assertFalse(codex_start.valid_color("-1"))
        self.assertEqual(codex_start.color_sequence("#nothex", "truecolor"), "")

    def test_directory_shortening_uses_home_notation(self):
        home = Path("/home/example")
        self.assertEqual(
            codex_start.compact_path(home / "work" / "project", home),
            "~/work/project",
        )
        self.assertEqual(codex_start.compact_path(home, home), "~")
        self.assertEqual(
            codex_start.compact_path(Path("/srv/project"), home),
            "/srv/project",
        )

    def test_reset_formatting_uses_local_today_tomorrow_and_compact_relative(self):
        now = datetime(2026, 8, 27, 10, 0).timestamp()
        today = datetime(2026, 8, 27, 12, 14).timestamp()
        tomorrow = datetime(2026, 8, 28, 5, 0).timestamp()
        later = datetime(2026, 9, 2, 12, 0).timestamp()
        self.assertEqual(codex_start.reset_label(int(today), now), "Today 12:14 PM")
        self.assertEqual(codex_start.time_left_label(int(today), now), "2h 14m left")
        self.assertEqual(
            codex_start.reset_label(int(tomorrow), now),
            "Tomorrow 5:00 AM",
        )
        self.assertEqual(codex_start.time_left_label(int(tomorrow), now), "19h left")
        self.assertEqual(codex_start.reset_label(int(later), now), "Sep 2 12:00 PM")
        self.assertEqual(codex_start.time_left_label(int(later), now), "6d 2h left")
        self.assertEqual(codex_start.reset_label(None, now), "--")

    def test_color_modes_have_truecolor_256_16_and_plain_fallbacks(self):
        self.assertEqual(
            codex_start.detect_color_mode(
                {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
            ),
            "truecolor",
        )
        self.assertEqual(
            codex_start.detect_color_mode({"TERM": "xterm-256color"}), "256"
        )
        self.assertEqual(codex_start.detect_color_mode({"TERM": "xterm"}), "16")
        self.assertEqual(
            codex_start.detect_color_mode(
                {"TERM": "xterm-256color", "NO_COLOR": "1"}
            ),
            "none",
        )
        rendered = codex_start.render_spans(
            [codex_start.Span("hello", "#ffffff")], 8, "none"
        )
        self.assertEqual(rendered, "hello   ")
        self.assertNotIn("\x1b", rendered)

    def test_terminal_text_replaces_untrusted_control_characters(self):
        unsafe = "safe\x1b]0;owned\x07\n\t\x9btext"
        cleaned = codex_start.terminal_safe_text(unsafe)
        self.assertEqual(len(cleaned), len(unsafe))
        self.assertTrue(
            all(
                not (ord(character) < 32 or 127 <= ord(character) < 160)
                for character in cleaned
            )
        )
        rendered = codex_start.render_spans(
            [codex_start.Span(unsafe)], len(unsafe), "none"
        )
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertNotIn("\n", rendered)

    def test_compact_title_combines_five_hour_and_weekly_resets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            title = codex_start.compact_title(
                self.snapshot(Path(temp_dir), now=1),
                now=1,
            )
        self.assertIn("alpha • Plus", title)
        self.assertIn("gpt-5.6-sol xhigh", title)
        self.assertIn("5h 73% ", title)
        self.assertIn("week 96% ", title)
        self.assertIn(" · week", title)

    def test_native_terminal_initialization_clears_prior_history_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = self.snapshot(Path(temp_dir))
            initialization = codex_start.terminal_initialization(snapshot)
        self.assertEqual(initialization.count(b"\x1b[2J"), 1)
        self.assertEqual(initialization.count(b"\x1b[3J"), 1)
        self.assertLess(
            initialization.index(b"\x1b[2J"),
            initialization.index(b"\x1b[3J"),
        )
        self.assertIn(b"\x1b[H", initialization)
        self.assertIn(b"alpha", initialization)
        self.assertNotIn(b"\x1b[?1049", initialization)

    def test_selector_frame_never_exceeds_reported_dimensions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entries = tuple(
                self.make_account(root, name)
                for name in ("alpha", "beta", "account-two")
            )
            store = codex_start.ThemeStore(root / "config")
            for width, height in ((120, 24), (50, 10), (20, 3)):
                frame = codex_start._selector_frame(
                    entries, 0, width, height, store, "none"
                )
                lines = frame.removeprefix("\x1b[H").split("\r\n")
                self.assertEqual(len(lines), height)
                self.assertTrue(
                    all(codex_start.cell_width(line) == width for line in lines)
                )

    def test_non_tty_account_selection_is_keyboard_friendly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            entries = self.make_accounts(Path(temp_dir))
            fake_input = io.StringIO("2\n")
            fake_output = io.StringIO()
            with mock.patch.object(codex_start.sys, "stdin", fake_input), mock.patch.object(
                codex_start.sys, "stdout", fake_output
            ):
                selected = codex_start.choose_account(entries)
        self.assertEqual(selected.name, "beta")

    def test_rollout_tracker_updates_directory_model_effort_plan_and_usage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account = self.make_account(root)
            (account.home / "config.toml").write_text(
                'model = "gpt-old"\nmodel_reasoning_effort = "low"\n'
            )
            tracker = codex_start.RolloutTracker(
                account, root / "initial", started_at=time.time()
            )
            session = account.home / "sessions" / "rollout.jsonl"
            session.parent.mkdir(parents=True)
            events = [
                {
                    "timestamp": "2026-08-27T10:00:00Z",
                    "type": "session_meta",
                    "payload": {"cwd": str(root / "session")},
                },
                {
                    "timestamp": "2026-08-27T10:00:01Z",
                    "type": "turn_context",
                    "payload": {
                        "cwd": str(root / "turn"),
                        "model": "gpt-5.6-sol",
                        "effort": "xhigh",
                    },
                },
                {
                    "timestamp": "2026-08-27T10:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": {
                            "cwd": str(root / "next"),
                            "model": "gpt-5.6-sol",
                            "reasoning_effort": "max",
                        },
                    },
                },
                {
                    "timestamp": "2026-08-27T10:00:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "rate_limits": self.limits(time.time()),
                    },
                },
            ]
            session.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n"
            )
            snapshot = tracker.refresh()
            self.assertEqual(snapshot.cwd, root / "next")
            self.assertEqual(
                snapshot.settings,
                codex_start.ModelSettings("gpt-5.6-sol", "max"),
            )
            self.assertEqual(snapshot.plan, "Plus")
            self.assertEqual(snapshot.limits["primary"]["used_percent"], 27)

    def test_terminal_filter_streams_drawing_queries_paste_and_clipboard(self):
        output_filter = codex_start.TerminalFilter()
        self.assertEqual(output_filter.feed(b"\x1b[?104"), b"")
        filtered = output_filter.feed(
            b"9hhello\x1b]0;child title\x07"
            b"\x1b[?1000;1006;2004h"
        )
        self.assertEqual(filtered, b"hello\x1b[?2004h")

        native = (
            b"\x1b[2J\x1b[H\x1b[1;20r"
            b"\x1b[?1;1004h\x1b[>3u\x1b[6npaint"
            b"\x1b]52;c;Y29weQ==\x1b\\"
        )
        self.assertEqual(output_filter.feed(native), native)
        self.assertEqual(output_filter.finish(), b"")

    def test_terminal_filter_blocks_only_alt_screen_and_mouse_ownership(self):
        output_filter = codex_start.TerminalFilter()
        blocked = output_filter.feed(
            b"\x1b[?47;1047;1049;9;1000;1001;1002;1003;"
            b"1005;1006;1007;1015;1016h"
        )
        self.assertEqual(blocked, b"")
        self.assertEqual(
            output_filter.feed(b"\x1b[?1;1049;1000;2004h"),
            b"\x1b[?1;2004h",
        )
        self.assertEqual(
            output_filter.feed(b"\x1b[?1049;1000;2004l"),
            b"\x1b[?2004l",
        )

    def test_wheel_never_becomes_codex_prompt_history_input(self):
        unsafe = b"\x1b[?1049h\x1b[?1h"
        self.assertEqual(
            vte_wheel_up_input(unsafe, rows=24),
            b"\x1bOA" * 3,
        )
        safe = codex_start.TerminalFilter().feed(unsafe)
        self.assertEqual(vte_wheel_up_input(safe, rows=24), b"")

    def test_right_click_and_selection_remain_owned_by_terminal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            startup = codex_start.terminal_initialization(
                self.snapshot(Path(temp_dir))
            )
        attempted_mouse = b"\x1b[?1000h\x1b[?1006h"
        filtered = codex_start.TerminalFilter().feed(attempted_mouse)
        self.assertIsNone(vte_mouse_button_input(startup + filtered, button=1))
        self.assertIsNone(vte_mouse_button_input(startup + filtered, button=3))
        self.assertNotIn(b"\x1b[?1000h", startup + filtered)
        self.assertNotIn(b"\x1b[?1006h", startup + filtered)

    def test_bracketed_paste_mode_remains_terminal_compatible(self):
        output_filter = codex_start.TerminalFilter()
        enabled = output_filter.feed(b"\x1b[?2004h")
        disabled = output_filter.feed(b"\x1b[?2004l")
        self.assertEqual(enabled, b"\x1b[?2004h")
        self.assertEqual(disabled, b"\x1b[?2004l")

    def test_terminal_restoration_does_not_erase_native_scrollback(self):
        restoration = codex_start.terminal_restoration()
        self.assertNotIn(b"\x1b[2J", restoration)
        self.assertNotIn(b"\x1b[3J", restoration)
        self.assertIn(b"\x1b[?2004l", restoration)
        self.assertIn(b"\x1b[?25h", restoration)
        self.assertTrue(restoration.endswith(b"\x1b[23;0t"))

    def test_independent_sessions_have_no_shared_mouse_or_focus_state(self):
        first = codex_start.TerminalFilter()
        second = codex_start.TerminalFilter()
        self.assertEqual(first.feed(b"\x1b[?1000hfirst"), b"first")
        self.assertEqual(second.feed(b"second"), b"second")
        self.assertEqual(second.pending, b"")

    def test_obsolete_viewport_and_x11_runtime_symbols_are_gone(self):
        for name in (
            "X11WheelCapture",
            "VirtualTerminal",
            "DashboardInput",
            "TerminalRenderer",
            "DashboardLayout",
            "run_dashboard",
        ):
            self.assertFalse(hasattr(codex_start, name), name)

    def test_codex_command_always_uses_native_inline_status(self):
        command = codex_start.codex_command(
            "/usr/bin/codex", extra_args=("--search",)
        )
        joined = " ".join(command)
        self.assertEqual(command[:2], ["/usr/bin/codex", "--approve-for-me"])
        self.assertIn("tui.alternate_screen=never", command)
        self.assertIn("current-dir", joined)
        self.assertIn("five-hour-limit", joined)
        self.assertIn("weekly-limit", joined)
        self.assertEqual(command[-1], "--search")

    def test_interactive_launch_uses_native_pty_relay_for_direct_terminals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = self.make_account(Path(temp_dir))
            terminal_input = mock.Mock()
            terminal_output = mock.Mock()
            terminal_input.isatty.return_value = True
            terminal_output.isatty.return_value = True
            with mock.patch.dict(
                os.environ,
                {"TERM": "xterm-256color"},
                clear=False,
            ), mock.patch.object(
                codex_start.sys, "stdin", terminal_input
            ), mock.patch.object(
                codex_start.sys, "stdout", terminal_output
            ), mock.patch.object(
                codex_start, "save_runtime"
            ), mock.patch.object(
                codex_start, "run_terminal", return_value=6
            ) as relay:
                result = codex_start.launch(
                    account,
                    plain=True,
                    codex_path="/fake/codex",
                )

        self.assertEqual(result, 6)
        launched_account, command, environment = relay.call_args.args
        self.assertIs(launched_account, account)
        self.assertEqual(environment["CODEX_HOME"], str(account.home))
        self.assertIn("tui.alternate_screen=never", command)

    def test_plain_launch_passes_correct_codex_home_and_exit_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account = self.make_account(root)
            capture = root / "capture.json"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['CAPTURE'], 'w') as output:\n"
                "    json.dump({'home': os.environ.get('CODEX_HOME'), "
                "'args': sys.argv[1:]}, output)\n"
                "raise SystemExit(7)\n"
            )
            fake_codex.chmod(0o755)
            output = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"CAPTURE": str(capture), "TERM": "dumb"},
                clear=False,
            ), mock.patch.object(
                codex_start, "save_runtime"
            ), contextlib.redirect_stdout(output):
                result = codex_start.launch(
                    account,
                    plain=True,
                    codex_path=str(fake_codex),
                    extra_args=("--search",),
                )
            captured = json.loads(capture.read_text())
            self.assertEqual(result, 7)
            self.assertEqual(captured["home"], str(account.home))
            self.assertEqual(captured["args"][0], "--approve-for-me")
            self.assertIn("--search", captured["args"])

    def test_main_routes_all_three_accounts_to_launch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            entries = self.make_accounts(Path(temp_dir))
            with mock.patch.object(
                codex_start, "load_accounts", return_value=entries
            ), mock.patch.object(codex_start, "launch", return_value=0) as launch:
                for account in entries:
                    self.assertEqual(codex_start.main([account.name]), 0)
                    self.assertEqual(launch.call_args.args[0], account)

    def test_version_does_not_create_or_load_account_configuration(self):
        self.assertEqual(codex_start.VERSION, "2.2.5")
        output = io.StringIO()
        with mock.patch.object(codex_start, "load_accounts") as load_accounts:
            with contextlib.redirect_stdout(output), self.assertRaises(
                SystemExit
            ) as error:
                codex_start.main(["--version"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn(codex_start.VERSION, output.getvalue())
        load_accounts.assert_not_called()

    def test_accounts_path_is_read_only_until_init_is_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = io.StringIO()
            with mock.patch.object(
                codex_start, "config_dir", return_value=root
            ), contextlib.redirect_stdout(output):
                self.assertEqual(codex_start.accounts_command(["--path"]), 0)
                self.assertFalse((root / "accounts.json").exists())
                self.assertEqual(
                    codex_start.accounts_command(["--init", "--path"]),
                    0,
                )
                self.assertTrue((root / "accounts.json").is_file())

    def test_accounts_add_cli_preserves_entries_and_sets_generic_preset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = codex_start.Account("alpha", root / "alpha-home")
            codex_start.save_accounts((existing,), root)
            output = io.StringIO()
            with mock.patch.object(
                codex_start, "config_dir", return_value=root
            ), contextlib.redirect_stdout(output):
                self.assertEqual(
                    codex_start.accounts_command(
                        [
                            "--add",
                            "beta",
                            str(root / "beta-home"),
                            "--theme",
                            "Forest",
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                [account.name for account in codex_start.load_accounts(root)],
                ["alpha", "beta"],
            )
            self.assertEqual(
                codex_start.ThemeStore(root).preset_for("beta"), "forest"
            )

    def test_theme_command_show_set_field_reset_copy_and_account_reset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entries = self.make_accounts(root)
            output = io.StringIO()
            with mock.patch.object(
                codex_start, "config_dir", return_value=root / "config"
            ), contextlib.redirect_stdout(output):
                self.assertEqual(
                    codex_start.theme_command(
                        ["account-two", "set", "weekly", "#123456"], entries
                    ),
                    0,
                )
                self.assertEqual(
                    codex_start.theme_command(
                        ["account-two", "set", "model", "50", "199", "255"],
                        entries,
                    ),
                    0,
                )
                store = codex_start.ThemeStore()
                self.assertEqual(store.theme_for("account-two")["weekly"], "#123456")
                self.assertEqual(store.theme_for("account-two")["model"], "#32c7ff")
                self.assertEqual(
                    codex_start.theme_command(["account-two", "show"], entries),
                    0,
                )
                self.assertIn("separators", output.getvalue())
                self.assertEqual(
                    codex_start.theme_command(
                        ["account-two", "reset", "weekly"], entries
                    ),
                    0,
                )
                self.assertEqual(
                    store.theme_for("account-two")["weekly"],
                    codex_start.DEFAULT_THEME["weekly"],
                )
                self.assertEqual(
                    codex_start.theme_command(
                        ["beta", "copy-from", "account-two"], entries
                    ),
                    0,
                )
                self.assertEqual(
                    store.theme_for("beta"),
                    store.theme_for("account-two"),
                )
                self.assertEqual(
                    codex_start.theme_command(["account-two", "reset"], entries),
                    0,
                )
                self.assertEqual(
                    store.theme_for("account-two"),
                    codex_start.DEFAULT_THEME,
                )

    def test_nested_pty_streams_native_scrollback_resize_input_signal_and_cleanup(self):
        if not hasattr(termios, "TIOCSCTTY"):
            self.skipTest("controlling PTYs are unavailable")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account = self.make_account(root)
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import fcntl, os, signal, struct, sys, termios, tty\n"
                "def size():\n"
                "    packed = fcntl.ioctl(1, termios.TIOCGWINSZ, b'\\0' * 8)\n"
                "    rows, columns, _, _ = struct.unpack('HHHH', packed)\n"
                "    return rows, columns\n"
                "def report(*_args):\n"
                "    rows, columns = size()\n"
                "    os.write(1, f'\\r\\nCHILD-SIZE:{rows}x{columns}'.encode())\n"
                "def terminate(signum, _frame):\n"
                "    os.write(1, f'\\r\\nCHILD-SIGNAL:{signum}'.encode())\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGWINCH, report)\n"
                "signal.signal(signal.SIGTERM, terminate)\n"
                "tty.setraw(0)\n"
                "report()\n"
                "os.write(1, b'\\x1b[?1049h\\x1b[?1000;1006h')\n"
                "os.write(1, b'\\x1b]0;child-owned-title\\x07')\n"
                "for number in range(45):\n"
                "    os.write(1, f'\\r\\nRESUMED-LINE:{number:02d}'.encode())\n"
                "for number in range(6):\n"
                "    os.write(1, f'\\r\\nFRESH-LINE:{number:02d}'.encode())\n"
                "os.write(1, b'\\r\\n\\x1b[?2004hREADY')\n"
                "data = os.read(0, 4096)\n"
                "os.write(1, f'\\r\\nCHILD-INPUT:{data.hex()}'.encode())\n"
                "while True:\n"
                "    signal.pause()\n"
            )
            fake_codex.chmod(0o755)
            runner = root / "runner.py"
            project = Path(codex_start.__file__).resolve().parent
            runner.write_text(
                "import os, sys\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {str(project)!r})\n"
                "import codex_start\n"
                "account = codex_start.Account("
                f"'alpha', Path({str(account.home)!r}))\n"
                "raise SystemExit(codex_start.run_terminal("
                f"account, [{str(fake_codex)!r}], os.environ.copy(), "
                "rate_limit_reader_factory=lambda *_args: None))\n"
            )
            master_fd, slave_fd = pty.openpty()
            codex_start.set_window_size(slave_fd, 24, 120)
            original_lflag = termios.tcgetattr(master_fd)[3]
            os.write(slave_fd, b"PRE-CODEX-SHELL\\r\\n")

            def child_setup():
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

            process = subprocess.Popen(
                [sys.executable, str(runner)],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=project,
                close_fds=True,
                env={**os.environ, "TERM": "xterm-256color"},
                preexec_fn=child_setup,
            )
            os.close(slave_fd)
            output = bytearray()

            def read_until(marker: bytes, seconds: float = 8) -> bool:
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    readable, _writable, _exceptional = select.select(
                        [master_fd], [], [], 0.1
                    )
                    if readable:
                        try:
                            chunk = os.read(master_fd, 65_536)
                        except OSError:
                            chunk = b""
                        if chunk:
                            output.extend(chunk)
                            if marker in output:
                                return True
                    if process.poll() is not None:
                        return marker in output
                return marker in output

            try:
                self.assertTrue(
                    read_until(b"READY"),
                    output.decode("utf-8", "replace"),
                )
                self.assertIn(b"PRE-CODEX-SHELL", output)
                shell = output.index(b"PRE-CODEX-SHELL")
                erase = output.index(b"\x1b[2J\x1b[3J", shell)
                resumed = output.index(b"RESUMED-LINE:00")
                self.assertLess(shell, erase)
                self.assertLess(erase, resumed)
                self.assertEqual(output.count(b"\x1b[3J"), 1)
                self.assertIn(b"RESUMED-LINE:44", output)
                self.assertIn(b"FRESH-LINE:05", output)
                self.assertIn(b"CHILD-SIZE:24x120", output)
                self.assertNotIn(b"\x1b[?1049h", output)
                self.assertNotIn(b"\x1b[?1000;1006h", output)
                self.assertNotIn(b"child-owned-title", output)
                self.assertIn(b"\x1b[?2004h", output)
                self.assertIsNone(
                    vte_mouse_button_input(bytes(output), button=3)
                )
                self.assertEqual(
                    vte_wheel_up_input(bytes(output), rows=24),
                    b"",
                )

                os.write(master_fd, b"x")
                self.assertTrue(
                    read_until(b"CHILD-INPUT:78"),
                    output.decode("utf-8", "replace"),
                )

                codex_start.set_window_size(master_fd, 30, 100)
                self.assertTrue(
                    read_until(b"CHILD-SIZE:30x100"),
                    output.decode("utf-8", "replace"),
                )

                process.send_signal(signal.SIGTERM)
                self.assertTrue(
                    read_until(b"CHILD-SIGNAL:15"),
                    output.decode("utf-8", "replace"),
                )
                self.assertTrue(
                    read_until(b"\x1b[23;0t"),
                    output.decode("utf-8", "replace"),
                )
                returncode = process.wait(timeout=3)
                restored_lflag = termios.tcgetattr(master_fd)[3]
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
                os.close(master_fd)

            terminal_flags = termios.ECHO | termios.ICANON | termios.ISIG
            self.assertEqual(returncode, 0, output.decode("utf-8", "replace"))
            self.assertEqual(
                restored_lflag & terminal_flags,
                original_lflag & terminal_flags,
            )
            self.assertNotIn(b"HISTORY", output)
            self.assertNotIn(b"CHILD-INPUT:1b5b41", output)
            self.assertIn(b"\x1b[?2004l", output)
            self.assertIn(b"\x1b[?25h", output)

if __name__ == "__main__":
    unittest.main()
