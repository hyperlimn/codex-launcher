#!/usr/bin/env python3

import io
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import codex_start
import codex_terminal_bridge
import codex_terminal_theme
import codex_terminal_ui


class TerminalPresentationTests(unittest.TestCase):
    def make_account(self, root: Path, name: str = "beta"):
        home = root / f".codex-{name}"
        home.mkdir(parents=True)
        return codex_start.Account(name, home)

    @staticmethod
    def snapshot(
        account: codex_start.Account,
        cwd: Path,
        *,
        now: float,
        used_five: int = 14,
        used_week: int = 44,
        model: str = "gpt-5.6-sol",
        effort: str = "xhigh",
    ) -> codex_start.StatusSnapshot:
        five_reset = datetime(2026, 9, 2, 13, 1).timestamp()
        week_reset = datetime(2026, 9, 8, 12, 18).timestamp()
        limits = {
            "limit_id": "codex",
            "plan_type": "plus",
            "primary": {
                "used_percent": used_five,
                "window_minutes": 300,
                "resets_at": int(five_reset),
            },
            "secondary": {
                "used_percent": used_week,
                "window_minutes": 10_080,
                "resets_at": int(week_reset),
            },
        }
        return codex_start.StatusSnapshot(
            account,
            "Plus",
            codex_start.ModelSettings(model, effort),
            cwd,
            limits,
            now,
        )

    def test_exact_packaged_default_theme_field_mapping(self):
        expected = {
            "labels": "#c8c8c8",
            "directory": "#19bdf2",
            "account": "#b26cff",
            "plan": "#39d353",
            "model": "#19bdf2",
            "five_hour": "#39d353",
            "weekly": "#2997ff",
            "reset": "#ffb000",
            "separators": "#555b61",
            "text": "#81868f",
            "background": "#080a0c",
        }
        self.assertEqual(codex_terminal_theme.DEFAULT_THEME, expected)
        self.assertEqual(codex_start.DEFAULT_THEME, expected)
        self.assertEqual(
            codex_terminal_theme.ThemeModel.default().as_dict(), expected
        )

    def test_packaged_presets_are_exactly_the_generic_public_set(self):
        self.assertEqual(
            [
                codex_terminal_theme.THEME_PRESETS[name].label
                for name in codex_terminal_theme.PRESET_ORDER
            ],
            ["Default", "Crimson", "Cobalt", "Forest", "Graphite"],
        )

    def test_legacy_v1_theme_survives_preset_and_override_composition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = codex_start.ThemeStore(root)
            store.path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "default": {"labels": "#101112"},
                        "accounts": {"alpha": {"account": "#abcdef"}},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(store.theme_for("alpha")["account"], "#abcdef")
            store.set_preset("alpha", "cobalt")
            model = store.theme_model_for("alpha")
            document = json.loads(store.path.read_text(encoding="utf-8"))

        self.assertEqual(model.preset, "cobalt")
        self.assertEqual(model.labels, "#101112")
        self.assertEqual(model.account, "#abcdef")
        self.assertEqual(
            model.weekly,
            codex_terminal_theme.THEME_PRESETS["cobalt"].colors["weekly"],
        )
        self.assertEqual(document["version"], 2)
        self.assertEqual(
            document["accounts"]["alpha"],
            {"account": "#abcdef", "preset": "cobalt"},
        )

    def test_preset_cycle_persists_without_copying_packaged_palette(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = codex_start.ThemeStore(Path(temp_dir))
            self.assertEqual(store.cycle_preset("alpha"), "crimson")
            self.assertEqual(store.cycle_preset("alpha"), "cobalt")
            document = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(
            document["accounts"]["alpha"], {"preset": "cobalt"}
        )

    def test_terminal_background_modes_are_independent_from_rail_color(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = codex_start.ThemeStore(Path(temp_dir))
            store.set_preset("alpha", "crimson")
            store.set_neutral_terminal_background("alpha", "#121416")

            store.set_terminal_background_mode("alpha", "neutral")
            neutral = store.theme_model_for("alpha")
            store.set_terminal_background_mode("alpha", "themed")
            themed = store.theme_model_for("alpha")
            store.set_terminal_background_mode("alpha", "inherit")
            inherited = store.theme_model_for("alpha")

        self.assertEqual(neutral.terminal_background_color(), "#121416")
        self.assertEqual(themed.terminal_background_color(), themed.background)
        self.assertIsNone(inherited.terminal_background_color())
        self.assertEqual(neutral.background, themed.background)

    def test_status_model_generation_is_independent_of_gtk(self):
        now = datetime(2026, 9, 2, 10, 0).timestamp()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account = self.make_account(root)
            status = codex_start.terminal_status_model(
                self.snapshot(account, root / "project", now=now), now
            )
        self.assertEqual(status.account, "beta")
        self.assertEqual(status.plan, "Plus")
        self.assertEqual(status.model, "gpt-5.6-sol xhigh")
        self.assertEqual(status.five_hour, "86%")
        self.assertEqual(status.five_hour_reset, "13:01")
        self.assertEqual(status.weekly, "56%")
        self.assertEqual(status.weekly_reset, "Tue 12:18")

    def test_combined_five_hour_and_week_reset_representation(self):
        status = codex_terminal_theme.StatusModel(
            directory=codex_terminal_theme.DirectoryPresentation(
                "~/Desktop/", "project", "~/Desktop/project"
            ),
            account="beta",
            plan="Plus",
            model="gpt-5.6-sol xhigh",
            five_hour="86%",
            five_hour_reset="13:01",
            weekly="56%",
            weekly_reset="Tue 12:18",
        )
        self.assertEqual(
            status.compact_limits(),
            "5h 86% 13:01 · week 56% Tue 12:18",
        )
        rail = "".join(
            segment.text
            for segment in codex_terminal_theme.status_rail_segments(status)
        )
        self.assertIn("5h: 86%  13:01", rail)
        self.assertIn("week: 56%  Tue 12:18", rail)
        self.assertNotIn("reset:", rail)

        groups = {
            group.name: group
            for group in codex_terminal_theme.status_rail_groups(status)
        }
        for name in ("five_hour", "weekly"):
            usage = next(
                item for item in groups[name].segments if item.semantic == "usage"
            )
            reset = next(
                item for item in groups[name].segments if item.semantic == "reset"
            )
            self.assertTrue(usage.bold)
            self.assertTrue(reset.small)
            self.assertEqual(reset.theme_field, "text")

    def test_compact_directory_and_responsive_grouping_are_semantic(self):
        home = Path("/tmp/example-home")
        directory = codex_terminal_theme.DirectoryPresentation.from_path(
            home / "work" / "Desktop" / "codex-project", home
        )
        self.assertEqual(directory.prefix, "~/…/Desktop/")
        self.assertEqual(directory.name, "codex-project")
        self.assertEqual(
            directory.full, "~/work/Desktop/codex-project"
        )
        self.assertEqual(
            codex_terminal_theme.responsive_rail_layout(800, 800),
            codex_terminal_theme.WIDE_RAIL_LAYOUT,
        )
        narrow = codex_terminal_theme.responsive_rail_layout(799, 800)
        self.assertEqual(narrow, codex_terminal_theme.NARROW_RAIL_LAYOUT)
        self.assertEqual(
            narrow.rows,
            (
                ("directory", "identity", "model"),
                ("five_hour", "weekly", "actions"),
            ),
        )

    def test_transcript_exporter_is_write_only_and_targets_one_session(self):
        first_writes: list[bytes] = []
        second_writes: list[bytes] = []
        exporter = codex_terminal_theme.TranscriptExporter()
        first = codex_terminal_theme.TranscriptSession("one", first_writes.append)
        second = codex_terminal_theme.TranscriptSession("two", second_writes.append)

        result = exporter.copy_current(second)

        self.assertTrue(result.succeeded)
        self.assertEqual(first_writes, [])
        self.assertEqual(second_writes, [b"/export\r\x1b[B\r"])
        self.assertEqual(
            set(vars(second)), {"identifier", "write_input", "active"}
        )
        self.assertFalse(
            any(name.startswith("read") for name in vars(second))
        )

    def test_transcript_host_action_has_no_vte_scraping_path(self):
        source = inspect.getsource(codex_terminal_ui)
        self.assertIn("self.terminal.feed_child", source)
        for forbidden in (
            "get_text(",
            "get_text_range(",
            "get_text_range_format(",
            "get_scrollback",
        ):
            self.assertNotIn(forbidden, source)

    def test_terminal_host_launch_constructs_argv_environment_and_cwd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account = self.make_account(root)
            spec = codex_start.build_terminal_host_launch(
                account,
                "/opt/codex",
                extra_args=("resume", "thread-id"),
                environment={"PATH": "/bin", "TERM": "xterm-256color"},
                cwd=root / "project",
                python_executable="/usr/bin/python3",
                bridge_path=root / "bridge.py",
            )
        self.assertEqual(spec.cwd, root / "project")
        self.assertEqual(spec.codex_path, "/opt/codex")
        self.assertEqual(spec.environment["CODEX_HOME"], str(account.home))
        self.assertEqual(
            spec.environment[codex_start.HOSTED_ENVIRONMENT], "1"
        )
        self.assertEqual(
            spec.argv,
            (
                "/usr/bin/python3",
                str(root / "bridge.py"),
                "--account-name",
                "beta",
                "--account-home",
                str(account.home),
                "--codex",
                "/opt/codex",
                "--",
                "resume",
                "thread-id",
            ),
        )

    def test_host_bridge_is_not_a_recursive_codex_start_launch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = self.make_account(Path(temp_dir))
            spec = codex_start.build_terminal_host_launch(
                account, "/opt/codex"
            )
        self.assertTrue(spec.argv[1].endswith("codex_terminal_bridge.py"))
        self.assertNotIn("--plain", spec.argv)
        self.assertNotIn("codex-start", Path(spec.argv[1]).name)

    def test_bridge_preserves_codex_home_args_and_uses_transparent_pty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = self.make_account(Path(temp_dir))
            with mock.patch.object(
                codex_terminal_bridge.codex_start,
                "run_terminal",
                return_value=7,
            ) as relay, mock.patch.dict(os.environ, {}, clear=True):
                result = codex_terminal_bridge.main(
                    [
                        "--account-name",
                        account.name,
                        "--account-home",
                        str(account.home),
                        "--codex",
                        "/opt/codex",
                        "--",
                        "resume",
                        "thread-id",
                    ]
                )
        self.assertEqual(result, 7)
        launched_account, command, environment = relay.call_args.args
        self.assertEqual(launched_account, account)
        self.assertEqual(environment["CODEX_HOME"], str(account.home))
        self.assertIn("tui.alternate_screen=never", command)
        self.assertEqual(command[-2:], ["resume", "thread-id"])
        self.assertIn("rate_limit_reader_factory", relay.call_args.kwargs)

    def _interactive_launch(self, account, *, plain=False, hosted=False):
        terminal_input = mock.Mock()
        terminal_output = mock.Mock()
        terminal_input.isatty.return_value = True
        terminal_output.isatty.return_value = True
        environment = {"TERM": "xterm-256color"}
        if hosted:
            environment[codex_start.HOSTED_ENVIRONMENT] = "1"
        patches = (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(codex_start.sys, "stdin", terminal_input),
            mock.patch.object(codex_start.sys, "stdout", terminal_output),
            mock.patch.object(codex_start, "save_runtime"),
            mock.patch.object(codex_start, "initial_snapshot"),
            mock.patch.object(codex_start, "_launch_terminal_host"),
            mock.patch.object(codex_start, "run_terminal", return_value=6),
        )
        snapshot = codex_start.StatusSnapshot(
            account,
            "Plus",
            codex_start.ModelSettings("gpt-5.6-sol", "xhigh"),
            Path("/tmp/project"),
            None,
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4] as initial, patches[5] as host, patches[6] as relay:
            initial.return_value = snapshot
            result = codex_start.launch(
                account,
                plain=plain,
                codex_path="/opt/codex",
            )
        return result, host, relay

    def test_default_interactive_launch_uses_themed_host(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = self.make_account(Path(temp_dir))
            terminal_input = mock.Mock()
            terminal_output = mock.Mock()
            terminal_input.isatty.return_value = True
            terminal_output.isatty.return_value = True
            snapshot = codex_start.initial_snapshot(account, Path("/tmp/project"))
            with mock.patch.dict(
                os.environ, {"TERM": "xterm-256color"}, clear=True
            ), mock.patch.object(
                codex_start.sys, "stdin", terminal_input
            ), mock.patch.object(
                codex_start.sys, "stdout", terminal_output
            ), mock.patch.object(
                codex_start, "save_runtime"
            ), mock.patch.object(
                codex_start, "initial_snapshot", return_value=snapshot
            ), mock.patch.object(
                codex_start, "_launch_terminal_host", return_value=9
            ) as host, mock.patch.object(
                codex_start, "run_terminal"
            ) as relay:
                result = codex_start.launch(
                    account, codex_path="/opt/codex"
                )
        self.assertEqual(result, 9)
        self.assertEqual(host.call_count, 1)
        relay.assert_not_called()

    def test_plain_and_already_hosted_launches_do_not_recurse(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = self.make_account(Path(temp_dir))
            for plain, hosted in ((True, False), (False, True)):
                with self.subTest(plain=plain, hosted=hosted):
                    result, host, relay = self._interactive_launch(
                        account, plain=plain, hosted=hosted
                    )
                    self.assertEqual(result, 6)
                    host.assert_not_called()
                    relay.assert_called_once()

    def test_gtk_vte_unavailable_falls_back_to_native_terminal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            account = self.make_account(Path(temp_dir))
            terminal_input = mock.Mock()
            terminal_output = mock.Mock()
            terminal_input.isatty.return_value = True
            terminal_output.isatty.return_value = True
            with mock.patch.dict(
                os.environ, {"TERM": "xterm-256color"}, clear=True
            ), mock.patch.object(
                codex_start.sys, "stdin", terminal_input
            ), mock.patch.object(
                codex_start.sys, "stdout", terminal_output
            ), mock.patch.object(
                codex_start, "save_runtime"
            ), mock.patch.object(
                codex_start, "_launch_terminal_host", return_value=None
            ) as host, mock.patch.object(
                codex_start, "run_terminal", return_value=4
            ) as relay:
                result = codex_start.launch(
                    account, codex_path="/opt/codex"
                )
        self.assertEqual(result, 4)
        host.assert_called_once()
        relay.assert_called_once()

    def test_ui_dependency_failure_returns_plain_fallback_signal(self):
        launch_spec = mock.Mock()
        snapshot = mock.Mock()
        store = mock.Mock()
        with mock.patch.object(
            codex_terminal_ui,
            "_load_gtk",
            side_effect=codex_terminal_ui.TerminalUIUnavailable("missing"),
        ):
            self.assertIsNone(
                codex_terminal_ui.launch_terminal_host(
                    launch_spec, snapshot, store
                )
            )

    def test_version_and_status_paths_never_dispatch_gtk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account = self.make_account(root)
            with mock.patch.object(
                codex_start, "load_accounts", return_value=(account,)
            ), mock.patch.object(
                codex_start, "_launch_terminal_host"
            ) as host, mock.patch.object(
                sys, "stdout", io.StringIO()
            ):
                with self.assertRaises(SystemExit) as version:
                    codex_start.main(["--version"])
                self.assertEqual(version.exception.code, 0)
                self.assertEqual(
                    codex_start.main(["--status", account.name]), 0
                )
        host.assert_not_called()
        self.assertNotIn("gi", codex_terminal_ui.__dict__)

    def test_status_rail_receives_updates_without_owning_a_pty(self):
        first = codex_terminal_theme.StatusModel(
            codex_terminal_theme.DirectoryPresentation("~/", "one", "~/one"),
            "beta", "Plus", "gpt-old low",
            "90%", "11:00", "80%", "Fri 09:00"
        )
        second = codex_terminal_theme.StatusModel(
            codex_terminal_theme.DirectoryPresentation("~/", "two", "~/two"),
            "beta", "Plus", "gpt-5.6-sol max",
            "70%", "12:00", "60%", "Tue 12:18"
        )
        updates = []
        rail = codex_terminal_theme.StatusRail(
            first,
            codex_terminal_theme.ThemeModel.default(),
            lambda status, theme, segments: updates.append(
                (status, theme, segments)
            ),
        )
        rail.update(status=second)
        self.assertEqual(updates[-1][0], second)
        self.assertIn("~/two", "".join(item.text for item in rail.segments))
        self.assertFalse(
            any("pty" in name.casefold() for name in vars(rail))
        )

    def test_account_theme_switching_uses_same_persistent_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = codex_start.ThemeStore(Path(temp_dir))
            store.set_color("beta", "account", "#112233")
            store.set_color("account-two", "account", "#abcdef")
            self.assertEqual(
                store.theme_model_for("beta").account, "#112233"
            )
            self.assertEqual(
                store.theme_model_for("account-two").account, "#abcdef"
            )

    def test_directory_model_and_rate_updates_propagate_to_rail(self):
        now = datetime(2026, 9, 2, 10, 0).timestamp()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account = self.make_account(root)
            first = codex_start.terminal_status_model(
                self.snapshot(account, root / "one", now=now), now
            )
            second = codex_start.terminal_status_model(
                self.snapshot(
                    account,
                    root / "two",
                    now=now,
                    used_five=30,
                    used_week=40,
                    model="gpt-next",
                    effort="max",
                ),
                now,
            )
        rail = codex_terminal_theme.StatusRail(
            first, codex_terminal_theme.ThemeModel.default()
        )
        rail.update(status=second)
        text = "".join(segment.text for segment in rail.segments)
        self.assertEqual(second.directory.full, str(root / "two"))
        self.assertIn(second.directory.prefix, text)
        self.assertIn(second.directory.name, text)
        self.assertIn("gpt-next max", text)
        self.assertIn("5h: 70%", text)
        self.assertIn("week: 60%", text)

    def test_removed_terminal_architecture_is_not_reintroduced(self):
        for module in (
            codex_start,
            codex_terminal_theme,
            codex_terminal_ui,
            codex_terminal_bridge,
        ):
            for name in (
                "X11WheelCapture",
                "VirtualTerminal",
                "TerminalRenderer",
                "run_dashboard",
            ):
                self.assertFalse(hasattr(module, name), f"{module.__name__}.{name}")


if __name__ == "__main__":
    unittest.main()
