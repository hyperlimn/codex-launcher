"""Standalone, dependency-free terminal launcher for multiple Codex homes."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tomllib
import tty
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator, Mapping, Sequence

from codex_terminal_theme import (
    DEFAULT_NEUTRAL_TERMINAL_BACKGROUND,
    DEFAULT_THEME,
    PRESET_ORDER,
    TERMINAL_BACKGROUND_MODES,
    THEME_FIELDS,
    THEME_PRESETS,
    DirectoryPresentation,
    StatusModel,
    ThemeModel,
    normalize_preset_name,
    status_rail_segments,
)

VERSION = "2.2.5"
POLL_SECONDS = 1.5
RATE_LIMIT_POLL_SECONDS = 60.0
RATE_LIMIT_RETRY_SECONDS = 15.0
RATE_LIMIT_REQUEST_TIMEOUT_SECONDS = 10.0
NATIVE_STATUS_ITEMS = (
    '["model-with-reasoning","current-dir",'
    '"five-hour-limit","weekly-limit"]'
)
BASE_FOREGROUND = "#d8dadd"
MUTED_FOREGROUND = "#81868f"
SEPARATOR_FOREGROUND = "#41464f"
DARK_BACKGROUND = "#080a0c"

HOSTED_ENVIRONMENT = "CODEX_START_HOSTED"
HOST_STATUS_OWNER_ENVIRONMENT = "CODEX_START_STATUS_OWNER"


@dataclass(frozen=True)
class Account:
    name: str
    home: Path


@dataclass(frozen=True)
class ModelSettings:
    model: str
    effort: str


@dataclass(frozen=True)
class StatusSnapshot:
    account: Account
    plan: str
    settings: ModelSettings
    cwd: Path
    limits: dict[str, Any] | None
    updated_at: float | None = None


@dataclass(frozen=True)
class RateLimitObservation:
    limits: dict[str, Any]
    observed_at: float
    sparse: bool = False


@dataclass(frozen=True)
class TerminalHostLaunch:
    """Complete, GTK-independent launch contract for a terminal host."""

    account: Account
    codex_path: str
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]


def config_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("XDG_CONFIG_HOME")
    if configured:
        root = Path(configured).expanduser()
        if root.is_absolute():
            return root / "codex-start"
    return Path.home() / ".config" / "codex-start"


def cache_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("XDG_CACHE_HOME")
    if configured:
        root = Path(configured).expanduser()
        if root.is_absolute():
            return root / "codex-start"
    return Path.home() / ".cache" / "codex-start"


def default_accounts(home: Path | None = None) -> tuple[Account, ...]:
    """Return no identities: account configuration is always local user data."""
    return ()


def _account_document(entries: Sequence[Account]) -> dict[str, Any]:
    return {
        "version": 1,
        "accounts": [
            {"name": account.name, "codex_home": compact_path(account.home)}
            for account in entries
        ],
    }


def _atomic_json_write(path: Path, document: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def ensure_accounts_config(root: Path | None = None) -> Path:
    """Create an explicitly requested empty local account document."""
    root = config_dir() if root is None else root
    path = root / "accounts.json"
    if not path.exists():
        _atomic_json_write(path, _account_document(()))
    return path


def load_accounts(
    root: Path | None = None,
    *,
    create: bool = False,
    home: Path | None = None,
) -> tuple[Account, ...]:
    root = config_dir() if root is None else root
    path = root / "accounts.json"
    if not path.exists():
        return ()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("the document must be an object")
        raw_accounts = document.get("accounts")
        if not isinstance(raw_accounts, list):
            raise ValueError("'accounts' must be a list")
        parsed: list[Account] = []
        names: set[str] = set()
        for item in raw_accounts:
            if not isinstance(item, dict):
                raise ValueError("each account must be an object")
            name = item.get("name")
            raw_home = item.get("codex_home")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("each account needs a non-empty name")
            if name in names:
                raise ValueError(f"duplicate account name: {name}")
            if not isinstance(raw_home, str) or not raw_home.strip():
                raise ValueError(f"account {name} needs codex_home")
            names.add(name)
            account_home = Path(raw_home).expanduser()
            if not account_home.is_absolute():
                account_home = (path.parent / account_home).resolve(strict=False)
            parsed.append(Account(name, account_home))
        return tuple(parsed)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise SystemExit(f"Invalid {path}: {error}") from error


def save_accounts(entries: Sequence[Account], root: Path | None = None) -> Path:
    root = config_dir() if root is None else root
    path = root / "accounts.json"
    _atomic_json_write(path, _account_document(entries))
    return path


def add_account(
    name: str,
    codex_home: str | Path,
    *,
    preset: str = "default",
    entries: Sequence[Account] | None = None,
    root: Path | None = None,
    theme_store: "ThemeStore | None" = None,
) -> Account:
    """Append one account to local user configuration without built-in data."""

    account_name = name.strip()
    if not account_name:
        raise ValueError("account name cannot be empty")
    if account_name == "default":
        raise ValueError("account name 'default' is reserved")
    if any(ord(character) < 32 for character in account_name):
        raise ValueError("account name cannot contain control characters")
    choices = list(load_accounts(root) if entries is None else entries)
    if resolve_account(account_name, choices) is not None:
        raise ValueError(f"account already exists: {account_name}")
    try:
        account_home = Path(codex_home).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"invalid Codex home: {error}") from error
    preset_name = normalize_preset_name(preset)
    account = Account(account_name, account_home)
    save_accounts((*choices, account), root)
    if preset_name != "default":
        (theme_store or ThemeStore(root)).set_preset(account_name, preset_name)
    return account


def prompt_add_account(
    entries: Sequence[Account],
    *,
    input_func: Callable[[str], str] | None = None,
    root: Path | None = None,
    theme_store: "ThemeStore | None" = None,
) -> Account:
    """Collect one local account definition using a small keyboard-first form."""

    ask = input if input_func is None else input_func
    print("Add Codex account")
    try:
        name = ask("Display name: ").strip()
        codex_home = ask("CODEX_HOME path: ").strip()
        labels = ", ".join(
            THEME_PRESETS[preset].label for preset in PRESET_ORDER
        )
        raw_preset = ask(
            f"Theme preset [{labels}] (Default): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        raise SystemExit(130)
    try:
        account = add_account(
            name,
            codex_home,
            preset=raw_preset or "default",
            entries=entries,
            root=root,
            theme_store=theme_store,
        )
    except (OSError, ValueError) as error:
        raise SystemExit(f"Could not add account: {error}") from error
    print(f"Added {account.name} to {config_dir() / 'accounts.json'}")
    return account


def resolve_account(
    value: str, entries: Sequence[Account] | None = None
) -> Account | None:
    choices = load_accounts() if entries is None else entries
    for account in choices:
        if value == account.name:
            return account
    if value.isdecimal():
        number = int(value)
        if 1 <= number <= len(choices):
            return choices[number - 1]
    return None


def compact_path(path: Path, home: Path | None = None) -> str:
    user_home = Path.home() if home is None else home
    try:
        resolved = path.expanduser().resolve(strict=False)
        resolved_home = user_home.resolve(strict=False)
    except (OSError, RuntimeError):
        return str(path.expanduser())
    try:
        relative = resolved.relative_to(resolved_home)
    except ValueError:
        return str(resolved)
    return "~" if not relative.parts else f"~/{relative}"


def load_runtime(root: Path | None = None) -> dict[str, Any]:
    path = (cache_dir() if root is None else root) / "runtime.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_runtime(account: Account, root: Path | None = None) -> None:
    path = (cache_dir() if root is None else root) / "runtime.json"
    try:
        _atomic_json_write(
            path,
            {"version": 1, "last_account": account.name, "selected_at": time.time()},
        )
    except OSError:
        pass


class ThemeStore:
    """Persistent preset choices plus global and per-account overrides.

    Existing version-1 color-only themes.json documents remain valid. New
    presentation keys are additive and never duplicate an entire preset.
    """

    PRESENTATION_FIELDS = {
        "terminal_background_mode",
        "neutral_terminal_background",
    }

    def __init__(self, root: Path | None = None):
        self.root = config_dir() if root is None else root
        self.path = self.root / "themes.json"

    def _load(self, *, strict: bool = False) -> dict[str, Any]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            if strict:
                raise ValueError(f"invalid {self.path}: {error}") from error
            return {}
        if isinstance(document, dict):
            return document
        if strict:
            raise ValueError(f"invalid {self.path}: the document must be an object")
        return {}

    @staticmethod
    def _mark_version(document: dict[str, Any]) -> None:
        """Upgrade documents only when a user explicitly changes a theme."""

        version = document.get("version")
        document["version"] = max(version if isinstance(version, int) else 1, 2)

    @staticmethod
    def _target_from_document(
        document: Mapping[str, Any], account_name: str
    ) -> Mapping[str, Any]:
        if account_name in ("", "default"):
            target = document.get("default", {})
        else:
            raw_accounts = document.get("accounts", {})
            target = (
                raw_accounts.get(account_name, {})
                if isinstance(raw_accounts, dict)
                else {}
            )
        return target if isinstance(target, dict) else {}

    @classmethod
    def _preset_from_document(
        cls, document: Mapping[str, Any], account_name: str
    ) -> str:
        targets = [cls._target_from_document(document, account_name)]
        if account_name not in ("", "default"):
            targets.append(cls._target_from_document(document, "default"))
        for target in targets:
            raw = target.get("preset")
            if isinstance(raw, str):
                try:
                    return normalize_preset_name(raw)
                except ValueError:
                    continue
        return "default"

    @classmethod
    def _theme_from_document(
        cls, document: Mapping[str, Any], account_name: str
    ) -> dict[str, str]:
        preset_name = cls._preset_from_document(document, account_name)
        theme = dict(THEME_PRESETS[preset_name].colors)
        targets = [cls._target_from_document(document, "default")]
        if account_name not in ("", "default"):
            targets.append(cls._target_from_document(document, account_name))
        for overrides in targets:
            for field, color in overrides.items():
                if (
                    field in THEME_FIELDS
                    and isinstance(color, str)
                    and valid_color(color)
                ):
                    theme[field] = normalize_color(color)
        return theme

    @classmethod
    def _presentation_from_document(
        cls, document: Mapping[str, Any], account_name: str
    ) -> dict[str, str]:
        preset_name = cls._preset_from_document(document, account_name)
        preset = THEME_PRESETS[preset_name]
        presentation = {
            "preset": preset_name,
            "terminal_background_mode": preset.terminal_background_mode,
            "neutral_terminal_background": preset.neutral_terminal_background,
            "density": preset.density,
            "border_intensity": preset.border_intensity,
        }
        targets = [cls._target_from_document(document, "default")]
        if account_name not in ("", "default"):
            targets.append(cls._target_from_document(document, account_name))
        for target in targets:
            mode = target.get("terminal_background_mode")
            if mode in TERMINAL_BACKGROUND_MODES:
                presentation["terminal_background_mode"] = mode
            neutral = target.get("neutral_terminal_background")
            if isinstance(neutral, str) and valid_color(neutral):
                presentation["neutral_terminal_background"] = normalize_color(neutral)
        return presentation

    def _target(
        self,
        document: dict[str, Any],
        account_name: str,
        *,
        create: bool,
    ) -> dict[str, Any] | None:
        if account_name == "default":
            key = "default"
            container = document
        else:
            raw_accounts = document.get("accounts")
            if raw_accounts is None:
                if not create:
                    return None
                raw_accounts = {}
                document["accounts"] = raw_accounts
            if not isinstance(raw_accounts, dict):
                raise ValueError(f"invalid {self.path}: 'accounts' must be an object")
            key = account_name
            container = raw_accounts
        target = container.get(key)
        if target is None:
            if not create:
                return None
            target = {}
            container[key] = target
        if not isinstance(target, dict):
            raise ValueError(
                f"invalid {self.path}: theme '{account_name}' must be an object"
            )
        return target

    def theme_for(self, account_name: str) -> dict[str, str]:
        return self._theme_from_document(self._load(), account_name)

    def preset_for(self, account_name: str) -> str:
        return self._preset_from_document(self._load(), account_name)

    def theme_model_for(self, account_name: str) -> ThemeModel:
        document = self._load()
        presentation = self._presentation_from_document(document, account_name)
        return ThemeModel.from_mapping(
            self._theme_from_document(document, account_name),
            preset=presentation["preset"],
            terminal_background_mode=presentation["terminal_background_mode"],
            neutral_terminal_background=presentation[
                "neutral_terminal_background"
            ],
            density=presentation["density"],
            border_intensity=presentation["border_intensity"],
        )

    def set_color(self, account_name: str, field: str, color: str) -> None:
        self.set_colors(account_name, {field: color})

    def set_colors(
        self, account_name: str, colors: Mapping[str, str]
    ) -> None:
        """Validate and persist a group of color overrides atomically."""
        normalized: dict[str, str] = {}
        for field, color in colors.items():
            if field not in THEME_FIELDS:
                raise ValueError(f"unknown theme field: {field}")
            if not isinstance(color, str) or not valid_color(color):
                raise ValueError(f"invalid color: {color}")
            normalized[field] = normalize_color(color)
        document = self._load(strict=True)
        self._mark_version(document)
        target = self._target(document, account_name, create=True)
        assert target is not None
        target.update(normalized)
        _atomic_json_write(self.path, document)

    def set_preset(self, account_name: str, preset: str) -> None:
        preset_name = normalize_preset_name(preset)
        document = self._load(strict=True)
        self._mark_version(document)
        target = self._target(document, account_name, create=True)
        assert target is not None
        target["preset"] = preset_name
        _atomic_json_write(self.path, document)

    def cycle_preset(self, account_name: str, step: int = 1) -> str:
        current = self.preset_for(account_name)
        index = PRESET_ORDER.index(current)
        selected = PRESET_ORDER[(index + step) % len(PRESET_ORDER)]
        self.set_preset(account_name, selected)
        return selected

    def set_terminal_background_mode(
        self, account_name: str, mode: str
    ) -> None:
        if mode not in TERMINAL_BACKGROUND_MODES:
            raise ValueError(f"unknown terminal background mode: {mode}")
        document = self._load(strict=True)
        self._mark_version(document)
        target = self._target(document, account_name, create=True)
        assert target is not None
        target["terminal_background_mode"] = mode
        _atomic_json_write(self.path, document)

    def set_neutral_terminal_background(
        self, account_name: str, color: str
    ) -> None:
        if not isinstance(color, str) or not valid_color(color):
            raise ValueError(f"invalid color: {color}")
        document = self._load(strict=True)
        self._mark_version(document)
        target = self._target(document, account_name, create=True)
        assert target is not None
        target["neutral_terminal_background"] = normalize_color(color)
        _atomic_json_write(self.path, document)

    def theme_details(self, account_name: str) -> dict[str, Any]:
        """Return effective values and per-field inheritance information."""
        document = self._load()
        target = self._target_from_document(document, account_name)
        effective = self._theme_from_document(document, account_name)

        reset_document = dict(document)
        if account_name in ("", "default"):
            reset_document["default"] = {
                key: value
                for key, value in target.items()
                if key not in THEME_FIELDS
            }
        else:
            raw_accounts = document.get("accounts", {})
            reset_accounts = dict(raw_accounts) if isinstance(raw_accounts, dict) else {}
            reset_accounts[account_name] = {
                key: value
                for key, value in target.items()
                if key not in THEME_FIELDS
            }
            reset_document["accounts"] = reset_accounts
        reset_colors = self._theme_from_document(reset_document, account_name)
        explicit = {
            field
            for field, color in target.items()
            if (
                field in THEME_FIELDS
                and isinstance(color, str)
                and valid_color(color)
            )
        }
        presentation = self._presentation_from_document(document, account_name)
        return {
            "colors": effective,
            "reset_colors": reset_colors,
            "inherited": {
                field: field not in explicit for field in THEME_FIELDS
            },
            "preset": presentation["preset"],
            "presentation": presentation,
        }

    def reset(self, account_name: str, field: str | None = None) -> None:
        if field is not None and field not in THEME_FIELDS:
            raise ValueError(f"unknown theme field: {field}")
        document = self._load(strict=True)
        if account_name == "default":
            if field is None:
                document.pop("default", None)
            else:
                target = self._target(document, account_name, create=False)
                if target is None:
                    _atomic_json_write(self.path, document)
                    return
                target.pop(field, None)
                if not target:
                    document.pop("default", None)
        else:
            account_themes = document.get("accounts")
            if account_themes is not None and not isinstance(account_themes, dict):
                raise ValueError(f"invalid {self.path}: 'accounts' must be an object")
            if isinstance(account_themes, dict):
                if field is None:
                    account_themes.pop(account_name, None)
                else:
                    target = self._target(document, account_name, create=False)
                    if target is None:
                        _atomic_json_write(self.path, document)
                        return
                    target.pop(field, None)
                    if not target:
                        account_themes.pop(account_name, None)
                if not account_themes:
                    document.pop("accounts", None)
        _atomic_json_write(self.path, document)

    def copy_from(self, account_name: str, source_name: str) -> None:
        if account_name == "default":
            raise ValueError("the global default theme cannot copy an account theme")
        document = self._load(strict=True)
        self._mark_version(document)
        effective = self._theme_from_document(document, source_name)
        presentation = self._presentation_from_document(document, source_name)
        target = self._target(document, account_name, create=True)
        assert target is not None
        target.clear()
        target["preset"] = presentation["preset"]

        baseline = self._theme_from_document(document, account_name)
        baseline_presentation = self._presentation_from_document(
            document, account_name
        )
        for field in THEME_FIELDS:
            if effective[field] != baseline[field]:
                target[field] = effective[field]
        for field in self.PRESENTATION_FIELDS:
            if presentation[field] != baseline_presentation[field]:
                target[field] = presentation[field]
        _atomic_json_write(self.path, document)


COLOR_NAMES: dict[str, str] = {
    "black": "#000000",
    "red": "#cd3131",
    "green": "#0dbc79",
    "yellow": "#e5e510",
    "blue": "#2472c8",
    "magenta": "#bc3fbc",
    "purple": "#b26cff",
    "cyan": "#11a8cd",
    "white": "#e5e5e5",
    "gray": "#808080",
    "grey": "#808080",
    "orange": "#ffb000",
    "bright_black": "#666666",
    "bright_red": "#f14c4c",
    "bright_green": "#23d18b",
    "bright_yellow": "#f5f543",
    "bright_blue": "#3b8eea",
    "bright_magenta": "#d670d6",
    "bright_cyan": "#29b8db",
    "bright_white": "#ffffff",
}


THEME_FIELD_ALIASES = {
    "dir": "directory",
    "directory": "directory",
    "five-hour": "five_hour",
    "five_hour": "five_hour",
    "5h": "five_hour",
    "week": "weekly",
    "weekly": "weekly",
    "separator": "separators",
    "separators": "separators",
    "ordinary": "text",
    "ordinary_text": "text",
    "text": "text",
}


def normalize_theme_field(value: str) -> str:
    lowered = value.strip().lower().replace(" ", "_")
    return THEME_FIELD_ALIASES.get(lowered, lowered)


def _rgb_literal(value: str) -> str | None:
    lowered = value.strip().lower()
    if lowered.startswith("rgb(") and lowered.endswith(")"):
        lowered = lowered[4:-1]
    if lowered.count(",") != 2:
        return None
    try:
        channels = tuple(int(part.strip()) for part in lowered.split(","))
    except ValueError:
        return None
    if len(channels) != 3 or any(channel < 0 or channel > 255 for channel in channels):
        return None
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def valid_color(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in COLOR_NAMES or lowered == "default":
        return True
    if _rgb_literal(lowered) is not None:
        return True
    if lowered.startswith("#") and len(lowered) == 7:
        try:
            int(lowered[1:], 16)
            return True
        except ValueError:
            return False
    if lowered.isascii() and lowered.isdecimal():
        try:
            return int(lowered) <= 255
        except ValueError:
            # Python limits extremely long integer literals; they are not
            # useful terminal palette indexes in any case.
            return False
    return False


def normalize_color(value: str) -> str:
    lowered = value.strip().lower()
    return COLOR_NAMES.get(lowered, _rgb_literal(lowered) or lowered)


def detect_color_mode(
    environ: Mapping[str, str] | None = None, *, is_tty: bool = True
) -> str:
    env = os.environ if environ is None else environ
    if not is_tty or "NO_COLOR" in env or env.get("TERM", "") == "dumb":
        return "none"
    color_term = env.get("COLORTERM", "").lower()
    if "truecolor" in color_term or "24bit" in color_term:
        return "truecolor"
    if "256color" in env.get("TERM", "").lower():
        return "256"
    return "16"


ANSI16_RGB = (
    (0, 0, 0),
    (205, 49, 49),
    (13, 188, 121),
    (229, 229, 16),
    (36, 114, 200),
    (188, 63, 188),
    (17, 168, 205),
    (229, 229, 229),
    (102, 102, 102),
    (241, 76, 76),
    (35, 209, 139),
    (245, 245, 67),
    (59, 142, 234),
    (214, 112, 214),
    (41, 184, 219),
    (255, 255, 255),
)


def _rgb(value: str) -> tuple[int, int, int] | None:
    normalized = normalize_color(value)
    if (
        normalized == "default"
        or len(normalized) != 7
        or not normalized.startswith("#")
    ):
        return None
    try:
        red, green, blue = (
            int(normalized[index : index + 2], 16) for index in (1, 3, 5)
        )
    except ValueError:
        return None
    return red, green, blue


def _nearest_ansi16(red: int, green: int, blue: int) -> int:
    return min(
        range(16),
        key=lambda index: sum(
            (actual - expected) ** 2
            for actual, expected in zip(ANSI16_RGB[index], (red, green, blue))
        ),
    )


def _nearest_ansi256(red: int, green: int, blue: int) -> int:
    if red == green == blue:
        gray = max(0, min(23, round((red - 8) / 10)))
        return 232 + gray
    cube = tuple(
        max(0, min(5, round(channel / 255 * 5)))
        for channel in (red, green, blue)
    )
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]


def _ansi256_rgb(index: int) -> tuple[int, int, int]:
    if index < 16:
        return ANSI16_RGB[index]
    if index < 232:
        index -= 16
        red, remainder = divmod(index, 36)
        green, blue = divmod(remainder, 6)
        levels = (0, 95, 135, 175, 215, 255)
        return levels[red], levels[green], levels[blue]
    gray = 8 + (index - 232) * 10
    return gray, gray, gray


def color_to_hex(value: str, fallback: str) -> str:
    """Resolve any supported terminal color to a browser-safe RGB value."""
    normalized = normalize_color(value)
    if normalized == "default":
        normalized = normalize_color(fallback)
    if normalized.isdecimal() and int(normalized) <= 255:
        rgb = _ansi256_rgb(int(normalized))
    else:
        rgb = _rgb(normalized) or _rgb(fallback) or (0, 0, 0)
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _ansi16_sequence(index: int, *, background: bool) -> str:
    base = (40 if background else 30) + (index % 8)
    if index >= 8:
        base += 60
    return f"\x1b[{base}m"


def color_sequence(value: str, mode: str, *, background: bool = False) -> str:
    if mode == "none" or normalize_color(value) == "default":
        return ""
    normalized = normalize_color(value)
    if normalized.isdigit():
        palette_index = int(normalized)
        if mode in ("truecolor", "256"):
            return f"\x1b[{48 if background else 38};5;{palette_index}m"
        return _ansi16_sequence(
            _nearest_ansi16(*_ansi256_rgb(palette_index)),
            background=background,
        )
    rgb = _rgb(normalized)
    if rgb is None:
        return ""
    if mode == "truecolor":
        return f"\x1b[{48 if background else 38};2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if mode == "256":
        return f"\x1b[{48 if background else 38};5;{_nearest_ansi256(*rgb)}m"
    return _ansi16_sequence(_nearest_ansi16(*rgb), background=background)


def load_model_settings(codex_home: Path) -> ModelSettings:
    try:
        with (codex_home / "config.toml").open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return ModelSettings("--", "")
    model = str(config.get("model") or "--")
    effort = str(config.get("model_reasoning_effort") or "")
    return ModelSettings(model, effort)


def reverse_lines(path: Path, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Yield a file's non-empty lines from newest to oldest."""
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        position = source.tell()
        remainder = b""
        while position:
            size = min(chunk_size, position)
            position -= size
            source.seek(position)
            block = source.read(size) + remainder
            lines = block.split(b"\n")
            remainder = lines[0]
            for line in reversed(lines[1:]):
                if line:
                    yield line
        if remainder:
            yield remainder


