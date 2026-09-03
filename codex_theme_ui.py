"""Local-only browser editor for codex-start account themes."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from codex_start import (
    Account,
    DEFAULT_THEME,
    PRESET_ORDER,
    TERMINAL_BACKGROUND_MODES,
    THEME_FIELDS,
    THEME_PRESETS,
    ThemeStore,
    color_to_hex,
    load_accounts,
    valid_color,
)


THEME_UI_HOST = "127.0.0.1"
MAX_REQUEST_BYTES = 64 * 1024
TOKEN_PLACEHOLDER = "__CODEX_START_THEME_UI_TOKEN__"


def theme_ui_state(
    store: ThemeStore, entries: Sequence[Account]
) -> dict[str, Any]:
    """Build the browser's filesystem-free view of configured themes."""
    accounts = [
        {"id": "default", "label": "Global defaults", "kind": "global"}
    ]
    accounts.extend(
        {"id": account.name, "label": account.name, "kind": "account"}
        for account in entries
    )
    themes: dict[str, Any] = {}
    for item in accounts:
        account_name = item["id"]
        details = store.theme_details(account_name)
        themes[account_name] = {
            "colors": {
                field: color_to_hex(
                    details["colors"][field], DEFAULT_THEME[field]
                )
                for field in THEME_FIELDS
            },
            "source_values": {
                field: details["colors"][field] for field in THEME_FIELDS
            },
            "reset_colors": {
                field: color_to_hex(
                    details["reset_colors"][field], DEFAULT_THEME[field]
                )
                for field in THEME_FIELDS
            },
            "inherited": details["inherited"],
            "preset": details["preset"],
            "presentation": details["presentation"],
        }
    return {
        "version": 2,
        "fields": list(THEME_FIELDS),
        "presets": [
            {"id": name, "label": THEME_PRESETS[name].label}
            for name in PRESET_ORDER
        ],
        "terminal_background_modes": list(TERMINAL_BACKGROUND_MODES),
        "accounts": accounts,
        "themes": themes,
    }


class ThemeUIServer(ThreadingHTTPServer):
    """HTTP server carrying only the explicit theme-editor context."""

    daemon_threads = True

    def __init__(
        self,
        entries: Sequence[Account],
        store: ThemeStore,
        *,
        port: int = 0,
        token: str | None = None,
    ):
        self.entries = tuple(entries)
        self.store = store
        self.token = token or secrets.token_urlsafe(32)
        super().__init__((THEME_UI_HOST, port), ThemeUIRequestHandler)

    @property
    def account_names(self) -> set[str]:
        return {account.name for account in self.entries}


