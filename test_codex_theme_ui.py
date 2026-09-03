#!/usr/bin/env python3

import contextlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import codex_start
import codex_theme_ui


class ThemeUITests(unittest.TestCase):
    TOKEN = "test-theme-ui-token"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.entries = (
            codex_start.Account("alpha", self.root / ".codex-alpha"),
            codex_start.Account("beta", self.root / ".codex-beta"),
            codex_start.Account(
                "account-two", self.root / ".codex-account-two"
            ),
            codex_start.Account("future-account", self.root / ".codex-future"),
        )
        self.store = codex_start.ThemeStore(self.root / "config")
        self.server = codex_theme_ui.create_theme_ui_server(
            self.entries, self.store, token=self.TOKEN
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(
        self,
        method,
        path,
        document=None,
        *,
        token=TOKEN,
        content_type="application/json",
        raw_body=None,
    ):
        connection = http.client.HTTPConnection(
            codex_theme_ui.THEME_UI_HOST,
            self.server.server_port,
            timeout=2,
        )
        headers = {}
        if token is not None:
            headers["X-Codex-Start-Token"] = token
        body = raw_body
        if document is not None:
            body = json.dumps(document)
        if body is not None:
            headers["Content-Type"] = content_type
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        payload = (
            json.loads(response_body)
            if response_headers.get("Content-Type", "").startswith(
                "application/json"
            )
            else response_body.decode("utf-8")
        )
        return response.status, payload, response_headers

    def test_loading_themes_and_future_configured_accounts(self):
        self.store.set_color("default", "plan", "green")
        self.store.set_color("future-account", "account", "#123456")

        status, state, _headers = self.request("GET", "/api/state")

        self.assertEqual(status, 200)
        self.assertEqual(
            [preset["label"] for preset in state["presets"]],
            ["Default", "Crimson", "Cobalt", "Forest", "Graphite"],
        )
        self.assertEqual(
            state["terminal_background_modes"],
            ["inherit", "neutral", "themed"],
        )
        self.assertEqual(
            [account["id"] for account in state["accounts"]],
            [
                "default",
                "alpha",
                "beta",
                "account-two",
                "future-account",
            ],
        )
        future = state["themes"]["future-account"]
        self.assertEqual(future["colors"]["account"], "#123456")
        self.assertEqual(future["colors"]["plan"], "#0dbc79")
        self.assertFalse(future["inherited"]["account"])
        self.assertTrue(future["inherited"]["plan"])
        self.assertEqual(
            state["themes"]["default"]["reset_colors"]["plan"],
            codex_start.DEFAULT_THEME["plan"],
        )

        page_status, page, headers = self.request(
            "GET", "/", token=None
        )
        self.assertEqual(page_status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("gpt-5.6-sol max", page)
        self.assertIn("native VTE scrollback", page)
        self.assertIn('class="status-rail-preview"', page)
        self.assertIn("82%", page)
        self.assertIn('class="reset-meta" data-color="text"> 14:14', page)
        self.assertIn("64%", page)
        self.assertIn('class="reset-meta" data-color="text"> Fri 09:03', page)
        self.assertIn('id="preset-select"', page)
        self.assertIn('id="terminal-background-mode"', page)
        self.assertIn('class="path-prefix"', page)
        self.assertIn("Copy Full Transcript", page)
        self.assertNotIn('class="terminal-chrome"', page)
        self.assertNotIn('class="palette-sample"', page)
        self.assertNotIn("top + bottom status rows", page)
        self.assertNotIn("status bars stay pinned", page)
        self.assertNotIn('id="layout-left"', page)
        self.assertIn(self.TOKEN, page)
        self.assertNotIn(codex_theme_ui.TOKEN_PLACEHOLDER, page)

    def test_preset_and_terminal_background_actions_persist_additively(self):
        status, state, _headers = self.request(
            "POST",
            "/api/theme",
            {"action": "set-preset", "account": "beta", "preset": "Cobalt"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(state["themes"]["beta"]["preset"], "cobalt")

        status, state, _headers = self.request(
            "POST",
            "/api/theme",
            {
                "action": "set-presentation",
                "account": "beta",
                "terminal_background_mode": "neutral",
                "neutral_terminal_background": "#121416",
            },
        )
        self.assertEqual(status, 200)
        presentation = state["themes"]["beta"]["presentation"]
        self.assertEqual(presentation["terminal_background_mode"], "neutral")
        self.assertEqual(
            presentation["neutral_terminal_background"], "#121416"
        )
        document = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(
            document["accounts"]["beta"],
            {
                "preset": "cobalt",
                "terminal_background_mode": "neutral",
                "neutral_terminal_background": "#121416",
            },
        )

    def test_saving_colors_is_atomic_and_account_isolated(self):
        status, _state, _headers = self.request(
            "POST",
            "/api/theme",
            {
                "action": "save",
                "account": "beta",
                "colors": {"plan": "#112233", "model": "#abcdef"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.store.theme_for("beta")["plan"], "#112233"
        )
        self.assertEqual(
            self.store.theme_for("beta")["model"], "#abcdef"
        )
        self.assertEqual(
            self.store.theme_for("alpha")["plan"],
            codex_start.DEFAULT_THEME["plan"],
        )

        before = self.store.path.read_text(encoding="utf-8")
        status, error, _headers = self.request(
            "POST",
            "/api/theme",
            {
                "action": "save",
                "account": "beta",
                "colors": {"plan": "#445566", "model": "#bad"},
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("Invalid color", error["error"])
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), before)

    def test_reset_field_and_entire_account_use_store_inheritance(self):
        self.store.set_color("default", "weekly", "#102030")
        self.store.set_color("account-two", "weekly", "#abcdef")
        self.store.set_color("account-two", "model", "#123456")

        status, state, _headers = self.request(
            "POST",
            "/api/theme",
            {"action": "reset", "account": "account-two", "field": "weekly"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            state["themes"]["account-two"]["colors"]["weekly"], "#102030"
        )
        self.assertTrue(
            state["themes"]["account-two"]["inherited"]["weekly"]
        )
        self.assertEqual(
            self.store.theme_for("account-two")["model"], "#123456"
        )

        status, state, _headers = self.request(
            "POST",
            "/api/theme",
            {"action": "reset", "account": "account-two"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.store.theme_for("account-two"),
            self.store.theme_for("alpha"),
        )
        self.assertTrue(
            all(state["themes"]["account-two"]["inherited"].values())
        )

    def test_copy_from_uses_effective_source_theme(self):
        self.store.set_color("default", "labels", "#101112")
        self.store.set_color("alpha", "account", "#aabbcc")
        self.store.set_color("beta", "account", "#123456")

        status, _state, _headers = self.request(
            "POST",
            "/api/theme",
            {
                "action": "copy",
                "account": "beta",
                "source": "alpha",
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            self.store.theme_for("beta"),
            self.store.theme_for("alpha"),
        )
        self.store.set_color("alpha", "account", "#ffffff")
        self.assertEqual(
            self.store.theme_for("beta")["account"], "#aabbcc"
        )

    def test_server_is_loopback_only_and_serves_no_filesystem_paths(self):
        self.assertEqual(
            self.server.server_address[0], codex_theme_ui.THEME_UI_HOST
        )
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertGreater(self.server.server_port, 0)

        status, _error, _headers = self.request(
            "GET", "/api/state", token=None
        )
        self.assertEqual(status, 403)
        status, _error, _headers = self.request(
            "GET", "/../../etc/passwd", token=None
        )
        self.assertEqual(status, 404)
        status, _error, _headers = self.request(
            "GET", "/codex_start.py", token=None
        )
        self.assertEqual(status, 404)

    def test_api_rejects_invalid_shapes_accounts_fields_and_sources(self):
        requests = (
            (
                {"action": "save", "account": "unknown", "colors": {"plan": "#ffffff"}},
                400,
            ),
            (
                {"action": "save", "account": "beta", "colors": {"bogus": "#ffffff"}},
                400,
            ),
            (
                {"action": "save", "account": "beta", "colors": {}},
                400,
            ),
            (
                {"action": "reset", "account": "beta", "field": "bogus"},
                400,
            ),
            (
                {"action": "copy", "account": "beta", "source": "unknown"},
                400,
            ),
            (
                {"action": "copy", "account": "default", "source": "account-two"},
                400,
            ),
            (
                {"action": "reset", "account": "beta", "extra": True},
                400,
            ),
            (
                {"action": "set-preset", "account": "beta", "preset": "private"},
                409,
            ),
            (
                {
                    "action": "set-presentation",
                    "account": "beta",
                    "terminal_background_mode": "painted",
                },
                400,
            ),
            ({"action": "launch", "account": "beta"}, 400),
        )
        for document, expected in requests:
            with self.subTest(document=document):
                status, _error, _headers = self.request(
                    "POST", "/api/theme", document
                )
                self.assertEqual(status, expected)

        status, _error, _headers = self.request(
            "POST",
            "/api/theme",
            raw_body="not json",
        )
        self.assertEqual(status, 400)
        status, _error, _headers = self.request(
            "POST",
            "/api/theme",
            document={"action": "reset", "account": "beta"},
            content_type="text/plain",
        )
        self.assertEqual(status, 415)
        status, _error, _headers = self.request(
            "POST",
            "/api/theme",
            {"action": "reset", "account": "beta"},
            token="wrong-token",
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.store.path.exists())

    def test_cli_and_ui_round_trip_the_same_theme_file(self):
        output = io.StringIO()
        with mock.patch.object(
            codex_start, "config_dir", return_value=self.store.root
        ), contextlib.redirect_stdout(output):
            self.assertEqual(
                codex_start.theme_command(
                    ["beta", "set", "five_hour", "57"], self.entries
                ),
                0,
            )

        status, state, _headers = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(
            state["themes"]["beta"]["source_values"]["five_hour"],
            "57",
        )
        self.assertEqual(
            state["themes"]["beta"]["colors"]["five_hour"],
            codex_start.color_to_hex("57", "#000000"),
        )

        status, _state, _headers = self.request(
            "POST",
            "/api/theme",
            {
                "action": "save",
                "account": "beta",
                "colors": {"weekly": "#765432"},
            },
        )
        self.assertEqual(status, 200)
        output = io.StringIO()
        with mock.patch.object(
            codex_start, "config_dir", return_value=self.store.root
        ), contextlib.redirect_stdout(output):
            self.assertEqual(
                codex_start.theme_command(
                    ["beta", "show"], self.entries
                ),
                0,
            )
        self.assertIn("weekly       #765432", output.getvalue())
        self.assertEqual(
            json.loads(self.store.path.read_text(encoding="utf-8"))[
                "accounts"
            ]["beta"]["weekly"],
            "#765432",
        )

    def test_command_opens_browser_by_default_and_honors_no_open(self):
        fake_server = mock.Mock()
        fake_server.server_port = 43123
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(
            codex_theme_ui, "load_accounts", return_value=self.entries
        ), mock.patch.object(
            codex_theme_ui, "create_theme_ui_server", return_value=fake_server
        ), mock.patch(
            "webbrowser.open", return_value=True
        ) as browser, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex_theme_ui.theme_ui_command([]), 0)
            browser.assert_called_once_with(
                "http://127.0.0.1:43123/", new=2
            )
            fake_server.server_close.assert_called_once()

        fake_server.reset_mock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(
            codex_theme_ui, "load_accounts", return_value=self.entries
        ), mock.patch.object(
            codex_theme_ui, "create_theme_ui_server", return_value=fake_server
        ), mock.patch(
            "webbrowser.open"
        ) as browser, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                codex_theme_ui.theme_ui_command(["--no-open"]), 0
            )
            browser.assert_not_called()
            fake_server.server_close.assert_called_once()

    def test_main_routes_theme_ui_without_entering_launcher_path(self):
        with mock.patch.object(
            codex_theme_ui, "theme_ui_command", return_value=7
        ) as command, mock.patch.object(
            codex_start, "load_accounts"
        ) as load_accounts:
            self.assertEqual(
                codex_start.main(["theme-ui", "--no-open"]), 7
            )
        command.assert_called_once_with(["--no-open"])
        load_accounts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
