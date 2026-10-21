#!/usr/bin/env python3
"""Write the generated reference pages of the documentation site.

    generate.py            write docs/reference/cli.md and docs/reference/configuration.md
    generate.py --check    exit 1 when a committed page differs from what the sources produce

The API reference needs no generator: the page embeds the committed `docs/openapi.json`, which
`scripts/export_openapi.py` keeps current.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli_reference
import config_reference

PAGES: dict[str, tuple[Path, Callable[[], str]]] = {
    "cli": (cli_reference.TARGET, cli_reference.render),
    "configuration": (config_reference.TARGET, config_reference.render),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when a page is stale")
    args = parser.parse_args(argv)
    stale = []
    for target, render in PAGES.values():
        text = render()
        if args.check:
            if not target.exists() or target.read_text() != text:
                stale.append(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            print(f"wrote {target}")
    for target in stale:
        print(f"{target} is stale; run `make docs-generate`", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
