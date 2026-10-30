"""Row-level security is lifted only where it is meant to be.

System paths (job queue, webhook deliveries, audit and trace stores, background passes) read
and write across Domains with `rls_scope(session, bypass=True)` or by setting
`lorebase.bypass_rls` themselves. Every such site of the package sources is pinned here, per
module and count: a new one fails this test until it is reviewed and added to `ALLOWED`.
Migrations are not scanned.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETTING = "lorebase.bypass_rls"
DEFINITION = "packages/store/src/lorebase_store/auth/scope.py"
"""Where `rls_scope` and the setting's name are defined; not a bypass site itself."""

ALLOWED: dict[str, int] = {
    "packages/scribe/src/lorebase_scribe/scheduler.py": 1,
    "packages/store/src/lorebase_store/auth/audit.py": 6,  # one conditional: no Domain given
    "packages/store/src/lorebase_store/auth/grants.py": 1,
    "packages/store/src/lorebase_store/embeddings.py": 1,
    "packages/store/src/lorebase_store/indexes.py": 1,
    "packages/store/src/lorebase_store/jobs.py": 7,
    "packages/store/src/lorebase_store/materialize/lineage_backfill.py": 2,
    "packages/store/src/lorebase_store/materialize/paths.py": 1,
    "packages/store/src/lorebase_store/materialize/stale.py": 1,
    "packages/store/src/lorebase_store/storage.py": 4,
    "packages/store/src/lorebase_store/tracing.py": 4,
    "packages/store/src/lorebase_store/vault/rotation.py": 2,
    "packages/store/src/lorebase_store/webhooks/delivery.py": 9,
}


def _is_bypass(node: ast.AST) -> bool:
    """A call passing `bypass=` anything but a literal false, or a string naming the setting."""
    if isinstance(node, ast.Call):
        return any(
            keyword.arg == "bypass"
            and not (isinstance(keyword.value, ast.Constant) and not keyword.value.value)
            for keyword in node.keywords
        )
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and SETTING in node.value


def bypass_sites() -> Counter[str]:
    found: Counter[str] = Counter()
    for path in sorted(ROOT.glob("packages/*/src/**/*.py")):
        name = path.relative_to(ROOT).as_posix()
        if name == DEFINITION:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
        count = sum(1 for node in ast.walk(tree) if _is_bypass(node))
        if count:
            found[name] = count
    return found


def test_row_level_security_is_lifted_only_at_the_pinned_sites() -> None:
    found = bypass_sites()
    unexpected = {name: n for name, n in found.items() if n > ALLOWED.get(name, 0)}
    assert not unexpected, (
        f"new row-level security bypass sites {unexpected}: review them, then update ALLOWED"
    )
    # Removed sites are pinned down too, so the list stays exact.
    stale = {name: n for name, n in ALLOWED.items() if found.get(name, 0) < n}
    assert not stale, f"fewer bypass sites than pinned {stale}: update ALLOWED"


def test_the_scan_sees_both_forms() -> None:
    call = ast.parse("rls_scope(session, bypass=True)").body[0]
    plain = ast.parse("rls_scope(session, bypass=False)").body[0]
    literal = ast.parse("text(\"SELECT set_config('lorebase.bypass_rls', 'on', true)\")").body[0]
    assert any(_is_bypass(node) for node in ast.walk(call))
    assert not any(_is_bypass(node) for node in ast.walk(plain))
    assert any(_is_bypass(node) for node in ast.walk(literal))