class ThemeUIRequestHandler(BaseHTTPRequestHandler):
    """Serve one embedded page and a narrow JSON mutation API."""

    server: ThemeUIServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self, status: int, document: Mapping[str, Any]
    ) -> None:
        body = json.dumps(document, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Codex-Start-Token", "")
        return secrets.compare_digest(supplied, self.server.token)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/" and not parsed.query:
            page = THEME_UI_HTML.replace(
                TOKEN_PLACEHOLDER, self.server.token
            ).encode("utf-8")
            self._send(200, page, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state" and not parsed.query:
            if not self._authorized():
                self._error(403, "Invalid request token.")
                return
            self._send_json(
                200, theme_ui_state(self.server.store, self.server.entries)
            )
            return
        self._error(404, "Not found.")

    @staticmethod
    def _check_keys(
        payload: Mapping[str, Any],
        required: set[str],
        optional: set[str] | None = None,
    ) -> str | None:
        allowed = required | (optional or set())
        missing = required - payload.keys()
        unexpected = payload.keys() - allowed
        if missing:
            return f"Missing field: {sorted(missing)[0]}."
        if unexpected:
            return f"Unexpected field: {sorted(unexpected)[0]}."
        return None

    def _read_json(self) -> Mapping[str, Any] | None:
        if self.headers.get_content_type() != "application/json":
            self._error(415, "Content-Type must be application/json.")
            return None
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self._error(411, "A valid Content-Length is required.")
            return None
        if length > MAX_REQUEST_BYTES:
            self._error(413, "Request body is too large.")
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "Request body must be valid JSON.")
            return None
        if not isinstance(payload, dict):
            self._error(400, "Request body must be a JSON object.")
            return None
        return payload

    def _valid_account(self, value: Any) -> bool:
        return isinstance(value, str) and (
            value == "default" or value in self.server.account_names
        )

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path != "/api/theme" or parsed.query:
            self._error(404, "Not found.")
            return
        if not self._authorized():
            self._error(403, "Invalid request token.")
            return
        payload = self._read_json()
        if payload is None:
            return
        action = payload.get("action")
        account_name = payload.get("account")
        if not isinstance(action, str):
            self._error(400, "Action must be a string.")
            return
        if not self._valid_account(account_name):
            self._error(400, "Unknown account.")
            return

        try:
            if action == "save":
                error = self._check_keys(
                    payload, {"action", "account", "colors"}
                )
                colors = payload.get("colors")
                if error:
                    self._error(400, error)
                    return
                if not isinstance(colors, dict) or not colors:
                    self._error(400, "Colors must be a non-empty object.")
                    return
                checked: dict[str, str] = {}
                for field, color in colors.items():
                    if field not in THEME_FIELDS:
                        self._error(400, "Unknown theme field.")
                        return
                    if (
                        not isinstance(color, str)
                        or len(color) > 64
                        or not valid_color(color)
                    ):
                        self._error(400, f"Invalid color for {field}.")
                        return
                    checked[field] = color
                self.server.store.set_colors(account_name, checked)
            elif action == "set-preset":
                error = self._check_keys(
                    payload, {"action", "account", "preset"}
                )
                if error:
                    self._error(400, error)
                    return
                preset = payload.get("preset")
                if not isinstance(preset, str):
                    self._error(400, "Preset must be a string.")
                    return
                self.server.store.set_preset(account_name, preset)
            elif action == "set-presentation":
                error = self._check_keys(
                    payload,
                    {"action", "account", "terminal_background_mode"},
                    {"neutral_terminal_background"},
                )
                if error:
                    self._error(400, error)
                    return
                mode = payload.get("terminal_background_mode")
                neutral = payload.get("neutral_terminal_background")
                if mode not in TERMINAL_BACKGROUND_MODES:
                    self._error(400, "Unknown terminal background mode.")
                    return
                if neutral is not None and (
                    not isinstance(neutral, str)
                    or len(neutral) > 64
                    or not valid_color(neutral)
                ):
                    self._error(400, "Invalid neutral terminal background.")
                    return
                self.server.store.set_terminal_background_mode(
                    account_name, mode
                )
                if neutral is not None:
                    self.server.store.set_neutral_terminal_background(
                        account_name, neutral
                    )
            elif action == "reset":
                error = self._check_keys(
                    payload, {"action", "account"}, {"field"}
                )
                if error:
                    self._error(400, error)
                    return
                field = payload.get("field")
                if field is not None and field not in THEME_FIELDS:
                    self._error(400, "Unknown theme field.")
                    return
                self.server.store.reset(account_name, field)
            elif action == "copy":
                error = self._check_keys(
                    payload, {"action", "account", "source"}
                )
                source = payload.get("source")
                if error:
                    self._error(400, error)
                    return
                if account_name == "default":
                    self._error(
                        400, "Global defaults cannot copy an account theme."
                    )
                    return
                if not self._valid_account(source):
                    self._error(400, "Unknown source account.")
                    return
                if source == account_name:
                    self._error(400, "Source and target accounts must differ.")
                    return
                self.server.store.copy_from(
                    account_name, "" if source == "default" else source
                )
            else:
                self._error(400, "Unknown action.")
                return
        except ValueError as error:
            self._error(409, str(error))
            return
        except OSError:
            self._error(500, "Could not update theme preferences.")
            return

        self._send_json(
            200, theme_ui_state(self.server.store, self.server.entries)
        )


def create_theme_ui_server(
    entries: Sequence[Account],
    store: ThemeStore | None = None,
    *,
    port: int = 0,
    token: str | None = None,
) -> ThemeUIServer:
    """Create an IPv4 loopback-only server, using an ephemeral port by default."""
    return ThemeUIServer(entries, store or ThemeStore(), port=port, token=token)


def theme_ui_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-start theme-ui",
        description="Edit codex-start account themes in a local-only browser UI.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="start the server without opening a browser",
    )
    args = parser.parse_args(list(argv))
    entries = load_accounts()
    try:
        server = create_theme_ui_server(entries)
    except OSError as error:
        raise SystemExit(f"Could not start theme UI: {error}") from error
    address = f"http://{THEME_UI_HOST}:{server.server_port}/"
    print(f"Theme UI: {address}", flush=True)
    print("Local only. Press Ctrl+C to stop.", flush=True)
    if not args.no_open:
        import webbrowser

        try:
            if not webbrowser.open(address, new=2):
                print("Could not open a browser automatically; use the URL above.", file=sys.stderr)
        except webbrowser.Error as error:
            print(f"Could not open a browser automatically: {error}", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nTheme UI stopped.", flush=True)
    finally:
        server.server_close()
    return 0


THEME_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="codex-start-token" content="__CODEX_START_THEME_UI_TOKEN__">
  <title>codex-start Theme Lab</title>
  <style>
    :root {
      color-scheme: dark;
      --page: #090b10;
      --panel: #10131a;
      --panel-raised: #151923;
      --border: #272d39;
      --muted: #8a93a3;
      --text: #e7eaf0;
      --accent: #8c7cff;
      --accent-2: #4ec9f5;
      --danger: #ff6b78;
      --success: #4bd68a;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-width: 320px;
      min-height: 100vh;
      color: var(--text);
      background:
        radial-gradient(circle at 75% -10%, #1b2040 0, transparent 34rem),
        radial-gradient(circle at -15% 45%, #10212a 0, transparent 30rem),
        var(--page);
    }
    button, input, select { font: inherit; }
    button, select, input[type="text"] {
      color: var(--text);
      background: #0d1016;
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    button {
      min-height: 34px;
      padding: 0 12px;
      cursor: pointer;
      transition: border-color .15s, background .15s, transform .15s;
    }
    button:hover:not(:disabled) { border-color: #596274; background: #171b24; }
    button:active:not(:disabled) { transform: translateY(1px); }
    button:focus-visible, input:focus-visible, select:focus-visible {
      outline: 2px solid var(--accent-2);
      outline-offset: 2px;
    }
    button:disabled { cursor: not-allowed; opacity: .45; }
    .primary { background: #6657dd; border-color: #8578ef; font-weight: 700; }
    .primary:hover:not(:disabled) { background: #7465e9; border-color: #9a90f6; }
    .danger { color: #ff9ca5; }
    .shell { width: min(1240px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 42px; }
    header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 22px;
    }
    .eyebrow {
      margin: 0 0 5px;
      color: var(--accent-2);
      font: 700 11px/1.3 ui-monospace, SFMono-Regular, Consolas, monospace;
      letter-spacing: .16em;
      text-transform: uppercase;
    }
    h1 { margin: 0; font-size: clamp(25px, 4vw, 36px); letter-spacing: -.035em; }
    .subtitle { margin: 6px 0 0; color: var(--muted); }
    .local-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
      margin-top: 5px;
      padding: 7px 10px;
      border: 1px solid #254d3a;
      border-radius: 999px;
      color: #9ae6bc;
      background: #102219;
      font: 650 12px ui-monospace, SFMono-Regular, Consolas, monospace;
    }
    .local-pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--success); box-shadow: 0 0 10px #4bd68aaa; }
    .toolbar, .panel {
      border: 1px solid var(--border);
      border-radius: 13px;
      background: color-mix(in srgb, var(--panel) 94%, transparent);
      box-shadow: 0 18px 45px #0005;
    }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) repeat(3, minmax(140px, auto)) auto;
      gap: 14px;
      align-items: end;
      padding: 14px;
      margin-bottom: 16px;
    }
    .field-label { display: block; margin: 0 0 6px; color: var(--muted); font-size: 12px; font-weight: 650; }
    select { width: 100%; height: 38px; padding: 0 32px 0 11px; }
    .dirty {
      align-self: center;
      min-width: 128px;
      padding: 8px 11px;
      border: 1px solid #3a404d;
      border-radius: 999px;
      color: var(--muted);
      text-align: center;
      font-size: 12px;
    }
    .dirty.changed { color: #ffd185; border-color: #684f23; background: #281e0e; }
    .dirty.invalid { color: #ffadb5; border-color: #69323a; background: #291319; }
    .preview-panel { overflow: hidden; margin-bottom: 16px; }
    .panel-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 15px;
      border-bottom: 1px solid var(--border);
    }
    .panel-title h2 { margin: 0; font-size: 13px; letter-spacing: .02em; }
    .panel-title span { color: var(--muted); font-size: 12px; }
    .terminal {
      margin: 14px;
      overflow: hidden;
      border: 1px solid #303641;
      border-radius: 10px;
      background: #080a0c;
      box-shadow: inset 0 0 0 1px #ffffff05, 0 16px 35px #0008;
    }
    .status-rail-preview {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      width: 100%;
      min-height: 30px;
      padding: 0 8px;
      border-bottom: 1px solid #555b61;
      font: 400 12px/30px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .rail-row { display: flex; align-items: center; white-space: nowrap; }
    .rail-primary { flex: 1 1 520px; min-width: 0; }
    .rail-usage { flex: 0 1 auto; margin-left: auto; }
    .status-rail-preview .field { display: inline; }
    .status-rail-preview .separator { padding: 0 8px; font-weight: 400; }
    .status-rail-preview .path-prefix,
    .status-rail-preview .reset-meta { font-size: 10px; opacity: .78; }
    .status-rail-preview .path-name { font-weight: 750; }
    .status-action-preview {
      min-height: 22px;
      margin-left: 8px;
      padding: 0 6px;
      border-radius: 5px;
      color: inherit;
      font-size: 10px;
    }
    .status-rail-preview [data-color="directory"],
    .status-rail-preview [data-color="account"],
    .status-rail-preview [data-color="plan"],
    .status-rail-preview [data-color="model"],
    .status-rail-preview [data-color="five_hour"],
    .status-rail-preview [data-color="weekly"] { font-weight: 700; }
    .terminal-scroll { overflow: hidden; }
    .terminal-body {
      height: 142px;
      padding: 18px 16px;
      color: #81868f;
      background: #080a0c;
      font: 12px/1.8 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .terminal-body .prompt { color: #c8cbd1; }
    .terminal-body .slash { color: #8378df; }
    .editor {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 258px;
      gap: 16px;
    }
    .color-list { padding: 7px 14px 12px; }
    .color-row {
      display: grid;
      grid-template-columns: minmax(112px, 1fr) 42px minmax(104px, 126px) 74px 36px 70px;
      gap: 9px;
      align-items: center;
      min-height: 52px;
      border-bottom: 1px solid #202530;
    }
    .color-row:last-child { border-bottom: 0; }
    .color-name { min-width: 0; }
    .color-name strong { display: block; font: 650 13px ui-monospace, SFMono-Regular, Consolas, monospace; }
    .source { display: block; overflow: hidden; color: var(--muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    input[type="color"] {
      width: 38px;
      height: 34px;
      padding: 2px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #0d1016;
      cursor: pointer;
    }
    input[type="color"]::-webkit-color-swatch-wrapper { padding: 2px; }
    input[type="color"]::-webkit-color-swatch { border: 0; border-radius: 4px; }
    input[type="text"] {
      width: 100%;
      height: 34px;
      padding: 0 9px;
      font: 12px ui-monospace, SFMono-Regular, Consolas, monospace;
      text-transform: lowercase;
    }
    input[type="text"].invalid { border-color: var(--danger); box-shadow: 0 0 0 1px #ff6b7840; }
    .ratio { color: var(--muted); font: 11px ui-monospace, SFMono-Regular, Consolas, monospace; text-align: center; }
    .ratio.pass { color: #83dda9; }
    .ratio.fail { color: #ff9ea7; }
    .icon-button { width: 34px; min-height: 32px; padding: 0; color: var(--muted); }
    .reset-field { min-height: 30px; padding: 0 8px; color: #b8bfcb; font-size: 11px; }
    aside { padding: 14px; align-self: start; position: sticky; top: 14px; }
    aside h2 { margin: 1px 0 4px; font-size: 14px; }
    aside p { margin: 0 0 15px; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .stack { display: grid; gap: 9px; }
    .stack select { margin-bottom: 1px; }
    .divider { height: 1px; margin: 7px 0; background: var(--border); }
    .hint { margin-top: 14px; padding: 10px; border-radius: 8px; color: #9199a7; background: #0c0f15; font: 11px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }
    #toast {
      position: fixed;
      right: 20px;
      bottom: 20px;
      max-width: min(390px, calc(100% - 40px));
      padding: 11px 14px;
      border: 1px solid #38404d;
      border-radius: 9px;
      color: #dfe3ea;
      background: #171b23;
      box-shadow: 0 12px 35px #000a;
      opacity: 0;
      transform: translateY(8px);
      pointer-events: none;
      transition: .18s;
      font-size: 12px;
    }
    #toast.show { opacity: 1; transform: translateY(0); }
    @media (max-width: 900px) {
      .editor { grid-template-columns: 1fr; }
      aside { position: static; }
      .toolbar { grid-template-columns: 1fr 1fr; }
      .dirty { grid-column: 1 / -1; }
    }
    @media (max-width: 650px) {
      .shell { width: min(100% - 20px, 1240px); padding-top: 18px; }
      header { display: block; }
      .local-pill { margin-top: 14px; }
      .toolbar { grid-template-columns: 1fr; }
      .dirty { grid-column: auto; }
      .color-list { padding: 7px 10px 12px; }
      .color-row {
        grid-template-columns: minmax(90px, 1fr) 40px minmax(100px, 1.1fr) 34px;
        gap: 7px;
        padding: 7px 0;
      }
      .ratio { grid-column: 2 / 4; text-align: left; }
      .reset-field { grid-column: 4; grid-row: 2; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <p class="eyebrow">codex-start / developer tool</p>
        <h1>Theme Lab</h1>
        <p class="subtitle">Tune the real launcher palette, without touching the runtime.</p>
      </div>
      <div class="local-pill">127.0.0.1 only</div>
    </header>

    <section class="toolbar" aria-label="Preview settings">
      <label>
        <span class="field-label">Theme target</span>
        <select id="account-select" aria-label="Theme target"></select>
      </label>
      <label>
        <span class="field-label">Packaged preset</span>
        <select id="preset-select" aria-label="Packaged preset"></select>
      </label>
      <label>
        <span class="field-label">Terminal background</span>
        <select id="terminal-background-mode" aria-label="Terminal background mode"></select>
      </label>
      <label>
        <span class="field-label">Neutral background</span>
        <input id="neutral-background" type="color" aria-label="Neutral terminal background">
      </label>
      <div id="dirty-state" class="dirty" role="status">All changes saved</div>
    </section>

    <section class="panel preview-panel">
      <div class="panel-title">
        <h2>Live launcher preview</h2>
        <span>actual host rail + native VTE presentation</span>
      </div>
      <div class="terminal">
        <div class="terminal-scroll">
          <div class="status-rail-preview" aria-label="Terminal host status rail">
            <span class="rail-row rail-primary">
              <span class="field"><span data-color="labels">dir: </span><span class="path-prefix" data-color="text">~/…/Desktop/</span><span class="path-name" data-color="directory">project</span></span>
              <span class="separator" data-color="separators">|</span>
              <span class="field"><span data-color="labels">account: </span><span data-color="account" data-account-value>alpha</span><span data-color="text"> • </span><span data-color="plan">Plus</span></span>
              <span class="separator" data-color="separators">|</span>
              <span class="field"><span data-color="labels">model: </span><span data-color="model">gpt-5.6-sol max</span></span>
            </span>
            <span class="rail-row rail-usage">
              <span class="separator" data-color="separators">|</span>
              <span class="field"><span data-color="labels">5h: </span><span data-color="five_hour">82%</span><span class="reset-meta" data-color="text"> 14:14</span></span>
              <span class="separator" data-color="separators">|</span>
              <span class="field"><span data-color="labels">week: </span><span data-color="weekly">64%</span><span class="reset-meta" data-color="text"> Fri 09:03</span></span>
              <button class="status-action-preview" type="button" tabindex="-1">Copy Full Transcript</button>
            </span>
          </div>
        </div>
        <div class="terminal-body">
          <div><span class="slash">›</span> <span class="prompt">Build something precise</span></div>
          <div>&nbsp;&nbsp;native VTE scrollback, selection, paste, and context menu</div>
          <div>&nbsp;&nbsp;Codex native status line: model · directory · 5h · week</div>
        </div>
      </div>
    </section>

    <div class="editor">
      <section class="panel">
        <div class="panel-title">
          <h2>Palette</h2>
          <span>contrast measured against background</span>
        </div>
        <div id="color-list" class="color-list"></div>
      </section>

      <aside class="panel">
        <h2>Theme actions</h2>
        <p>Edits stay in this browser until you save. Reset and copy actions write immediately.</p>
        <div class="stack">
          <button id="save-button" class="primary" type="button" disabled>Save Theme</button>
          <button id="randomize-button" type="button">Randomize / experiment</button>
          <div class="divider"></div>
          <label>
            <span class="field-label">Copy from</span>
            <select id="copy-source"></select>
          </label>
          <button id="copy-button" type="button">Copy theme</button>
          <button id="reset-theme-button" class="danger" type="button">Reset entire account theme</button>
        </div>
        <div class="hint">Persistence: ~/.config/codex-start/themes.json<br>Nothing is saved by preview or randomize.</div>
      </aside>
    </div>
  </main>
  <div id="toast" role="status" aria-live="polite"></div>

  <script>
    "use strict";
    const token = document.querySelector('meta[name="codex-start-token"]').content;
    const fieldLabels = {
      labels: "labels", directory: "directory", account: "account",
      plan: "plan", model: "model", five_hour: "five_hour",
      weekly: "weekly", reset: "reset", separators: "separators",
      text: "text", background: "background"
    };
    let state = null;
    let currentAccount = "";
    let savedColors = {};
    let colors = {};
    let presentation = {};
    let invalidFields = new Set();
    let toastTimer = null;

    const accountSelect = document.getElementById("account-select");
    const presetSelect = document.getElementById("preset-select");
    const terminalBackgroundMode = document.getElementById("terminal-background-mode");
    const neutralBackground = document.getElementById("neutral-background");
    const copySource = document.getElementById("copy-source");
    const colorList = document.getElementById("color-list");
    const dirtyState = document.getElementById("dirty-state");
    const saveButton = document.getElementById("save-button");
    function notify(message) {
      const toast = document.getElementById("toast");
      toast.textContent = message;
      toast.classList.add("show");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toast.classList.remove("show"), 2400);
    }

    async function request(method, body = null) {
      const options = {
        method,
        headers: { "X-Codex-Start-Token": token }
      };
      if (body !== null) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }
      const response = await fetch(method === "GET" ? "/api/state" : "/api/theme", options);
      const payload = await response.json().catch(() => ({ error: "Invalid server response." }));
      if (!response.ok) throw new Error(payload.error || `Request failed (${response.status}).`);
      return payload;
    }

    function isDirty() {
      return Object.keys(colors).some(field => colors[field] !== savedColors[field]);
    }

    function hasPendingChanges() {
      return isDirty() || invalidFields.size > 0;
    }

    function changedColors() {
      return Object.fromEntries(
        Object.keys(colors)
          .filter(field => colors[field] !== savedColors[field])
          .map(field => [field, colors[field]])
      );
    }

    function updateDirtyState() {
      dirtyState.classList.toggle("changed", isDirty() && !invalidFields.size);
      dirtyState.classList.toggle("invalid", invalidFields.size > 0);
      if (invalidFields.size) dirtyState.textContent = "Fix invalid colors";
      else if (isDirty()) dirtyState.textContent = "Unsaved changes";
      else dirtyState.textContent = "All changes saved";
      saveButton.disabled = !isDirty() || invalidFields.size > 0;
    }

    function luminance(hex) {
      const channels = [1, 3, 5].map(index => parseInt(hex.slice(index, index + 2), 16) / 255)
        .map(value => value <= .03928 ? value / 12.92 : Math.pow((value + .055) / 1.055, 2.4));
      return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
    }

    function contrast(foreground, background) {
      const first = luminance(foreground);
      const second = luminance(background);
      return (Math.max(first, second) + .05) / (Math.min(first, second) + .05);
    }

    function updatePreview() {
      document.querySelectorAll("[data-color]").forEach(element => {
        element.style.color = colors[element.dataset.color];
      });
      document.querySelectorAll(".status-rail-preview").forEach(element => {
        element.style.backgroundColor = colors.background;
        element.style.borderBottomColor = colors.separators;
      });
      document.querySelectorAll(".terminal-body").forEach(element => {
        element.style.color = colors.text;
        element.style.backgroundColor = presentation.terminal_background_mode === "themed"
          ? colors.background
          : presentation.terminal_background_mode === "neutral"
            ? presentation.neutral_terminal_background
            : "#080a0c";
      });
      const accountText = currentAccount === "default"
        ? (state.accounts.find(item => item.kind === "account")?.label || "alpha")
        : currentAccount;
      document.querySelectorAll("[data-account-value]").forEach(element => {
        element.textContent = accountText;
      });
      document.querySelectorAll(".color-row").forEach(row => {
        const field = row.dataset.field;
        const ratio = row.querySelector(".ratio");
        if (field === "background") {
          ratio.textContent = "canvas";
          ratio.className = "ratio";
          return;
        }
        const value = contrast(colors[field], colors.background);
        ratio.textContent = `${value.toFixed(1)}:1 ${value >= 4.5 ? "AA" : "low"}`;
        ratio.className = `ratio ${value >= 4.5 ? "pass" : "fail"}`;
      });
      updateDirtyState();
    }

    function setColor(field, value) {
      const normalized = value.toLowerCase();
      const textInput = document.getElementById(`text-${field}`);
      if (!/^#[0-9a-f]{6}$/.test(normalized)) {
        invalidFields.add(field);
        textInput.classList.add("invalid");
        updateDirtyState();
        return;
      }
      invalidFields.delete(field);
      textInput.classList.remove("invalid");
      textInput.value = normalized;
      document.getElementById(`picker-${field}`).value = normalized;
      colors[field] = normalized;
      const inherited = state.themes[currentAccount].inherited[field];
      const source = document.querySelector(`[data-source="${field}"]`);
      source.textContent = normalized === savedColors[field]
        ? (inherited ? (currentAccount === "default" ? "built-in" : "inherited") : "account override")
        : "new unsaved value";
      updatePreview();
    }

    async function copyHex(field) {
      const value = document.getElementById(`text-${field}`).value;
      try {
        await navigator.clipboard.writeText(value);
      } catch (_error) {
        const input = document.getElementById(`text-${field}`);
        input.select();
        document.execCommand("copy");
      }
      notify(`${field}: ${value} copied`);
    }

    function buildColorRows() {
      colorList.replaceChildren();
      const theme = state.themes[currentAccount];
      state.fields.forEach(field => {
        const row = document.createElement("div");
        row.className = "color-row";
        row.dataset.field = field;

        const name = document.createElement("div");
        name.className = "color-name";
        const strong = document.createElement("strong");
        strong.textContent = fieldLabels[field];
        const source = document.createElement("span");
        source.className = "source";
        source.dataset.source = field;
        const raw = theme.source_values[field];
        const converted = raw.toLowerCase() !== savedColors[field] ? ` · source ${raw}` : "";
        source.textContent = (theme.inherited[field]
          ? (currentAccount === "default" ? "built-in" : "inherited")
          : "account override") + converted;
        name.append(strong, source);

        const picker = document.createElement("input");
        picker.type = "color";
        picker.id = `picker-${field}`;
        picker.value = colors[field];
        picker.setAttribute("aria-label", `${field} color picker`);
        picker.addEventListener("input", () => setColor(field, picker.value));

        const text = document.createElement("input");
        text.type = "text";
        text.id = `text-${field}`;
        text.value = colors[field];
        text.maxLength = 7;
        text.spellcheck = false;
        text.setAttribute("aria-label", `${field} hex value`);
        text.addEventListener("input", () => setColor(field, text.value));

        const ratio = document.createElement("span");
        ratio.className = "ratio";

        const copy = document.createElement("button");
        copy.type = "button";
        copy.className = "icon-button";
        copy.title = `Copy ${field} hex value`;
        copy.setAttribute("aria-label", `Copy ${field} hex value`);
        copy.textContent = "⧉";
        copy.addEventListener("click", () => copyHex(field));

        const reset = document.createElement("button");
        reset.type = "button";
        reset.className = "reset-field";
        reset.textContent = currentAccount === "default" ? "Built-in" : "Inherit";
        reset.title = `Reset ${field}`;
        reset.addEventListener("click", () => resetField(field));
        row.append(name, picker, text, ratio, copy, reset);
        colorList.append(row);
      });
      updatePreview();
    }

    function populateSelectors() {
      accountSelect.replaceChildren();
      state.accounts.forEach(account => {
        const option = document.createElement("option");
        option.value = account.id;
        option.textContent = account.label;
        accountSelect.append(option);
      });
      accountSelect.value = currentAccount;

      presetSelect.replaceChildren();
      state.presets.forEach(preset => {
        const option = document.createElement("option");
        option.value = preset.id;
        option.textContent = preset.label;
        presetSelect.append(option);
      });
      presetSelect.value = presentation.preset;

      terminalBackgroundMode.replaceChildren();
      state.terminal_background_modes.forEach(mode => {
        const option = document.createElement("option");
        option.value = mode;
        option.textContent = mode[0].toUpperCase() + mode.slice(1);
        terminalBackgroundMode.append(option);
      });
      terminalBackgroundMode.value = presentation.terminal_background_mode;
      neutralBackground.value = presentation.neutral_terminal_background;
      neutralBackground.disabled = presentation.terminal_background_mode !== "neutral";

      copySource.replaceChildren();
      state.accounts.filter(account => account.id !== currentAccount).forEach(account => {
        const option = document.createElement("option");
        option.value = account.id;
        option.textContent = account.label;
        copySource.append(option);
      });
      const globalTarget = currentAccount === "default";
      copySource.disabled = globalTarget || copySource.options.length === 0;
      document.getElementById("copy-button").disabled = copySource.disabled;
      document.getElementById("reset-theme-button").textContent = globalTarget
        ? "Reset global defaults"
        : "Reset entire account theme";
    }

    function selectAccount(accountName, freshState = state) {
      state = freshState;
      currentAccount = accountName;
      savedColors = { ...state.themes[currentAccount].colors };
      colors = { ...savedColors };
      presentation = { ...state.themes[currentAccount].presentation };
      invalidFields.clear();
      populateSelectors();
      buildColorRows();
    }

    async function resetField(field) {
      const theme = state.themes[currentAccount];
      if (theme.inherited[field]) {
        setColor(field, theme.reset_colors[field]);
        return;
      }
      const pending = changedColors();
      delete pending[field];
      try {
        const fresh = await request("POST", { action: "reset", account: currentAccount, field });
        selectAccount(currentAccount, fresh);
        Object.entries(pending).forEach(([name, value]) => setColor(name, value));
        notify(`${field} restored to ${currentAccount === "default" ? "built-in" : "inherited"} color`);
      } catch (error) {
        notify(error.message);
      }
    }

    function hslToHex(hue, saturation, lightness) {
      saturation /= 100;
      lightness /= 100;
      const channel = offset => {
        const k = (offset + hue / 30) % 12;
        const a = saturation * Math.min(lightness, 1 - lightness);
        return lightness - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
      };
      return "#" + [0, 8, 4].map(offset => Math.round(255 * channel(offset)).toString(16).padStart(2, "0")).join("");
    }

    document.getElementById("randomize-button").addEventListener("click", () => {
      const baseHue = Math.floor(Math.random() * 360);
      state.fields.forEach((field, index) => {
        const value = field === "background"
          ? hslToHex((baseHue + 25) % 360, 28, 5 + Math.random() * 5)
          : hslToHex((baseHue + index * 43) % 360, 62 + Math.random() * 24, 62 + Math.random() * 18);
        setColor(field, value);
      });
      notify("Experiment generated — nothing saved");
    });

    saveButton.addEventListener("click", async () => {
      try {
        const fresh = await request("POST", {
          action: "save", account: currentAccount, colors: changedColors()
        });
        selectAccount(currentAccount, fresh);
        notify("Theme saved");
      } catch (error) {
        notify(error.message);
      }
    });

    accountSelect.addEventListener("change", () => {
      const next = accountSelect.value;
      if (hasPendingChanges() && !confirm("Discard unsaved color changes?")) {
        accountSelect.value = currentAccount;
        return;
      }
      selectAccount(next);
    });

    presetSelect.addEventListener("change", async () => {
      if (hasPendingChanges() && !confirm("Discard unsaved color changes?")) {
        presetSelect.value = presentation.preset;
        return;
      }
      try {
        const fresh = await request("POST", {
          action: "set-preset", account: currentAccount, preset: presetSelect.value
        });
        selectAccount(currentAccount, fresh);
        notify(`Preset changed to ${presetSelect.selectedOptions[0].textContent}`);
      } catch (error) {
        presetSelect.value = presentation.preset;
        notify(error.message);
      }
    });

    async function savePresentation() {
      try {
        const fresh = await request("POST", {
          action: "set-presentation",
          account: currentAccount,
          terminal_background_mode: terminalBackgroundMode.value,
          neutral_terminal_background: neutralBackground.value
        });
        selectAccount(currentAccount, fresh);
        notify("Terminal background saved");
      } catch (error) {
        selectAccount(currentAccount);
        notify(error.message);
      }
    }
    terminalBackgroundMode.addEventListener("change", savePresentation);
    neutralBackground.addEventListener("change", savePresentation);

    document.getElementById("copy-button").addEventListener("click", async () => {
      const source = copySource.value;
      if (!source) return;
      const label = copySource.selectedOptions[0].textContent;
      if (!confirm(`Replace this theme with the effective colors from ${label}?`)) return;
      try {
        const fresh = await request("POST", {
          action: "copy", account: currentAccount, source
        });
        selectAccount(currentAccount, fresh);
        notify(`Theme copied from ${label}`);
      } catch (error) {
        notify(error.message);
      }
    });

    document.getElementById("reset-theme-button").addEventListener("click", async () => {
      const description = currentAccount === "default" ? "built-in defaults" : "global defaults";
      if (!confirm(`Reset every color to ${description}?`)) return;
      try {
        const fresh = await request("POST", { action: "reset", account: currentAccount });
        selectAccount(currentAccount, fresh);
        notify(`Theme restored to ${description}`);
      } catch (error) {
        notify(error.message);
      }
    });

    window.addEventListener("beforeunload", event => {
      if (hasPendingChanges()) {
        event.preventDefault();
        event.returnValue = "";
      }
    });

    request("GET")
      .then(payload => {
        state = payload;
        const firstAccount = state.accounts.find(account => account.kind === "account")
          || state.accounts[0];
        selectAccount(firstAccount.id);
      })
      .catch(error => {
        dirtyState.textContent = "Server unavailable";
        dirtyState.classList.add("invalid");
        notify(error.message);
      });
  </script>
</body>
</html>
"""
