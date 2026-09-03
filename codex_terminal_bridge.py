#!/usr/bin/env python3
"""Internal transparent PTY bridge used inside the standalone VTE host."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

import codex_start


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--account-name", required=True)
    parser.add_argument("--account-home", required=True)
    parser.add_argument("--codex", required=True)
    parser.add_argument("codex_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    extra_args = list(args.codex_args)
    if extra_args and extra_args[0] == "--":
        extra_args.pop(0)
    account = codex_start.Account(args.account_name, Path(args.account_home))
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(account.home)
    environment[codex_start.HOSTED_ENVIRONMENT] = "1"
    command = codex_start.codex_command(args.codex, extra_args=extra_args)
    return codex_start.run_terminal(
        account,
        command,
        environment,
        rate_limit_reader_factory=lambda *_args: None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
