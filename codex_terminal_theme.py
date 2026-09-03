"""GTK-free presentation models for codex-start terminal chrome.

This module is the reusable boundary between launcher status/theme data and a
terminal host. It deliberately has no knowledge of PTYs, VTE, or GTK.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol


THEME_FIELDS = (
    "labels",
    "directory",
    "account",
    "plan",
    "model",
    "five_hour",
    "weekly",
    "reset",
    "separators",
    "text",
    "background",
)

TERMINAL_BACKGROUND_MODES = ("inherit", "neutral", "themed")
DEFAULT_TERMINAL_BACKGROUND_MODE = "neutral"
DEFAULT_NEUTRAL_TERMINAL_BACKGROUND = "#0b0d10"

# Kept byte-for-byte compatible with the original themes.json color base.
DEFAULT_THEME: dict[str, str] = {
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


@dataclass(frozen=True)
class ThemePreset:
    """One small packaged palette plus its host-presentation intent."""

    name: str
    label: str
    colors: Mapping[str, str]
    terminal_background_mode: str = DEFAULT_TERMINAL_BACKGROUND_MODE
    neutral_terminal_background: str = DEFAULT_NEUTRAL_TERMINAL_BACKGROUND
    density: str = "compact"
    border_intensity: str = "standard"


THEME_PRESETS: dict[str, ThemePreset] = {
    "default": ThemePreset("default", "Default", DEFAULT_THEME),
    "crimson": ThemePreset(
        "crimson",
        "Crimson",
        {
            "labels": "#d8c7cf",
            "directory": "#ef91bc",
            "account": "#ff7185",
            "plan": "#c98bff",
            "model": "#e7a2d7",
            "five_hour": "#70d69a",
            "weekly": "#a88aff",
            "reset": "#c7a8b3",
            "separators": "#603743",
            "text": "#af969f",
            "background": "#240a12",
        },
    ),
    "cobalt": ThemePreset(
        "cobalt",
        "Cobalt",
        {
            "labels": "#b8c8d9",
            "directory": "#82b9ff",
            "account": "#68d6ff",
            "plan": "#a896ff",
            "model": "#62c7e8",
            "five_hour": "#65d9ad",
            "weekly": "#559cff",
            "reset": "#9caaba",
            "separators": "#29445d",
            "text": "#91a5b8",
            "background": "#071421",
        },
    ),
    "forest": ThemePreset(
        "forest",
        "Forest",
        {
            "labels": "#c6d4c9",
            "directory": "#82cdb9",
            "account": "#a4d37a",
            "plan": "#66d991",
            "model": "#b9d9c0",
            "five_hour": "#61d69a",
            "weekly": "#7baedc",
            "reset": "#b7aa7d",
            "separators": "#304b3a",
            "text": "#92a89a",
            "background": "#07150f",
        },
    ),
    "graphite": ThemePreset(
        "graphite",
        "Graphite",
        {
            "labels": "#9ca2a8",
            "directory": "#65c6d4",
            "account": "#e6e4de",
            "plan": "#b8bcc1",
            "model": "#f0eee8",
            "five_hour": "#d0ad68",
            "weekly": "#78aab7",
            "reset": "#898f94",
            "separators": "#353a3e",
            "text": "#b5b8b8",
            "background": "#111315",
        },
        terminal_background_mode="inherit",
        density="graphite",
        border_intensity="subtle",
    ),
}

PRESET_ORDER = tuple(THEME_PRESETS)
PRESET_ALIASES = {
    "red": "crimson",
    "blue": "cobalt",
    "green": "forest",
}


def normalize_preset_name(value: str) -> str:
    """Return a canonical packaged preset name or raise ``ValueError``."""

    normalized = value.strip().casefold().replace(" ", "-")
    normalized = PRESET_ALIASES.get(normalized, normalized)
    if normalized not in THEME_PRESETS:
        raise ValueError(f"unknown theme preset: {value}")
    return normalized


@dataclass(frozen=True)
class ThemeModel:
    """A complete palette and the few presentation choices a host needs."""

    labels: str
    directory: str
    account: str
    plan: str
    model: str
    five_hour: str
    weekly: str
    reset: str
    separators: str
    text: str
    background: str
    preset: str = "default"
    terminal_background_mode: str = DEFAULT_TERMINAL_BACKGROUND_MODE
    neutral_terminal_background: str = DEFAULT_NEUTRAL_TERMINAL_BACKGROUND
    density: str = "compact"
    border_intensity: str = "standard"

    @classmethod
    def from_mapping(
        cls,
        colors: Mapping[str, str],
        *,
        preset: str = "default",
        terminal_background_mode: str | None = None,
        neutral_terminal_background: str | None = None,
        density: str | None = None,
        border_intensity: str | None = None,
    ) -> "ThemeModel":
        preset_name = normalize_preset_name(preset)
        packaged = THEME_PRESETS[preset_name]
        mode = terminal_background_mode or packaged.terminal_background_mode
        if mode not in TERMINAL_BACKGROUND_MODES:
            mode = packaged.terminal_background_mode
        return cls(
            **{
                field: colors.get(field, packaged.colors[field])
                for field in THEME_FIELDS
            },
            preset=preset_name,
            terminal_background_mode=mode,
            neutral_terminal_background=(
                neutral_terminal_background
                or packaged.neutral_terminal_background
            ),
            density=density or packaged.density,
            border_intensity=border_intensity or packaged.border_intensity,
        )

    @classmethod
    def default(cls) -> "ThemeModel":
        return cls.from_mapping(DEFAULT_THEME)

    @classmethod
    def from_preset(cls, preset: str) -> "ThemeModel":
        packaged = THEME_PRESETS[normalize_preset_name(preset)]
        return cls.from_mapping(
            packaged.colors,
            preset=packaged.name,
            terminal_background_mode=packaged.terminal_background_mode,
            neutral_terminal_background=packaged.neutral_terminal_background,
            density=packaged.density,
            border_intensity=packaged.border_intensity,
        )

    def as_dict(self) -> dict[str, str]:
        """Return the legacy color-only mapping used by themes.json clients."""

        return {field: getattr(self, field) for field in THEME_FIELDS}

    def presentation_dict(self) -> dict[str, str]:
        return {
            "preset": self.preset,
            "terminal_background_mode": self.terminal_background_mode,
            "neutral_terminal_background": self.neutral_terminal_background,
            "density": self.density,
            "border_intensity": self.border_intensity,
        }

    def terminal_background_color(self) -> str | None:
        if self.terminal_background_mode == "inherit":
            return None
        if self.terminal_background_mode == "themed":
            return self.background
        return self.neutral_terminal_background


@dataclass(frozen=True)
class DirectoryPresentation:
    """A path split semantically into quiet context and dominant leaf."""

    prefix: str
    name: str
    full: str

    @classmethod
    def from_path(
        cls, path: Path, home: Path | None = None
    ) -> "DirectoryPresentation":
        user_home = (Path.home() if home is None else home).expanduser()
        try:
            resolved = path.expanduser().resolve(strict=False)
            resolved_home = user_home.resolve(strict=False)
        except (OSError, RuntimeError):
            resolved = path.expanduser()
            resolved_home = user_home

        try:
            relative = resolved.relative_to(resolved_home)
        except ValueError:
            relative = None

        if relative is not None:
            parts = relative.parts
            if not parts:
                return cls("", "~", "~")
            name = parts[-1]
            parents = parts[:-1]
            if not parents:
                prefix = "~/"
            elif len(parents) == 1:
                prefix = f"~/{parents[0]}/"
            else:
                prefix = f"~/\u2026/{parents[-1]}/"
            return cls(prefix, name, "~/" + "/".join(parts))

        parts = resolved.parts
        anchor = resolved.anchor
        if not resolved.name:
            return cls("", anchor or str(resolved), str(resolved))
        parent_parts = parts[1:-1] if anchor else parts[:-1]
        if not parent_parts:
            prefix = anchor
        elif len(parent_parts) == 1:
            prefix = f"{anchor}{parent_parts[0]}/"
        else:
            prefix = f"{anchor}\u2026/{parent_parts[-1]}/"
        return cls(prefix, resolved.name, str(resolved))

    def __str__(self) -> str:
        return self.full


@dataclass(frozen=True)
class StatusModel:
    """Display-ready identity and rate-limit data for one Codex session."""

    directory: DirectoryPresentation
    account: str
    plan: str
    model: str
    five_hour: str
    five_hour_reset: str
    weekly: str
    weekly_reset: str

    def compact_limits(self) -> str:
        return (
            f"5h {self.five_hour} {self.five_hour_reset} \u00b7 "
            f"week {self.weekly} {self.weekly_reset}"
        )


@dataclass(frozen=True)
class RailSegment:
    """One semantic run in the status rail."""

    text: str
    theme_field: str
    bold: bool = False
    small: bool = False
    semantic: str = "value"


@dataclass(frozen=True)
class RailGroup:
    """An indivisible group that a responsive view may place on a row."""

    name: str
    segments: tuple[RailSegment, ...]


@dataclass(frozen=True)
class RailLayout:
    """Toolkit-neutral grouping contract for wide and narrow hosts."""

    rows: tuple[tuple[str, ...], ...]

    @property
    def is_two_row(self) -> bool:
        return len(self.rows) == 2


WIDE_RAIL_LAYOUT = RailLayout(
    (("directory", "identity", "model", "five_hour", "weekly", "actions"),)
)
NARROW_RAIL_LAYOUT = RailLayout(
    (
        ("directory", "identity", "model"),
        ("five_hour", "weekly", "actions"),
    )
)


def responsive_rail_layout(
    available_width: int, one_row_natural_width: int
) -> RailLayout:
    """Choose rows from real widget measurement rather than a pixel breakpoint."""

    return (
        WIDE_RAIL_LAYOUT
        if available_width >= one_row_natural_width
        else NARROW_RAIL_LAYOUT
    )


def status_rail_groups(status: StatusModel) -> tuple[RailGroup, ...]:
    """Return stable semantic groups for GTK FlowBox or another host."""

    directory = status.directory
    return (
        RailGroup(
            "directory",
            (
                RailSegment("dir: ", "labels", semantic="label"),
                RailSegment(
                    directory.prefix,
                    "text",
                    small=True,
                    semantic="path_prefix",
                ),
                RailSegment(
                    directory.name,
                    "directory",
                    bold=True,
                    semantic="path_name",
                ),
            ),
        ),
        RailGroup(
            "identity",
            (
                RailSegment("account: ", "labels", semantic="label"),
                RailSegment(status.account, "account", True),
                RailSegment(" \u2022 ", "text"),
                RailSegment(status.plan, "plan", True),
            ),
        ),
        RailGroup(
            "model",
            (
                RailSegment("model: ", "labels", semantic="label"),
                RailSegment(status.model, "model", True),
            ),
        ),
        RailGroup(
            "five_hour",
            (
                RailSegment("5h: ", "labels", semantic="label"),
                RailSegment(status.five_hour, "five_hour", True, semantic="usage"),
                RailSegment("  ", "text"),
                RailSegment(
                    status.five_hour_reset,
                    "text",
                    small=True,
                    semantic="reset",
                ),
            ),
        ),
        RailGroup(
            "weekly",
            (
                RailSegment("week: ", "labels", semantic="label"),
                RailSegment(status.weekly, "weekly", True, semantic="usage"),
                RailSegment("  ", "text"),
                RailSegment(
                    status.weekly_reset,
                    "text",
                    small=True,
                    semantic="reset",
                ),
            ),
        ),
    )


def status_rail_segments(status: StatusModel) -> tuple[RailSegment, ...]:
    """Flatten semantic groups for legacy ANSI status rendering."""

    groups = status_rail_groups(status)
    separator = RailSegment(" | ", "separators", semantic="separator")
    result: list[RailSegment] = [RailSegment(" ", "text")]
    for index, group in enumerate(groups):
        if index:
            result.append(separator)
        result.extend(group.segments)
    return tuple(result)


class StatusRail:
    """Toolkit-neutral state holder used by GTK and future embedded panes."""

    def __init__(
        self,
        status: StatusModel,
        theme: ThemeModel,
        on_change: Callable[
            [StatusModel, ThemeModel, tuple[RailGroup, ...]], None
        ]
        | None = None,
    ) -> None:
        self.status = status
        self.theme = theme
        self._on_change = on_change

    @property
    def groups(self) -> tuple[RailGroup, ...]:
        return status_rail_groups(self.status)

    @property
    def segments(self) -> tuple[RailSegment, ...]:
        return status_rail_segments(self.status)

    def update(
        self,
        *,
        status: StatusModel | None = None,
        theme: ThemeModel | None = None,
    ) -> None:
        if status is not None:
            self.status = status
        if theme is not None:
            self.theme = theme
        if self._on_change is not None:
            self._on_change(self.status, self.theme, self.groups)

    def set_change_handler(
        self,
        handler: Callable[
            [StatusModel, ThemeModel, tuple[RailGroup, ...]], None
        ]
        | None,
        *,
        notify: bool = True,
    ) -> None:
        self._on_change = handler
        if notify and handler is not None:
            handler(self.status, self.theme, self.groups)


@dataclass(frozen=True)
class TranscriptActionResult:
    succeeded: bool
    message: str


@dataclass(frozen=True)
class TranscriptSession:
    """Write-only handle for exactly one active Codex input stream."""

    identifier: object
    write_input: Callable[[bytes], None]
    active: bool = True


class TranscriptAction(Protocol):
    def copy_current(self, session: TranscriptSession) -> TranscriptActionResult:
        """Ask Codex itself to copy the current session transcript."""


class TranscriptExporter:
    """Invoke Codex 0.152's supported ``/export`` menu via session input.

    The menu's first item saves Markdown and its second copies Markdown. This
    action deliberately has only a write-only session handle: it cannot read a
    terminal screen, VTE scrollback, or rollout transcript.
    """

    COPY_INPUT = b"/export\r\x1b[B\r"

    def copy_current(self, session: TranscriptSession) -> TranscriptActionResult:
        if not session.active:
            return TranscriptActionResult(False, "Codex session is not active.")
        try:
            session.write_input(self.COPY_INPUT)
        except (BrokenPipeError, OSError, RuntimeError) as error:
            return TranscriptActionResult(
                False, f"Could not request transcript export: {error}"
            )
        return TranscriptActionResult(
            True, "Codex export requested for this session."
        )