def event_time(value: Any, fallback: float) -> float:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def cached_rate_limits(codex_home: Path) -> dict[str, Any] | None:
    """Read the newest structured Codex rate-limit event from local rollouts."""
    sessions = codex_home / "sessions"
    try:
        files = sorted(
            sessions.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    newest_codex: tuple[float, dict[str, Any]] | None = None
    for path in files:
        try:
            fallback = path.stat().st_mtime
            for raw_line in reverse_lines(path):
                if b'"rate_limits"' not in raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                limits = payload.get("rate_limits")
                if not isinstance(limits, dict):
                    continue
                limit_id = rate_limit_id(limits)
                if limit_id is not None and limit_id.casefold() != "codex":
                    continue
                seen_at = event_time(event.get("timestamp"), fallback)
                candidate = dict(limits)
                candidate["_seen_at"] = seen_at
                if newest_codex is None or seen_at > newest_codex[0]:
                    newest_codex = (seen_at, candidate)
                break
        except OSError:
            continue
    return newest_codex[1] if newest_codex else None


def value_from(mapping: dict[str, Any], snake: str, camel: str) -> Any:
    return mapping.get(snake, mapping.get(camel))


def limit_windows(
    limits: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not limits:
        return None, None
    raw_windows = [limits.get("primary"), limits.get("secondary")]
    windows = [window for window in raw_windows if isinstance(window, dict)]
    five_hour = None
    weekly = None
    for window in windows:
        duration = value_from(window, "window_minutes", "windowDurationMins")
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            continue
        if duration == 300:
            five_hour = window
        elif duration == 10_080:
            weekly = window
    return five_hour, weekly


PLAN_LABELS = {
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "team": "Team",
    "self_serve_business_prolite": "Business Pro Lite",
    "self_serve_business_usage_based": "Business",
    "business": "Business",
    "ent26": "Enterprise",
    "enterprise_cbp_automation": "Enterprise",
    "enterprise_cbp_usage_based": "Enterprise",
    "enterprise": "Enterprise",
    "edu": "Edu",
    "edu_plus": "Edu Plus",
    "edu_pro": "Edu Pro",
    "unknown": "Unknown",
}


def plan_label(limits: dict[str, Any] | None) -> str:
    if not limits:
        return "--"
    raw_plan = value_from(limits, "plan_type", "planType")
    if not isinstance(raw_plan, str):
        return "--"
    return PLAN_LABELS.get(raw_plan, raw_plan.replace("_", " ").title())


def window_status(
    window: dict[str, Any] | None, now: float | None = None
) -> tuple[int | None, int | None]:
    if not window:
        return None, None
    now = time.time() if now is None else now
    used = value_from(window, "used_percent", "usedPercent")
    reset = value_from(window, "resets_at", "resetsAt")
    try:
        reset_at = int(reset) if reset is not None else None
    except (OverflowError, TypeError, ValueError):
        reset_at = None
    if reset_at is not None and reset_at <= now:
        return None, reset_at
    try:
        remaining = round(100 - float(used))
    except (OverflowError, TypeError, ValueError):
        remaining = None
    if remaining is not None:
        remaining = max(0, min(100, remaining))
    return remaining, reset_at


def reset_label(reset_at: int | None, now: float | None = None) -> str:
    if reset_at is None:
        return "--"
    now = time.time() if now is None else now
    if reset_at <= now:
        return "--"
    try:
        reset = datetime.fromtimestamp(reset_at)
        today = datetime.fromtimestamp(now).date()
    except (OSError, OverflowError, ValueError):
        return "--"
    day_delta = (reset.date() - today).days
    clock = reset.strftime("%I:%M %p").lstrip("0")
    if day_delta == 0:
        return f"Today {clock}"
    if day_delta == 1:
        return f"Tomorrow {clock}"
    return f"{reset.strftime('%b')} {reset.day} {clock}"


def time_left_label(reset_at: int | None, now: float | None = None) -> str:
    if reset_at is None:
        return "--"
    now = time.time() if now is None else now
    seconds = int(reset_at - now)
    if seconds <= 0:
        return "--"
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h left" if hours else f"{days}d left"
    if hours:
        return f"{hours}h {minutes}m left" if minutes else f"{hours}h left"
    return f"{max(1, minutes)}m left"


def usage_value(window: dict[str, Any] | None, now: float | None = None) -> str:
    remaining, _reset_at = window_status(window, now)
    return "--% left" if remaining is None else f"{remaining}% left"


def rate_limit_id(limits: dict[str, Any] | None) -> str | None:
    if not limits:
        return None
    value = value_from(limits, "limit_id", "limitId")
    return value if isinstance(value, str) else None


def merge_sparse_rate_limits(
    current: dict[str, Any] | None,
    update: dict[str, Any],
) -> dict[str, Any]:
    """Merge account/rateLimits/updated metadata using Codex's sparse semantics."""
    current_id = (rate_limit_id(current) or "codex").casefold()
    update_id = (rate_limit_id(update) or "codex").casefold()
    if not current or current_id != update_id:
        return dict(update)
    merged = {key: value for key, value in current.items() if key != "_seen_at"}
    preserve_when_null = {
        "credits",
        "individual_limit",
        "individualLimit",
        "limit_name",
        "limitName",
        "plan_type",
        "planType",
    }
    for key, value in update.items():
        if key == "_seen_at":
            continue
        if value is None and key in preserve_when_null and key in merged:
            continue
        merged[key] = value
    return merged


def snapshot_with_rate_limits(
    snapshot: StatusSnapshot,
    limits: dict[str, Any],
    observed_at: float,
    *,
    sparse: bool = False,
) -> StatusSnapshot:
    """Apply only a strictly newer rate-limit observation to a status snapshot."""
    if snapshot.updated_at is not None and observed_at <= snapshot.updated_at:
        return snapshot
    copied = (
        merge_sparse_rate_limits(snapshot.limits, limits)
        if sparse
        else dict(limits)
    )
    copied["_seen_at"] = observed_at
    candidate_plan = plan_label(copied)
    return StatusSnapshot(
        account=snapshot.account,
        plan=snapshot.plan if candidate_plan == "--" else candidate_plan,
        settings=snapshot.settings,
        cwd=snapshot.cwd,
        limits=copied,
        updated_at=observed_at,
    )


def initial_snapshot(account: Account, cwd: Path | None = None) -> StatusSnapshot:
    limits = cached_rate_limits(account.home)
    return StatusSnapshot(
        account=account,
        plan=plan_label(limits),
        settings=load_model_settings(account.home),
        cwd=Path.cwd() if cwd is None else cwd,
        limits=limits,
        updated_at=limits.get("_seen_at") if limits else None,
    )


class RolloutTracker:
    """Incrementally tail only the rollout created by this launcher run."""

    def __init__(
        self,
        account: Account,
        cwd: Path,
        *,
        started_at: float | None = None,
    ):
        self.account = account
        self.started_at = time.time() if started_at is None else started_at
        self.snapshot = initial_snapshot(account, cwd)
        self.active_path: Path | None = None
        self.offset = 0
        self.remainder = b""
        self.baseline = self._session_sizes()

    def _session_sizes(self) -> dict[Path, int]:
        result: dict[Path, int] = {}
        try:
            for path in (self.account.home / "sessions").rglob("*.jsonl"):
                try:
                    result[path] = path.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
        return result

    def _find_active_path(self) -> None:
        if self.active_path is not None:
            return
        candidates: list[tuple[float, Path, int]] = []
        for path, size in self._session_sizes().items():
            baseline_size = self.baseline.get(path)
            if baseline_size is not None and size <= baseline_size:
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if modified >= self.started_at - 5:
                candidates.append((modified, path, baseline_size or 0))
        if candidates:
            _modified, self.active_path, self.offset = max(candidates)

    def _apply_event(self, event: dict[str, Any]) -> None:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        event_type = event.get("type")
        payload_type = payload.get("type")
        settings: dict[str, Any] | None = None
        if event_type == "turn_context":
            settings = payload
        elif payload_type == "thread_settings_applied":
            raw_settings = payload.get("thread_settings")
            if isinstance(raw_settings, dict):
                settings = raw_settings
        elif event_type == "session_meta":
            raw_cwd = payload.get("cwd")
            if isinstance(raw_cwd, str):
                self.snapshot = self._replace(cwd=Path(raw_cwd))
        if settings is not None:
            model = settings.get("model")
            effort = settings.get(
                "reasoning_effort",
                settings.get("effort", settings.get("model_reasoning_effort")),
            )
            raw_cwd = settings.get("cwd")
            current = self.snapshot.settings
            next_settings = ModelSettings(
                str(model) if model else current.model,
                str(effort) if effort else current.effort,
            )
            next_cwd = Path(raw_cwd) if isinstance(raw_cwd, str) else self.snapshot.cwd
            self.snapshot = self._replace(settings=next_settings, cwd=next_cwd)
        limits = payload.get("rate_limits")
        if isinstance(limits, dict):
            limit_id = rate_limit_id(limits) or "codex"
            current_id = rate_limit_id(self.snapshot.limits)
            if limit_id == "codex" or current_id != "codex":
                self.apply_rate_limits(
                    limits,
                    event_time(event.get("timestamp"), time.time()),
                )

    def apply_rate_limits(
        self,
        limits: dict[str, Any],
        observed_at: float,
        *,
        sparse: bool = False,
    ) -> StatusSnapshot:
        self.snapshot = snapshot_with_rate_limits(
            self.snapshot, limits, observed_at, sparse=sparse
        )
        return self.snapshot

    def _replace(self, **changes: Any) -> StatusSnapshot:
        values = {
            "account": self.snapshot.account,
            "plan": self.snapshot.plan,
            "settings": self.snapshot.settings,
            "cwd": self.snapshot.cwd,
            "limits": self.snapshot.limits,
            "updated_at": self.snapshot.updated_at,
        }
        values.update(changes)
        return StatusSnapshot(**values)

    def refresh(self, child_pid: int | None = None) -> StatusSnapshot:
        self._find_active_path()
        if self.active_path is not None:
            try:
                size = self.active_path.stat().st_size
                if size < self.offset:
                    self.offset = 0
                    self.remainder = b""
                with self.active_path.open("rb") as source:
                    source.seek(self.offset)
                    data = source.read()
                    self.offset = source.tell()
                lines = (self.remainder + data).split(b"\n")
                self.remainder = lines.pop() if lines else b""
                for raw_line in lines:
                    try:
                        event = json.loads(raw_line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(event, dict):
                        self._apply_event(event)
            except OSError:
                pass
        if child_pid is not None:
            try:
                process_cwd = Path(os.readlink(f"/proc/{child_pid}/cwd"))
                if process_cwd != self.snapshot.cwd and self.active_path is None:
                    self.snapshot = self._replace(cwd=process_cwd)
            except OSError:
                pass
        return self.snapshot


def app_server_codex_rate_limits(result: dict[str, Any]) -> dict[str, Any] | None:
    """Select the Codex bucket from account/rateLimits/read's structured result."""
    by_limit_id = result.get(
        "rateLimitsByLimitId", result.get("rate_limits_by_limit_id")
    )
    if isinstance(by_limit_id, dict):
        for key, value in by_limit_id.items():
            if (
                isinstance(key, str)
                and key.casefold() == "codex"
                and isinstance(value, dict)
            ):
                return dict(value)
        for value in by_limit_id.values():
            if (
                isinstance(value, dict)
                and rate_limit_id(value) is not None
                and rate_limit_id(value).casefold() == "codex"
            ):
                return dict(value)
    limits = result.get("rateLimits", result.get("rate_limits"))
    if (
        isinstance(limits, dict)
        and (rate_limit_id(limits) or "codex").casefold() == "codex"
    ):
        return dict(limits)
    return None


def _terminate_process_group(
    process: subprocess.Popen[bytes], *, timeout: float
) -> None:
    """Terminate a child session and any descendants it left running."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()


class AppServerRateLimitReader:
    """Read Codex's native account rate-limit API over a private stdio child."""

    def __init__(
        self,
        codex: str,
        environment: Mapping[str, str],
        *,
        poll_seconds: float = RATE_LIMIT_POLL_SECONDS,
        retry_seconds: float = RATE_LIMIT_RETRY_SECONDS,
        request_timeout_seconds: float = RATE_LIMIT_REQUEST_TIMEOUT_SECONDS,
    ):
        self.codex = codex
        self.environment = dict(environment)
        self.poll_seconds = poll_seconds
        self.retry_seconds = retry_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.process: subprocess.Popen[bytes] | None = None
        self.buffer = b""
        self.initialized = False
        self.pending_request_id: int | None = None
        self.pending_observed_at: float | None = None
        self.next_request_id = 1
        self.next_poll_at = 0.0
        self.restart_at = 0.0
        self.response_deadline: float | None = None
        self.closed = False
        self._start(time.monotonic())

    def _start(self, now: float) -> None:
        if self.closed or self.process is not None:
            return
        try:
            process = subprocess.Popen(
                [self.codex, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.environment,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            self.restart_at = now + self.retry_seconds
            return
        if process.stdin is None or process.stdout is None:
            _terminate_process_group(process, timeout=0.2)
            self.restart_at = now + self.retry_seconds
            return
        self.process = process
        self.buffer = b""
        self.initialized = False
        self.pending_request_id = None
        self.pending_observed_at = None
        self.response_deadline = None
        try:
            os.set_blocking(process.stdout.fileno(), False)
        except OSError:
            self._drop(now)
            return
        if not self._send(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "codex-start",
                        "title": "codex-start",
                        "version": VERSION,
                    }
                },
            }
        ):
            self._drop(now)
        else:
            self.response_deadline = now + self.request_timeout_seconds

    def _send(self, message: dict[str, Any]) -> bool:
        process = self.process
        if process is None or process.stdin is None:
            return False
        remaining = memoryview(
            (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        )
        while remaining:
            try:
                written = os.write(process.stdin.fileno(), remaining)
            except InterruptedError:
                continue
            except OSError:
                return False
            if written <= 0:
                return False
            remaining = remaining[written:]
        return True

    def _request_rate_limits(self, now: float) -> None:
        request_id = self.next_request_id
        self.next_request_id += 1
        observed_at = time.time()
        if self._send({"method": "account/rateLimits/read", "id": request_id}):
            self.pending_request_id = request_id
            self.pending_observed_at = observed_at
            self.response_deadline = now + self.request_timeout_seconds
        else:
            self._drop(now)

    def _message_observation(
        self,
        message: dict[str, Any],
        received_at: float,
        now: float,
    ) -> RateLimitObservation | None:
        message_id = message.get("id")
        if message_id == 0:
            if not isinstance(message.get("result"), dict):
                self._drop(now)
                return None
            if not self._send({"method": "initialized", "params": {}}):
                self._drop(now)
                return None
            self.initialized = True
            self.response_deadline = None
            self._request_rate_limits(now)
            return None
        if message_id == self.pending_request_id:
            observed_at = self.pending_observed_at
            self.pending_request_id = None
            self.pending_observed_at = None
            self.response_deadline = None
            self.next_poll_at = now + self.poll_seconds
            result = message.get("result")
            if not isinstance(result, dict) or observed_at is None:
                return None
            limits = app_server_codex_rate_limits(result)
            if limits is None:
                return None
            return RateLimitObservation(limits, observed_at)
        if message.get("method") == "account/rateLimits/updated":
            params = message.get("params")
            if not isinstance(params, dict):
                return None
            limits = params.get("rateLimits", params.get("rate_limits"))
            if not isinstance(limits, dict):
                return None
            if (rate_limit_id(limits) or "codex").casefold() != "codex":
                return None
            return RateLimitObservation(dict(limits), received_at, sparse=True)
        return None

    def poll(self) -> list[RateLimitObservation]:
        """Drain complete JSON-RPC messages and schedule the next native read."""
        now = time.monotonic()
        if self.closed:
            return []
        if self.process is None:
            if now >= self.restart_at:
                self._start(now)
            return []
        if self.process.poll() is not None:
            self._drop(now)
            return []

        observations: list[RateLimitObservation] = []
        eof = False
        stdout = self.process.stdout
        if stdout is None:
            self._drop(now)
            return observations
        while True:
            try:
                chunk = os.read(stdout.fileno(), 65_536)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            except OSError:
                eof = True
                break
            if not chunk:
                eof = True
                break
            self.buffer += chunk
            while b"\n" in self.buffer:
                raw_line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                observation = self._message_observation(
                    message, time.time(), now
                )
                if observation is not None:
                    observations.append(observation)
        if eof:
            self._drop(now)
            return observations
        if self.response_deadline is not None and now >= self.response_deadline:
            self._drop(now)
            return observations
        if (
            self.initialized
            and self.pending_request_id is None
            and now >= self.next_poll_at
        ):
            self._request_rate_limits(now)
        return observations

    def _drop(self, now: float) -> None:
        process = self.process
        self.process = None
        self.buffer = b""
        self.initialized = False
        self.pending_request_id = None
        self.pending_observed_at = None
        self.response_deadline = None
        self.restart_at = now + self.retry_seconds
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        _terminate_process_group(process, timeout=0.2)

    def close(self) -> None:
        self.closed = True
        process = self.process
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        self._drop(time.monotonic())



@dataclass(frozen=True)
class Span:
    text: str
    color: str = BASE_FOREGROUND
    bold: bool = False
    dim: bool = False


def terminal_safe_text(text: str) -> str:
    """Replace terminal control characters without changing printable layout."""
    return "".join(
        " " if ord(character) < 32 or 127 <= ord(character) < 160 else character
        for character in text
    )


def cell_width(text: str) -> int:
    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
    return width


def clip_text(text: str, width: int, *, ellipsis: bool = False) -> tuple[str, int]:
    if width <= 0:
        return "", 0
    target = width - 1 if ellipsis and cell_width(text) > width and width > 1 else width
    result: list[str] = []
    used = 0
    for character in text:
        character_width = 0 if unicodedata.combining(character) else (
            2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        )
        if used + character_width > target:
            break
        result.append(character)
        used += character_width
    if ellipsis and cell_width(text) > width and width > 1:
        result.append("…")
        used += 1
    return "".join(result), used


def render_spans(
    spans: Sequence[Span],
    width: int,
    mode: str,
    *,
    background: str = DARK_BACKGROUND,
) -> str:
    remaining = max(0, width)
    rendered: list[str] = []
    if mode != "none":
        rendered.append(color_sequence(background, mode, background=True))
    for span in spans:
        if remaining <= 0:
            break
        clipped, used = clip_text(terminal_safe_text(span.text), remaining)
        if not clipped:
            continue
        if mode != "none":
            rendered.append("\x1b[1m" if span.bold else "\x1b[22m")
            rendered.append("\x1b[2m" if span.dim else "\x1b[22m")
            rendered.append(color_sequence(span.color, mode))
        rendered.append(clipped)
        remaining -= used
    if remaining:
        rendered.append(" " * remaining)
    if mode != "none":
        rendered.append("\x1b[0m")
    return "".join(rendered)


def themed(
    theme: Mapping[str, str], field: str, text: str, *, bold: bool = False
) -> Span:
    return Span(text, theme.get(field, DEFAULT_THEME[field]), bold=bold)


def divider(theme: Mapping[str, str] | None = None) -> Span:
    colors = DEFAULT_THEME if theme is None else theme
    return Span(" | ", colors.get("separators", DEFAULT_THEME["separators"]))


def label(text: str, theme: Mapping[str, str] | None = None) -> Span:
    colors = DEFAULT_THEME if theme is None else theme
    return Span(text, colors.get("labels", DEFAULT_THEME["labels"]))


def model_value(settings: ModelSettings) -> str:
    model = settings.model.strip() or "--"
    effort = settings.effort.strip()
    if effort.lower() in ("", "default", "none", "null", "--"):
        return model
    return f"{model} {effort}"


def status_fields(
    snapshot: StatusSnapshot,
    theme: Mapping[str, str],
    now: float,
) -> list[list[Span]]:
    presentation = terminal_status_model(snapshot, now)
    fields: list[list[Span]] = []
    current: list[Span] = []
    for segment in status_rail_segments(presentation):
        if segment.theme_field == "separators":
            if current:
                fields.append(current)
            current = []
            continue
        if not current and not fields and segment.text == " ":
            continue
        color = theme.get(segment.theme_field, DEFAULT_THEME[segment.theme_field])
        current.append(Span(segment.text, color, bold=segment.bold))
    if current:
        fields.append(current)
    return fields


def status_spans(
    snapshot: StatusSnapshot,
    theme: Mapping[str, str],
    now: float,
) -> list[Span]:
    return [
        Span(
            segment.text,
            theme.get(segment.theme_field, DEFAULT_THEME[segment.theme_field]),
            bold=segment.bold,
        )
        for segment in status_rail_segments(terminal_status_model(snapshot, now))
    ]


def _compact_reset_time(reset_at: int | None, now: float) -> str:
    if reset_at is None or reset_at <= now:
        return "--"
    try:
        reset = datetime.fromtimestamp(reset_at)
        today = datetime.fromtimestamp(now).date()
    except (OSError, OverflowError, ValueError):
        return "--"
    clock = reset.strftime("%H:%M")
    if reset.date() == today:
        return clock
    return f"{reset.strftime('%a')} {clock}"


def _usage_percent(window: dict[str, Any] | None, now: float) -> str:
    remaining, _reset_at = window_status(window, now)
    return "--%" if remaining is None else f"{remaining}%"


def terminal_status_model(
    snapshot: StatusSnapshot,
    now: float | None = None,
) -> StatusModel:
    """Adapt structured launcher state into the GTK-free rail model."""

    now = time.time() if now is None else now
    five_hour, weekly = limit_windows(snapshot.limits)
    _five_remaining, five_reset = window_status(five_hour, now)
    _week_remaining, week_reset = window_status(weekly, now)
    return StatusModel(
        directory=DirectoryPresentation.from_path(snapshot.cwd),
        account=snapshot.account.name,
        plan=snapshot.plan,
        model=model_value(snapshot.settings),
        five_hour=_usage_percent(five_hour, now),
        five_hour_reset=_compact_reset_time(five_reset, now),
        weekly=_usage_percent(weekly, now),
        weekly_reset=_compact_reset_time(week_reset, now),
    )


def compact_title(snapshot: StatusSnapshot, now: float | None = None) -> str:
    status = terminal_status_model(snapshot, now)
    return terminal_safe_text(
        " | ".join(
            (
                f"{status.account} • {status.plan}",
                status.model,
                status.compact_limits(),
            )
        )
    )


class TerminalFilter:
    """Keep terminal-native rendering while containing hostile viewport modes.

    Codex output is otherwise streamed unchanged to the real terminal. VTE
    therefore remains the only screen and scrollback owner.
    """

    _ALTERNATE_MODES = {b"47", b"1047", b"1049"}
    _MOUSE_MODES = {
        b"9",
        b"1000",
        b"1001",
        b"1002",
        b"1003",
        b"1005",
        b"1006",
        b"1007",
        b"1015",
        b"1016",
    }
    _BLOCKED_MODES = _ALTERNATE_MODES | _MOUSE_MODES
    _TITLE_COMMANDS = {b"0", b"1", b"2"}

    def __init__(self) -> None:
        self.pending = b""

    def feed(self, data: bytes) -> bytes:
        data = self.pending + data
        self.pending = b""
        output = bytearray()
        index = 0
        while index < len(data):
            if data[index] != 0x1B:
                output.append(data[index])
                index += 1
                continue
            if index + 1 >= len(data):
                self.pending = data[index:]
                break
            introducer = data[index + 1]
            if introducer == ord("["):
                end = index + 2
                while end < len(data) and not 0x40 <= data[end] <= 0x7E:
                    end += 1
                if end >= len(data):
                    self.pending = data[index:]
                    break
                output.extend(self._filter_csi(data[index : end + 1]))
                index = end + 1
                continue
            if introducer == ord("]"):
                end, terminator = self._osc_end(data, index + 2)
                if end is None:
                    self.pending = data[index:]
                    break
                body = data[index + 2 : end]
                command = body.split(b";", 1)[0]
                if command not in self._TITLE_COMMANDS:
                    output.extend(data[index : end + terminator])
                index = end + terminator
                continue
            output.extend(data[index : index + 2])
            index += 2
        return bytes(output)

    def finish(self) -> bytes:
        """Discard an incomplete terminal control sequence at child EOF."""
        self.pending = b""
        return b""

    @classmethod
    def _filter_csi(cls, sequence: bytes) -> bytes:
        body = sequence[2:-1]
        final = sequence[-1:]
        if final not in (b"h", b"l") or not body.startswith(b"?"):
            return sequence
        remaining = [
            mode
            for mode in body[1:].split(b";")
            if mode not in cls._BLOCKED_MODES
        ]
        if not remaining:
            return b""
        return b"\x1b[?" + b";".join(remaining) + final

    @staticmethod
    def _osc_end(data: bytes, start: int) -> tuple[int | None, int]:
        position = start
        while position < len(data):
            if data[position] == 0x07:
                return position, 1
            if (
                data[position] == 0x1B
                and position + 1 < len(data)
                and data[position + 1] == ord("\\")
            ):
                return position, 2
            position += 1
        return None, 0


def write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        except OSError:
            return
        if written <= 0:
            return
        view = view[written:]


def terminal_size(descriptor: int) -> tuple[int, int]:
    try:
        packed = fcntl.ioctl(descriptor, termios.TIOCGWINSZ, b"\0" * 8)
        rows, columns, _xpixel, _ypixel = struct.unpack("HHHH", packed)
        return max(1, columns), max(1, rows)
    except OSError:
        size = shutil.get_terminal_size((80, 24))
        return size.columns, size.lines


def set_window_size(descriptor: int, rows: int, columns: int) -> None:
    packed = struct.pack("HHHH", max(1, rows), max(1, columns), 0, 0)
    fcntl.ioctl(descriptor, termios.TIOCSWINSZ, packed)


def _spawn_pty(
    command: Sequence[str],
    environment: Mapping[str, str],
    rows: int,
    columns: int,
) -> tuple[subprocess.Popen[bytes], int]:
    master_fd, slave_fd = pty.openpty()
    set_window_size(slave_fd, rows, columns)

    def child_setup() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    try:
        child = subprocess.Popen(
            list(command),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=dict(environment),
            close_fds=True,
            preexec_fn=child_setup,
        )
    except BaseException:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    os.close(slave_fd)
    return child, master_fd


def _exit_code(returncode: int) -> int:
    return 128 + abs(returncode) if returncode < 0 else returncode


def _terminal_mode_reset() -> bytes:
    return (
        b"\x1b[?9;1000;1001;1002;1003;1005;1006;1007;1015;1016l"
        b"\x1b[?1004l\x1b[?2004l\x1b[?6l\x1b[r\x1b[0m\x1b[?25h"
    )


def terminal_initialization(snapshot: StatusSnapshot) -> bytes:
    """Prepare one normal-screen VTE session and erase prior shell history."""
    title = compact_title(snapshot).encode("utf-8", "replace")
    return (
        b"\x1b[22;0t"
        + _terminal_mode_reset()
        + b"\x1b[2J\x1b[3J\x1b[H"
        + b"\x1b]0;"
        + title
        + b"\x07"
    )


def terminal_restoration() -> bytes:
    """Restore terminal interaction modes without erasing Codex scrollback."""
    return _terminal_mode_reset() + b"\x1b[23;0t"


def terminal_title(snapshot: StatusSnapshot) -> bytes:
    return (
        b"\x1b]0;"
        + compact_title(snapshot).encode("utf-8", "replace")
        + b"\x07"
    )


def run_terminal(
    account: Account,
    command: Sequence[str],
    environment: Mapping[str, str],
    *,
    input_fd: int | None = None,
    output_fd: int | None = None,
    rate_limit_reader_factory: Callable[
        [str, Mapping[str, str]], AppServerRateLimitReader | None
    ] = AppServerRateLimitReader,
) -> int:
    """Relay one Codex PTY directly into the host terminal's native screen."""
    input_fd = sys.stdin.fileno() if input_fd is None else input_fd
    output_fd = sys.stdout.fileno() if output_fd is None else output_fd
    width, height = terminal_size(output_fd)
    tracker = RolloutTracker(account, Path.cwd())
    snapshot = tracker.snapshot
    old_terminal = termios.tcgetattr(input_fd)
    child, master_fd = _spawn_pty(command, environment, height, width)
    output_filter = TerminalFilter()
    event_selector = selectors.DefaultSelector()
    old_handlers: dict[signal.Signals, Any] = {}
    state: dict[str, bool | int | None] = {"resize": False, "signal": None}

    def mark_resize(_signum: int, _frame: Any) -> None:
        state["resize"] = True

    def mark_signal(signum: int, _frame: Any) -> None:
        state["signal"] = signum

    for signal_name in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP):
        old_handlers[signal_name] = signal.getsignal(signal_name)
    signal.signal(signal.SIGWINCH, mark_resize)
    signal.signal(signal.SIGTERM, mark_signal)
    signal.signal(signal.SIGHUP, mark_signal)

    pty_eof = False
    rate_limit_reader: AppServerRateLimitReader | None = None
    next_refresh = 0.0
    last_title: bytes | None = None

    def update_title(current: StatusSnapshot) -> None:
        nonlocal last_title
        sequence = terminal_title(current)
        if sequence != last_title:
            write_all(output_fd, sequence)
            last_title = sequence

    try:
        tty.setraw(input_fd)
        os.set_blocking(master_fd, False)
        event_selector.register(input_fd, selectors.EVENT_READ, "input")
        event_selector.register(master_fd, selectors.EVENT_READ, "child")
        write_all(output_fd, terminal_initialization(snapshot))
        last_title = terminal_title(snapshot)
        rate_limit_reader = rate_limit_reader_factory(str(command[0]), environment)

        while True:
            now = time.monotonic()
            timeout = max(0.0, min(0.25, next_refresh - now))
            for key, _mask in event_selector.select(timeout):
                if key.data == "input":
                    try:
                        user_input = os.read(input_fd, 65_536)
                    except OSError:
                        user_input = b""
                    if user_input:
                        write_all(master_fd, user_input)
                    else:
                        try:
                            event_selector.unregister(input_fd)
                        except (KeyError, ValueError):
                            pass
                else:
                    try:
                        child_output = os.read(master_fd, 65_536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            child_output = b""
                        else:
                            raise
                    if child_output:
                        write_all(output_fd, output_filter.feed(child_output))
                    else:
                        write_all(output_fd, output_filter.finish())
                        pty_eof = True
                        try:
                            event_selector.unregister(master_fd)
                        except (KeyError, ValueError):
                            pass

            if state["resize"]:
                state["resize"] = False
                width, height = terminal_size(output_fd)
                set_window_size(master_fd, height, width)

            pending_signal = state["signal"]
            if isinstance(pending_signal, int):
                state["signal"] = None
                try:
                    os.killpg(child.pid, pending_signal)
                except ProcessLookupError:
                    pass

            if rate_limit_reader is not None:
                observations = rate_limit_reader.poll()
                for observation in observations:
                    snapshot = tracker.apply_rate_limits(
                        observation.limits,
                        observation.observed_at,
                        sparse=observation.sparse,
                    )
                if observations:
                    update_title(snapshot)

            if time.monotonic() >= next_refresh:
                snapshot = tracker.refresh(child.pid)
                update_title(snapshot)
                next_refresh = time.monotonic() + POLL_SECONDS

            returncode = child.poll()
            if returncode is not None and pty_eof:
                return _exit_code(returncode)
    except KeyboardInterrupt:
        try:
            os.killpg(child.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            return _exit_code(child.wait(timeout=2))
        except subprocess.TimeoutExpired:
            return 130
    finally:
        event_selector.close()
        if rate_limit_reader is not None:
            rate_limit_reader.close()
        for signal_name, handler in old_handlers.items():
            signal.signal(signal_name, handler)
        try:
            termios.tcsetattr(input_fd, termios.TCSADRAIN, old_terminal)
        except OSError:
            pass
        write_all(output_fd, terminal_restoration())
        try:
            os.close(master_fd)
        except OSError:
            pass
        _terminate_process_group(child, timeout=2)


def plain_status(snapshot: StatusSnapshot, now: float | None = None) -> str:
    now = time.time() if now is None else now
    return "".join(
        terminal_safe_text(span.text)
        for span in status_spans(
            snapshot,
            DEFAULT_THEME,
            now,
        )
    ).rstrip()


def run_direct(
    command: Sequence[str],
    environment: Mapping[str, str],
    snapshot: StatusSnapshot,
) -> int:
    print(plain_status(snapshot))
    print()
    sys.stdout.flush()
    try:
        return _exit_code(subprocess.call(list(command), env=dict(environment)))
    except KeyboardInterrupt:
        return 130


def codex_command(
    codex: str,
    *,
    extra_args: Sequence[str] = (),
) -> list[str]:
    command = [
        codex,
        "--approve-for-me",
        "-c",
        f"tui.status_line={NATIVE_STATUS_ITEMS}",
        "-c",
        "tui.terminal_title=[]",
        "-c",
        "tui.alternate_screen=never",
    ]
    command.extend(extra_args)
    return command


def build_terminal_host_launch(
    account: Account,
    codex_path: str,
    *,
    extra_args: Sequence[str] = (),
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    python_executable: str | None = None,
    bridge_path: Path | None = None,
) -> TerminalHostLaunch:
    """Build the child contract without importing or initializing GTK."""

    host_environment = dict(os.environ if environment is None else environment)
    host_environment["CODEX_HOME"] = str(account.home)
    host_environment[HOSTED_ENVIRONMENT] = "1"
    host_environment[HOST_STATUS_OWNER_ENVIRONMENT] = "host"
    bridge = (
        Path(__file__).resolve().with_name("codex_terminal_bridge.py")
        if bridge_path is None
        else bridge_path
    )
    argv = (
        python_executable or sys.executable,
        str(bridge),
        "--account-name",
        account.name,
        "--account-home",
        str(account.home),
        "--codex",
        codex_path,
        "--",
        *extra_args,
    )
    return TerminalHostLaunch(
        account=account,
        codex_path=codex_path,
        argv=argv,
        cwd=Path.cwd() if cwd is None else cwd,
        environment=host_environment,
    )


def _launch_terminal_host(
    launch_spec: TerminalHostLaunch,
    snapshot: StatusSnapshot,
    theme_store: ThemeStore,
) -> int | None:
    try:
        from codex_terminal_ui import launch_terminal_host
    except ImportError:
        return None
    return launch_terminal_host(launch_spec, snapshot, theme_store)


def launch(
    account: Account,
    *,
    plain: bool = False,
    extra_args: Sequence[str] = (),
    codex_path: str | None = None,
) -> int:
    if not account.home.is_dir():
        raise SystemExit(f"Codex home does not exist: {account.home}")
    codex = codex_path or shutil.which("codex")
    if not codex:
        raise SystemExit("codex was not found on PATH")
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(account.home)
    save_runtime(account)
    command = codex_command(codex, extra_args=extra_args)
    snapshot = initial_snapshot(account)
    interactive = (
        environment.get("TERM", "") != "dumb"
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    if interactive:
        if not plain and environment.get(HOSTED_ENVIRONMENT) != "1":
            host_launch = build_terminal_host_launch(
                account,
                codex,
                extra_args=extra_args,
                environment=environment,
                cwd=snapshot.cwd,
            )
            hosted_result = _launch_terminal_host(
                host_launch, snapshot, ThemeStore()
            )
            if hosted_result is not None:
                return hosted_result
        return run_terminal(account, command, environment)
    return run_direct(command, environment, snapshot)



def accounts() -> tuple[Account, ...]:
    """Return configured accounts (kept as a small public convenience API)."""
    return load_accounts()


def _selector_frame(
    entries: Sequence[Account],
    selected: int,
    width: int,
    height: int,
    theme_store: ThemeStore,
    mode: str,
) -> str:
    width = max(1, width)
    height = max(1, height)
    lines: list[str] = []
    if height < 7:
        account = entries[selected] if entries else None
        theme = theme_store.theme_for(account.name if account else "")
        compact_lines = [
            [Span(" codex-start ", BASE_FOREGROUND, bold=True)],
            (
                [
                    themed(theme, "account", "> ", bold=True),
                    themed(theme, "account", account.name, bold=True),
                    Span(
                        f"  [{THEME_PRESETS[theme_store.preset_for(account.name)].label}]",
                        MUTED_FOREGROUND,
                    ),
                ]
                if account
                else [Span(" No Codex accounts configured", MUTED_FOREGROUND)]
            ),
            [
                Span(
                    " + Add account (a)" if not account
                    else " Enter launch · t theme · a add · q quit",
                    BASE_FOREGROUND if not account else MUTED_FOREGROUND,
                    bold=not bool(account),
                )
            ],
        ]
        for spans in compact_lines[:height]:
            lines.append(render_spans(spans, width, mode))
        while len(lines) < height:
            lines.append(render_spans([], width, mode))
        return "\x1b[H" + "\r\n".join(lines)
    title = "codex-start"
    subtitle = "Choose a Codex account"
    left = max(1, (width - len(title)) // 2)
    lines.append(
        render_spans(
            [Span(" " * left), Span(title, BASE_FOREGROUND, bold=True)],
            width,
            mode,
        )
    )
    lines.append(
        render_spans(
            [Span("─" * width, SEPARATOR_FOREGROUND, dim=True)], width, mode
        )
    )
    lines.append(
        render_spans([Span(f"  {subtitle}", MUTED_FOREGROUND)], width, mode)
    )
    lines.append(render_spans([], width, mode))
    if not entries:
        empty_lines = [
            [Span("  No Codex accounts configured", MUTED_FOREGROUND)],
            [Span("  + Add account", BASE_FOREGROUND, bold=True)],
        ]
        for spans in empty_lines:
            if len(lines) < height - 2:
                lines.append(render_spans(spans, width, mode))
    available = max(1, height - 7)
    first = max(0, min(selected - available // 2, max(0, len(entries) - available)))
    for index in range(first, min(len(entries), first + available)):
        account = entries[index]
        marker = "  > " if index == selected else "    "
        theme = theme_store.theme_for(account.name)
        settings = load_model_settings(account.home)
        spans = [
            themed(theme, "account", marker, bold=index == selected),
            Span(f"{index + 1}  ", MUTED_FOREGROUND),
            themed(
                theme,
                "account",
                account.name,
                bold=index == selected,
            ),
        ]
        preset_label = THEME_PRESETS[
            theme_store.preset_for(account.name)
        ].label
        spans.extend(
            (
                Span("  ", MUTED_FOREGROUND),
                Span(f"[{preset_label}]", MUTED_FOREGROUND),
            )
        )
        if width >= 58:
            spans.extend(
                (
                    divider(theme),
                    themed(
                        theme,
                        "model",
                        f"{settings.model} {settings.effort}",
                    ),
                )
            )
        if width >= 100:
            spans.extend(
                (
                    divider(theme),
                    themed(theme, "directory", compact_path(account.home)),
                )
            )
        lines.append(render_spans(spans, width, mode))
    while len(lines) < height - 2:
        lines.append(render_spans([], width, mode))
    lines.append(
        render_spans(
            [
                Span(
                    (
                        "  a Add account  ·  q quit"
                        if not entries
                        else "  ↑/↓ or j/k move  ·  Enter launch  ·  "
                        "1-9 select  ·  t theme  ·  a Add account  ·  q quit"
                    ),
                    MUTED_FOREGROUND,
                )
            ],
            width,
            mode,
        )
    )
    lines.append(render_spans([], width, mode))
    return "\x1b[H" + "\r\n".join(lines[:height])


def choose_account(entries: Sequence[Account] | None = None) -> Account:
    choices = load_accounts() if entries is None else tuple(entries)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        if not choices:
            print("No Codex accounts configured")
            print("+ Add account")
            try:
                action = input("Press a to add an account, or q to quit: ").strip()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                raise SystemExit(130)
            if action.casefold() == "a":
                return prompt_add_account(choices)
            raise SystemExit(130)
        print("Codex accounts:")
        for number, account in enumerate(choices, 1):
            print(f"  {number}. {account.name} ({compact_path(account.home)})")
        try:
            choice = input("Select account: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            raise SystemExit(130)
        account = resolve_account(choice, choices)
        if account is None:
            raise SystemExit(f"Invalid selection: {choice or '<empty>'}")
        return account

    runtime = load_runtime()
    last_account = runtime.get("last_account")
    selected = next(
        (
            index
            for index, account in enumerate(choices)
            if account.name == last_account
        ),
        0,
    )
    input_fd = sys.stdin.fileno()
    output_fd = sys.stdout.fileno()
    old_terminal = termios.tcgetattr(input_fd)
    state: dict[str, Any] = {"needed": False, "signal": None}
    old_handlers = {
        signal_name: signal.getsignal(signal_name)
        for signal_name in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP)
    }

    def mark_resize(_signum: int, _frame: Any) -> None:
        state["needed"] = True

    def mark_signal(signum: int, _frame: Any) -> None:
        state["signal"] = signum
        state["needed"] = True

    signal.signal(signal.SIGWINCH, mark_resize)
    signal.signal(signal.SIGTERM, mark_signal)
    signal.signal(signal.SIGHUP, mark_signal)
    theme_store = ThemeStore()
    mode = detect_color_mode(is_tty=True)
    action: str | None = None
    try:
        tty.setraw(input_fd)
        write_all(output_fd, b"\x1b[?1049h\x1b[?25l\x1b[2J")
        while True:
            pending_signal = state["signal"]
            if isinstance(pending_signal, int):
                raise SystemExit(128 + pending_signal)
            width, height = terminal_size(output_fd)
            frame = _selector_frame(
                choices, selected, width, height, theme_store, mode
            )
            write_all(output_fd, frame.encode("utf-8", "replace"))
            state["needed"] = False
            ready, _write, _error = select_with_resize(input_fd, state)
            if not ready:
                continue
            key = os.read(input_fd, 32)
            if not key:
                raise SystemExit(130)
            if key in (b"\r", b"\n"):
                if choices:
                    return choices[selected]
            if key in (b"q", b"Q", b"\x03", b"\x1b"):
                raise SystemExit(130)
            if key in (b"a", b"A"):
                action = "add"
                break
            if key in (b"t", b"T") and choices:
                theme_store.cycle_preset(choices[selected].name)
            elif key in (b"\x1b[A", b"k", b"K") and choices:
                selected = (selected - 1) % len(choices)
            elif key in (b"\x1b[B", b"j", b"J") and choices:
                selected = (selected + 1) % len(choices)
            elif choices and len(key) == 1 and key.isdigit() and key != b"0":
                number = int(key)
                if number <= len(choices):
                    selected = number - 1
                    return choices[selected]
    finally:
        for signal_name, handler in old_handlers.items():
            signal.signal(signal_name, handler)
        termios.tcsetattr(input_fd, termios.TCSADRAIN, old_terminal)
        write_all(output_fd, b"\x1b[0m\x1b[?25h\x1b[?1049l")
    if action == "add":
        return prompt_add_account(choices, theme_store=theme_store)
    raise SystemExit(130)


def select_with_resize(
    descriptor: int, resize: Mapping[str, Any]
) -> tuple[list[int], list[int], list[int]]:
    import select

    while not resize.get("needed"):
        readable, writable, exceptional = select.select([descriptor], [], [], 0.25)
        if readable:
            return readable, writable, exceptional
    return [], [], []


def theme_command(
    argv: Sequence[str], entries: Sequence[Account] | None = None
) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-start theme",
        description="Show, change, reset, or copy a persistent account theme.",
    )
    parser.add_argument("account", help="account name, number, or 'default'")
    parser.add_argument("operation", nargs="*")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="legacy alias for resetting the entire selected theme",
    )
    args = parser.parse_args(list(argv))
    entries = load_accounts() if entries is None else entries
    if args.account == "default":
        theme_name = "default"
    else:
        account = resolve_account(args.account, entries)
        if account is None:
            raise SystemExit(f"Unknown account: {args.account}")
        theme_name = account.name

    store = ThemeStore()
    operation = list(args.operation)
    if args.reset:
        if operation:
            parser.error("--reset cannot be combined with another operation")
        operation = ["reset"]

    if not operation or operation == ["show"]:
        effective = (
            store.theme_for("")
            if theme_name == "default"
            else store.theme_for(theme_name)
        )
        print(f"{theme_name} theme ({store.path})")
        model = store.theme_model_for(theme_name)
        print(f"  {'preset':<12} {THEME_PRESETS[model.preset].label}")
        print(
            f"  {'terminal':<12} {model.terminal_background_mode}"
        )
        print(
            f"  {'neutral_bg':<12} {model.neutral_terminal_background}"
        )
        for field in THEME_FIELDS:
            print(f"  {field:<12} {effective[field]}")
        return 0

    verb = operation[0]
    if verb == "preset":
        if len(operation) != 2:
            parser.error("usage: codex-start theme ACCOUNT preset PRESET")
        try:
            store.set_preset(theme_name, operation[1])
        except (OSError, ValueError) as error:
            parser.error(str(error))
        selected = store.preset_for(theme_name)
        print(f"{theme_name}.preset = {THEME_PRESETS[selected].label}")
        return 0

    if verb == "terminal-background":
        if len(operation) not in (2, 3):
            parser.error(
                "usage: codex-start theme ACCOUNT terminal-background "
                "MODE [NEUTRAL_COLOR]"
            )
        try:
            store.set_terminal_background_mode(theme_name, operation[1])
            if len(operation) == 3:
                store.set_neutral_terminal_background(
                    theme_name, operation[2]
                )
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print(f"{theme_name}.terminal_background_mode = {operation[1]}")
        return 0

    if verb == "reset":
        if len(operation) > 2:
            parser.error("usage: codex-start theme ACCOUNT reset [FIELD]")
        field = normalize_theme_field(operation[1]) if len(operation) == 2 else None
        try:
            store.reset(theme_name, field)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        target = f"{theme_name}.{field}" if field else f"{theme_name} theme"
        print(f"{target} reset to inherited default")
        return 0

    if verb == "copy-from":
        if len(operation) != 2:
            parser.error("usage: codex-start theme ACCOUNT copy-from SOURCE")
        source_value = operation[1]
        if source_value == "default":
            source_name = ""
        else:
            source = resolve_account(source_value, entries)
            if source is None:
                parser.error(f"unknown source account: {source_value}")
            source_name = source.name
        try:
            store.copy_from(theme_name, source_name)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print(f"{theme_name} theme copied from {source_value}")
        return 0

    values = operation[1:] if verb == "set" else operation
    if len(values) < 2:
        parser.error("usage: codex-start theme ACCOUNT set FIELD COLOR")
    field = normalize_theme_field(values[0])
    color_parts = values[1:]
    color = ",".join(color_parts) if len(color_parts) == 3 else " ".join(color_parts)
    try:
        store.set_color(theme_name, field, color)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"{theme_name}.{field} = {normalize_color(color)}")
    return 0


def accounts_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-start accounts",
        description="List or extend Codex account definitions.",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="create an empty accounts.json if it is missing",
    )
    parser.add_argument(
        "--add",
        nargs=2,
        metavar=("NAME", "CODEX_HOME"),
        help="add an account to accounts.json",
    )
    parser.add_argument(
        "--path", action="store_true", help="print the account configuration path"
    )
    parser.add_argument(
        "--theme",
        default="default",
        metavar="PRESET",
        help="generic theme preset for --add (default: Default)",
    )
    args = parser.parse_args(list(argv))
    path = config_dir() / "accounts.json"
    if args.init or args.add:
        try:
            path = ensure_accounts_config()
        except OSError as error:
            raise SystemExit(
                f"Could not create account configuration: {error}"
            ) from error
    if args.path and not args.add:
        print(path)
        return 0
    entries = list(load_accounts())
    if args.add:
        name, raw_home = args.add
        try:
            add_account(
                name,
                raw_home,
                preset=args.theme,
                entries=entries,
            )
        except (OSError, ValueError) as error:
            raise SystemExit(
                f"Could not save account configuration: {error}"
            ) from error
        entries = list(load_accounts())
    if args.path:
        print(path)
        return 0
    print(f"accounts: {path}")
    for number, account in enumerate(entries, 1):
        exists = "" if account.home.is_dir() else " (missing)"
        print(f"  {number}. {account.name}: {compact_path(account.home)}{exists}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-start",
        description="Choose a local Codex account and launch an interactive session.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "commands:\n"
            "  accounts  list or add Codex homes\n"
            "  theme     show or change account colors\n"
            "  theme-ui  edit account colors in a local browser"
        ),
    )
    parser.add_argument(
        "account", nargs="?", help="account name or number (omit for the picker)"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print cached structured status without launching Codex",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="use the current terminal without the optional GTK/VTE host",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {VERSION}"
    )
    parser.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="arguments after -- are passed to Codex",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "theme-ui":
        # Keep HTTP and browser modules out of the normal launcher/PTY path.
        from codex_theme_ui import theme_ui_command

        return theme_ui_command(arguments[1:])
    if arguments and arguments[0] == "theme":
        return theme_command(arguments[1:])
    if arguments and arguments[0] == "accounts":
        return accounts_command(arguments[1:])
    args = build_parser().parse_args(arguments)
    entries = load_accounts()
    account = (
        resolve_account(args.account, entries)
        if args.account
        else choose_account(entries)
    )
    if account is None:
        available = ", ".join(entry.name for entry in entries)
        raise SystemExit(f"Unknown account: {args.account} (available: {available})")
    if args.status:
        print(plain_status(initial_snapshot(account)))
        return 0
    extra_args = list(args.codex_args)
    if extra_args and extra_args[0] == "--":
        extra_args.pop(0)
    return launch(account, plain=args.plain, extra_args=extra_args)
