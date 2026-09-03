# codex-start

codex-start is a small terminal environment for choosing a local Codex account and launching the real interactive Codex CLI with `--approve-for-me`. Its core remains standard-library-only; the default Linux presentation optionally adds native GTK4/VTE chrome.

## Requirements

- Python 3.11 or newer (the launcher uses `tomllib` from the standard library).
- A Linux or compatible POSIX terminal with PTY and ANSI/VT support.
- GTK4, PyGObject, and VTE 3.91 for the default themed standalone host (optional; absence falls back to the current terminal).
- A Codex CLI build that provides `--approve-for-me`, the `tui.status_line` and `tui.alternate_screen` settings, and the app-server rate-limit API. The current release is tested with Codex CLI 0.152.1.

## Run or install

Run directly from the checkout:

~~~console
./codex-start
~~~

The wrapper resolves symbolic links, so it can also be placed on your path:

~~~console
mkdir -p ~/.local/bin
ln -s /absolute/path/to/codex-start ~/.local/bin/codex-start
codex-start --version
~~~

The selector supports Up/Down or j/k, number keys, Enter to launch, `t` to cycle the highlighted account's theme, `a` to add an account, and `q` to quit. You can skip it:

~~~console
./codex-start alpha
./codex-start beta
./codex-start account-two
~~~

Launcher options go before the account. Arguments after `--` are passed to Codex:

~~~console
./codex-start --plain alpha
./codex-start alpha -- --search
~~~

## Presentation architecture

On Linux, an interactive launch uses the familiar selector and then opens one themed GTK4 window when GTK/VTE is available. A compact, persistent identity/usage rail sits immediately above a real `Vte.Terminal`. Codex receives the VTE's full dimensions and runs in native inline mode with `tui.alternate_screen=never`.

The VTE widget is the only terminal emulator and the sole owner of:

- scrollback and wheel navigation;
- text selection and the right-click context menu;
- Ctrl+Shift+V and bracketed paste;
- keyboard focus and ordinary terminal input;
- independent behavior for every terminal host or future embedded pane.

Inside the VTE, a transparent PTY bridge filters only alternate-screen requests, terminal mouse-reporting modes, alternate-scroll mode, and child terminal-title changes. Drawing, erase/redraw, scroll-region, keyboard-query, focus, bracketed-paste, and OSC-52 clipboard controls otherwise pass through unchanged. It does not decode or repaint terminal content and owns no scrollback or transcript.

At startup, the bridge disables stale mouse modes and clears the new VTE's visible screen and saved scrollback exactly once before Codex starts, so no prior shell history can appear. Cleanup restores raw terminal state, interaction modes, signal handlers, and cursor visibility without erasing Codex's native transcript. Closing the host signals the bridge, which forwards termination to Codex and cleans up its process group.

The host rail uses subdued labels and separators, a tiny muted path prefix plus dominant final directory, strong identity/model values, and colored usage percentages. Reset date/time text is smaller and neutral, for example `5h: 72%  14:14 | week: 48%  Fri 09:03`. The rail stays on one row when its measured natural width fits and switches to two compact semantic rows when it does not. Codex's native footer remains visible inside VTE.

`codex_terminal_theme.py` contains the GTK-free `ThemeModel`, `StatusModel`, compact `DirectoryPresentation`, semantic rail groups, responsive layout contract, write-only `TranscriptExporter`, and toolkit-neutral `StatusRail` update holder. `codex_terminal_ui.create_status_rail_widget()` wraps those models as a reusable GTK widget without creating a window or terminal. The host keeps this rail outside VTE.

The VTE background is independent from the rail palette. Per account, it can inherit the terminal's original background, use a neutral background, or use the themed rail background.

`--plain` deliberately skips the themed host and uses the current terminal's native PTY path. Missing GTK/VTE, a missing graphical display, non-interactive output, and `TERM=dumb` also degrade to the existing plain/direct paths. `--version`, `--status`, `accounts`, `theme`, and `theme-ui` do not initialize GTK.

There is no wheel interception, X11 integration, multiplexer, fake screen model, privileged service, namespace change, or security relaxation. The themed host starts a private Codex app-server child over stdio only for structured rate-limit updates; it does not open a listener or make a model request.

### Full transcript export

Codex remains the transcript owner. The rail's small **Copy Full Transcript** action sends `/export`, Down, Enter to its own active Codex input stream, selecting the copy option in Codex CLI 0.152.1's deterministic export menu. The exporter has a write-only session handle: it cannot read the VTE screen, VTE scrollback, rollout transcript, or rendered output. The ordinary `/export` menu remains available for both **Save** and **Copy**.

## Status data

The launcher reads:

- model and initial reasoning effort from the selected `CODEX_HOME/config.toml`;
- an initial fallback from structured `token_count.rate_limits` rollout events;
- authoritative live rate-limit windows from the same `account/rateLimits/read` API that Codex uses for startup and `/status`, refreshed automatically once per minute;
- rolling `account/rateLimits/updated` and rollout notifications between full reads;
- live model, reasoning, and directory changes from structured `turn_context` and `thread_settings_applied` events;
- the child process working directory as a normal-permission fallback.

