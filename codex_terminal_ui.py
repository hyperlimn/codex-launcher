"""Optional GTK4/VTE terminal host for codex-start.

Importing this module does not import PyGObject.  GTK and VTE are loaded only
when a standalone host is actually requested, so the core CLI stays light and
works on systems without the optional desktop dependencies.
"""

from __future__ import annotations

import html
import os
import signal
from types import SimpleNamespace
from typing import Any

import codex_start
from codex_terminal_theme import (
    RailGroup,
    RailSegment,
    StatusModel,
    StatusRail,
    ThemeModel,
    TranscriptExporter,
    TranscriptSession,
    responsive_rail_layout,
)


SCROLLBACK_LINES = 10_000
STATUS_POLL_MILLISECONDS = 1_000
WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 720


class TerminalUIUnavailable(RuntimeError):
    """The optional GTK/VTE stack cannot be initialized."""


def _load_gtk() -> SimpleNamespace:
    try:
        import gi

        gi.require_version("Gdk", "4.0")
        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        gi.require_version("Gtk", "4.0")
        gi.require_version("Pango", "1.0")
        gi.require_version("Vte", "3.91")
        from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte
    except (ImportError, ValueError) as error:
        raise TerminalUIUnavailable(str(error)) from error
    return SimpleNamespace(
        Gdk=Gdk,
        Gio=Gio,
        GLib=GLib,
        Gtk=Gtk,
        Pango=Pango,
        Vte=Vte,
    )


def _rgba(Gdk: Any, color: str) -> Any:
    value = Gdk.RGBA()
    if not value.parse(color):
        value.parse("#080a0c")
    return value


def _markup(
    segments: tuple[RailSegment, ...], theme: ThemeModel
) -> str:
    result: list[str] = []
    colors = theme.as_dict()
    for segment in segments:
        weight = "bold" if segment.bold else "normal"
        size = ' size="small"' if segment.small else ""
        result.append(
            f'<span foreground="{colors[segment.theme_field]}" '
            f'weight="{weight}"{size}>{html.escape(codex_start.terminal_safe_text(segment.text))}</span>'
        )
    return "".join(result)


