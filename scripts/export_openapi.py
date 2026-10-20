#!/usr/bin/env python3
"""Write the OpenAPI document of Keeper to `docs/openapi.json`.

The document is built from the application factory with default settings (no database, no
authentication, no environment overrides), so it depends on the code only. The web app derives
its types from it and `tests/tooling/test_openapi_export.py` fails when the committed copy is
stale: run `make openapi` after changing a route or a model.

    export_openapi.py            write docs/openapi.json
    export_openapi.py --check    exit 1 when the committed file differs
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "openapi.json"
ENV_PREFIX = "LOREBASE_"


def render() -> str:
    """The OpenAPI document as stable, indented JSON text."""
    for name in [n for n in os.environ if n.startswith(ENV_PREFIX)]:
        del os.environ[name]  # ambient configuration must not leak into the document
    from lorebase_keeper.app import create_app
    from lorebase_platform.settings import Settings

    document = create_app(Settings(keeper={})).openapi()
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the file is stale")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        if not TARGET.exists() or TARGET.read_text() != text:
            print(f"{TARGET} is stale; run `make openapi`", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(text)
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
