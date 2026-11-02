"""Entry point of the `screenguide` command.

A shortcut's `toggle` reaches the running panel before anything else is imported: loading the rest
of the app, the Anthropic SDK above all, takes most of a second.
"""

import sys
from collections.abc import Sequence

from screenguide.instance import forward


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args == ["toggle"] and forward("toggle"):
        return 0
    from screenguide.cli import main as cli_main

    return cli_main(args)