The private client completes the documented `initialize` / `initialized` handshake, bounds stalled requests, and restarts failed readers. Full reads and rolling notifications are merged by observation time. A delayed response or stale rollout snapshot cannot replace a newer session value, and an unrelated metered bucket is never shown as Codex usage. Each accepted snapshot updates both windows and their reset timestamps together. See the [official Codex app-server protocol](https://developers.openai.com/codex/app-server).

The launcher never parses the rendered `/status` screen and never makes a model request to refresh usage. The app-server child uses the selected account's existing user-space authentication and works without sudo, a daemon, a socket listener, or weakened Linux security. Missing or expired usage is displayed as `--%  --` until a fresh trustworthy snapshot arrives.

Inspect the newest cached structured status without launching Codex:

~~~console
./codex-start --status alpha
~~~

## Accounts

Account definitions are local user data stored at:

~~~text
~/.config/codex-start/accounts.json
~~~

The public package contains no built-in identities or `CODEX_HOME` mappings. With no local file, the selector shows:

~~~text
No Codex accounts configured
+ Add account
~~~

Press `a` to enter a display name, `CODEX_HOME` path, and optional generic theme preset. Existing local configuration is read as-is and is never replaced by public defaults.

List definitions, print the configuration path without creating it, or initialize an empty file explicitly:

~~~console
./codex-start accounts
./codex-start accounts --path
./codex-start accounts --init
~~~

Add another account:

~~~console
./codex-start accounts --add account-name ~/.codex-account-name
./codex-start accounts --add account-two ~/.codex-account-two --theme Cobalt
~~~

Added paths are stored as stable absolute or home-relative paths. The JSON file can also be edited directly; relative paths in that file are resolved from the file's directory.

## Themes

The packaged presets are **Default**, **Crimson**, **Cobalt**, **Forest**, and **Graphite**. Any configured account can use any preset; there is no built-in account-to-preset mapping. Select a preset or terminal background mode from the CLI:

~~~console
./codex-start theme alpha preset Crimson
./codex-start theme alpha terminal-background neutral '#0b0d10'
./codex-start theme alpha terminal-background themed
./codex-start theme alpha terminal-background inherit
~~~

Each account can independently override labels, directory, account, plan, model, `five_hour`, weekly, reset, separators, ordinary text, and rail background without copying the preset palette into local JSON. Show the effective composed theme:

~~~console
./codex-start theme alpha show
~~~

Set a field using a named color, `#RRGGBB`, RGB triplet, or 0-255 terminal palette index:

~~~console
./codex-start theme account-two set account purple
./codex-start theme account-two set model '#32c7ff'
./codex-start theme account-two set five_hour 57
./codex-start theme account-two set weekly 74,163,255
~~~

Reset one field, reset an account, or copy another account's effective theme:

~~~console
./codex-start theme account-two reset weekly
./codex-start theme account-two reset
./codex-start theme beta copy-from account-two
~~~

The shared inherited default is edited the same way:

~~~console
./codex-start theme default set plan green
./codex-start theme default reset plan
./codex-start theme default reset
~~~

Themes are stored at `~/.config/codex-start/themes.json`. Version-1 color-only documents remain valid; version-2 adds preset and terminal-background choices while preserving per-field overrides. Truecolor, 256-color, 16-color, `TERM=dumb`, and `NO_COLOR` fallbacks are supported. Malformed preference files are ignored for display but never overwritten by a mutation command.

For a live visual editor, launch the developer theme UI:

~~~console
./codex-start theme-ui
./codex-start theme-ui --no-open
~~~

It opens the default browser automatically unless `--no-open` is supplied. The standard-library server binds only to `127.0.0.1` on an ephemeral port and exists only for the lifetime of this command; Ctrl+C shuts it down. The editor includes preset and terminal-background controls, custom overrides, and a wrapping preview of the real compact one/two-row rail hierarchy. It calls the same `ThemeStore` validation, inheritance, reset, and copy operations as the CLI. It serves one embedded page plus a token-protected JSON theme API, never a filesystem directory.

## Persistence

The launcher keeps separate files:

- account definitions: `~/.config/codex-start/accounts.json`
- theme preferences: `~/.config/codex-start/themes.json`
- last-selected runtime state: `~/.cache/codex-start/runtime.json`

`XDG_CONFIG_HOME` and `XDG_CACHE_HOME` are honored. The launcher reads Codex settings and rollout data; Codex and its private app-server child retain their normal access to the selected `CODEX_HOME`.

## Testing

Run the standard-library test suite and compilation checks:

~~~console
python3 -m unittest -v
python3 -m compileall -q codex_start.py codex_terminal_theme.py codex_terminal_ui.py codex_terminal_bridge.py codex_theme_ui.py test_codex_start.py test_codex_terminal_ui.py test_codex_theme_ui.py codex-start
~~~

The suite uses temporary Codex homes and fake Codex executables. It validates empty first-run behavior, local account addition and preservation, generic preset composition and cycling, semantic responsive rails, write-only correct-session transcript export, independent terminal backgrounds, the app-server handshake, newer-vs-stale rate-limit ordering, optional-host fallback, non-recursive bridging, native PTY streaming for fresh and resumed output, full-size resize propagation, raw keyboard/signal forwarding, one-time removal of pre-Codex shell history, bracketed-paste and clipboard pass-through, alternate-screen/mouse/title filtering, terminal-native right-click and selection, independent session state, clean exit, and restoration that preserves Codex scrollback. It does not make an OpenAI model request.