def _gtk_component_classes(modules: SimpleNamespace) -> tuple[type, type]:
    Gdk = modules.Gdk
    Gio = modules.Gio
    GLib = modules.GLib
    Gtk = modules.Gtk
    Pango = modules.Pango
    Vte = modules.Vte

    class StatusRailWidget(Gtk.Box):
        """Reusable measured one/two-row chrome; it never owns a terminal."""

        def __init__(self, status: StatusModel, theme: ThemeModel):
            super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            self.add_css_class("codex-status-rail")
            self.set_hexpand(True)
            self.presentation = StatusRail(status, theme)
            self._group_labels: dict[str, list[Any]] = {}
            self._action_boxes: list[Any] = []
            self._action_buttons: list[Any] = []
            self._wide_natural_width = 0

            self._wide = self._build_layout(
                (("directory", "identity", "model", "five_hour", "weekly"),),
                actions_row=0,
            )
            self._narrow = self._build_layout(
                (
                    ("directory", "identity", "model"),
                    ("five_hour", "weekly"),
                ),
                actions_row=1,
            )
            self._narrow.set_visible(False)
            self.append(self._wide)
            self.append(self._narrow)

            self._css_provider = Gtk.CssProvider()
            self.get_style_context().add_provider(
                self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            self.presentation.set_change_handler(self._render)

        def do_size_allocate(
            self, width: int, height: int, baseline: int
        ) -> None:
            self._select_layout(width)
            Gtk.Box.do_size_allocate(self, width, height, baseline)

        def _build_layout(
            self,
            rows: tuple[tuple[str, ...], ...],
            *,
            actions_row: int,
        ) -> Any:
            container = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=0
            )
            for row_index, names in enumerate(rows):
                row = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL, spacing=0
                )
                row.add_css_class("codex-status-row")
                row.set_hexpand(True)
                for index, name in enumerate(names):
                    if index:
                        separator = Gtk.Label(label="|")
                        separator.add_css_class("codex-status-separator")
                        row.append(separator)
                    label = Gtk.Label()
                    label.add_css_class("codex-status-text")
                    label.set_xalign(0.0)
                    label.set_single_line_mode(True)
                    label.set_ellipsize(Pango.EllipsizeMode.END)
                    if name == "directory":
                        label.set_hexpand(True)
                    self._group_labels.setdefault(name, []).append(label)
                    row.append(label)
                if row_index == actions_row:
                    actions = Gtk.Box(
                        orientation=Gtk.Orientation.HORIZONTAL, spacing=2
                    )
                    actions.add_css_class("codex-status-actions")
                    row.append(actions)
                    self._action_boxes.append(actions)
                container.append(row)
            return container

        def _select_layout(self, width: int) -> None:
            if width <= 0 or self._wide_natural_width <= 0:
                return
            layout = responsive_rail_layout(width, self._wide_natural_width)
            narrow = layout.is_two_row
            self._wide.set_visible(not narrow)
            self._narrow.set_visible(narrow)

        def update(
            self,
            *,
            status: StatusModel | None = None,
            theme: ThemeModel | None = None,
        ) -> None:
            self.presentation.update(status=status, theme=theme)

        def add_action(
            self,
            label: str,
            callback: Any,
            *,
            tooltip: str | None = None,
        ) -> Any:
            """Add the same tiny action to both measured layout variants."""

            buttons = []
            for actions in self._action_boxes:
                button = Gtk.Button(label=label)
                button.add_css_class("flat")
                button.add_css_class("codex-status-action")
                if tooltip:
                    button.set_tooltip_text(tooltip)
                button.connect("clicked", callback)
                actions.append(button)
                buttons.append(button)
                self._action_buttons.append(button)
            self._measure_wide_layout()
            return buttons[0]

        def set_actions_sensitive(self, sensitive: bool) -> None:
            for button in self._action_buttons:
                button.set_sensitive(sensitive)

        def _measure_wide_layout(self) -> None:
            _minimum, natural, _minimum_baseline, _natural_baseline = (
                self._wide.measure(Gtk.Orientation.HORIZONTAL, -1)
            )
            self._wide_natural_width = natural
            self._select_layout(self.get_width())

        def _render(
            self,
            status: StatusModel,
            theme: ThemeModel,
            groups: tuple[RailGroup, ...],
        ) -> None:
            by_name = {group.name: group for group in groups}
            for name, labels in self._group_labels.items():
                group = by_name[name]
                for label in labels:
                    label.set_markup(_markup(group.segments, theme))
                    if name == "directory":
                        label.set_tooltip_text(status.directory.full)
            css = f"""
                .codex-status-rail {{
                    background: {theme.background};
                    border-bottom: 1px solid {theme.separators};
                }}
                .codex-status-row {{
                    min-height: 28px;
                    padding: 0 7px;
                }}
                .codex-status-text {{
                    padding: 5px 5px 4px 5px;
                    font-family: monospace;
                    font-size: 11pt;
                }}
                .codex-status-separator {{
                    padding: 0 3px;
                    color: {theme.separators};
                    font-family: monospace;
                    font-size: 10pt;
                }}
                .codex-status-actions {{
                    padding: 2px 0 2px 5px;
                }}
                .codex-status-action {{
                    min-height: 22px;
                    min-width: 22px;
                    padding: 1px 5px;
                    color: {theme.text};
                    font-family: monospace;
                    font-size: 9pt;
                }}
            """
            self._css_provider.load_from_data(css.encode("utf-8"))
            self._measure_wide_layout()

    class StandaloneTerminalHost(Gtk.ApplicationWindow):
        """One independent GTK window containing a rail and one real VTE."""

        def __init__(
            self,
            application: Any,
            launch_spec: codex_start.TerminalHostLaunch,
            snapshot: codex_start.StatusSnapshot,
            theme_store: codex_start.ThemeStore,
        ) -> None:
            super().__init__(application=application)
            self.launch_spec = launch_spec
            self.theme_store = theme_store
            self.exit_code: int | None = None
            self.child_pid: int | None = None
            self._closing = False
            self._poll_source: int | None = None
            self._rate_reader: codex_start.AppServerRateLimitReader | None = None
            self._tracker = codex_start.RolloutTracker(
                launch_spec.account, launch_spec.cwd
            )
            self._tracker.snapshot = snapshot
            self._theme = theme_store.theme_model_for(
                launch_spec.account.name
            )

            self.set_title(self._window_title(snapshot))
            self.set_default_size(WINDOW_WIDTH, WINDOW_HEIGHT)

            content = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=0
            )
            self.status_rail = StatusRailWidget(
                codex_start.terminal_status_model(snapshot), self._theme
            )
            content.append(self.status_rail)

            self.terminal = Vte.Terminal()
            self.terminal.set_hexpand(True)
            self.terminal.set_vexpand(True)
            self.terminal.set_scrollback_lines(SCROLLBACK_LINES)
            self.terminal.set_scroll_on_output(False)
            self.terminal.set_scroll_on_keystroke(True)
            self.terminal.set_mouse_autohide(True)
            self.terminal.set_allow_hyperlink(True)
            self.terminal.set_font(Pango.FontDescription.from_string("Monospace 11"))
            self._inherited_terminal_background = (
                self.terminal.get_color_background_for_draw()
            )
            self._apply_terminal_theme(self._theme)
            self._transcript_exporter = TranscriptExporter()
            self._copy_transcript_button = self.status_rail.add_action(
                "Copy Full Transcript",
                self._copy_full_transcript,
                tooltip="Ask this Codex session to copy its full transcript",
            )
            self.status_rail.set_actions_sensitive(False)
            content.append(self.terminal)
            self.set_child(content)

            self._install_shortcuts()
            self._install_context_menu()
            self.terminal.connect("child-exited", self._child_exited)
            self.connect("close-request", self._close_requested)

            self._cancellable = Gio.Cancellable()
            self._spawn()

        def _window_title(
            self, snapshot: codex_start.StatusSnapshot
        ) -> str:
            status = codex_start.terminal_status_model(snapshot)
            return (
                f"Codex · {status.account} • {status.plan} · {status.model}"
            )

        def _apply_terminal_theme(self, theme: ThemeModel) -> None:
            foreground = _rgba(Gdk, theme.text)
            background_color = theme.terminal_background_color()
            background = (
                self._inherited_terminal_background
                if background_color is None
                else _rgba(Gdk, background_color)
            )
            self.terminal.set_colors(foreground, background, None)
            self.terminal.set_color_cursor(_rgba(Gdk, theme.labels))

        def _copy_full_transcript(self, button: Any) -> None:
            """Invoke Codex's own export UI through this session's input only."""

            result = self._transcript_exporter.copy_current(
                TranscriptSession(
                    identifier=self.child_pid,
                    write_input=self.terminal.feed_child,
                    active=self.child_pid is not None and not self._closing,
                )
            )
            button.set_tooltip_text(result.message)

        def _spawn(self) -> None:
            environment = [
                f"{key}={value}"
                for key, value in self.launch_spec.environment.items()
                if "\x00" not in key and "\x00" not in value
            ]
            self.terminal.spawn_async(
                Vte.PtyFlags.DEFAULT,
                str(self.launch_spec.cwd),
                list(self.launch_spec.argv),
                environment,
                GLib.SpawnFlags.DEFAULT,
                None,
                None,
                -1,
                self._cancellable,
                self._spawned,
                self,
            )

        def _spawned(
            self,
            _terminal: Any,
            pid: int,
            error: Exception | None,
            _user_data: Any,
        ) -> None:
            if error is not None or pid < 0:
                message = str(error) if error is not None else "unknown error"
                self.terminal.feed(
                    f"codex-start: could not start terminal child: {message}\r\n".encode()
                )
                self.exit_code = 1
                return
            self.child_pid = int(pid)
            self.status_rail.set_actions_sensitive(True)
            self._rate_reader = codex_start.AppServerRateLimitReader(
                self.launch_spec.codex_path,
                self.launch_spec.environment,
            )
            self._poll_source = GLib.timeout_add(
                STATUS_POLL_MILLISECONDS, self._poll_status
            )
            self.terminal.grab_focus()

        def _poll_status(self) -> bool:
            if self._closing or self.child_pid is None:
                return False
            snapshot = self._tracker.refresh(self.child_pid)
            if self._rate_reader is not None:
                for observation in self._rate_reader.poll():
                    snapshot = self._tracker.apply_rate_limits(
                        observation.limits,
                        observation.observed_at,
                        sparse=observation.sparse,
                    )
            next_theme = self.theme_store.theme_model_for(
                self.launch_spec.account.name
            )
            theme_changed = next_theme != self._theme
            self._theme = next_theme
            self.status_rail.update(
                status=codex_start.terminal_status_model(snapshot),
                theme=next_theme,
            )
            if theme_changed:
                self._apply_terminal_theme(next_theme)
            self.set_title(self._window_title(snapshot))
            return True

        def _install_shortcuts(self) -> None:
            controller = Gtk.EventControllerKey()
            controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

            def key_pressed(
                _controller: Any,
                keyval: int,
                _keycode: int,
                state: Any,
            ) -> bool:
                required = (
                    Gdk.ModifierType.CONTROL_MASK
                    | Gdk.ModifierType.SHIFT_MASK
                )
                if state & required != required:
                    return False
                lowered = Gdk.keyval_to_lower(keyval)
                if lowered == Gdk.KEY_v:
                    self.terminal.paste_clipboard()
                    return True
                if lowered == Gdk.KEY_c and self.terminal.get_has_selection():
                    self.terminal.copy_clipboard_format(Vte.Format.TEXT)
                    return True
                return False

            controller.connect("key-pressed", key_pressed)
            self.terminal.add_controller(controller)
            self._key_controller = controller

        def _install_context_menu(self) -> None:
            self._context_popover = Gtk.Popover()
            self._context_popover.set_autohide(True)
            menu = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=2
            )
            self._copy_button = Gtk.Button(label="Copy")
            self._paste_button = Gtk.Button(label="Paste")
            for button in (self._copy_button, self._paste_button):
                button.add_css_class("flat")
                menu.append(button)
            self._copy_button.connect("clicked", self._copy_selection)
            self._paste_button.connect("clicked", self._paste_clipboard)
            self._context_popover.set_child(menu)
            self._context_popover.set_parent(self.terminal)

            gesture = Gtk.GestureClick()
            gesture.set_button(3)
            gesture.connect("pressed", self._show_context_menu)
            self.terminal.add_controller(gesture)
            self._context_gesture = gesture

        def _show_context_menu(
            self,
            _gesture: Any,
            _presses: int,
            x: float,
            y: float,
        ) -> None:
            self._copy_button.set_sensitive(
                self.terminal.get_has_selection()
            )
            self._context_popover.set_pointing_to(
                Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1)
            )
            self._context_popover.popup()

        def _copy_selection(self, _button: Any) -> None:
            self.terminal.copy_clipboard_format(Vte.Format.TEXT)
            self._context_popover.popdown()

        def _paste_clipboard(self, _button: Any) -> None:
            self.terminal.paste_clipboard()
            self._context_popover.popdown()

        def _child_exited(self, _terminal: Any, status: int) -> None:
            try:
                self.exit_code = os.waitstatus_to_exitcode(status)
            except ValueError:
                self.exit_code = status if status >= 0 else 1
            self.child_pid = None
            self.status_rail.set_actions_sensitive(False)
            self._cleanup()
            self.get_application().quit()

        def _close_requested(self, _window: Any) -> bool:
            self._closing = True
            if self.child_pid is not None:
                try:
                    os.kill(self.child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                self._cancellable.cancel()
            self._cleanup()
            return False

        def _cleanup(self) -> None:
            if self._poll_source is not None:
                GLib.source_remove(self._poll_source)
                self._poll_source = None
            if self._rate_reader is not None:
                self._rate_reader.close()
                self._rate_reader = None

    return StatusRailWidget, StandaloneTerminalHost


def create_status_rail_widget(
    status: StatusModel,
    theme: ThemeModel,
) -> Any:
    """Create only the reusable GTK rail for an existing GTK/VTE pane."""

    modules = _load_gtk()
    rail_class, _host_class = _gtk_component_classes(modules)
    return rail_class(status, theme)


def launch_terminal_host(
    launch_spec: codex_start.TerminalHostLaunch,
    snapshot: codex_start.StatusSnapshot,
    theme_store: codex_start.ThemeStore,
) -> int | None:
    """Run one themed VTE host, or return ``None`` for plain fallback."""

    try:
        modules = _load_gtk()
    except TerminalUIUnavailable:
        return None
    if not modules.Gtk.init_check():
        return None

    _rail_class, host_class = _gtk_component_classes(modules)
    application = modules.Gtk.Application(
        application_id="io.codexstart.Launcher",
        flags=modules.Gio.ApplicationFlags.NON_UNIQUE,
    )
    state: dict[str, Any] = {}

    def activate(app: Any) -> None:
        window = host_class(app, launch_spec, snapshot, theme_store)
        state["window"] = window
        window.present()

    application.connect("activate", activate)
    application_result = application.run([])
    window = state.get("window")
    if window is not None and window.exit_code is not None:
        return int(window.exit_code)
    return int(application_result)
