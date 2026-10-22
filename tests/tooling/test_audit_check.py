"""scripts/audit_check.py: advisories fail unless allowlisted, and allowlist entries expire."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_check.py"
TODAY = dt.date(2026, 6, 1)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_check_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

PIP_REPORT = {
    "dependencies": [
        {"name": "requests", "version": "2.0.0", "vulns": []},
        {
            "name": "urllib3",
            "version": "1.0.0",
            "vulns": [
                {
                    "id": "PYSEC-2099-1",
                    "aliases": ["CVE-2099-0001", "GHSA-aaaa-bbbb-cccc"],
                    "fix_versions": ["1.0.1"],
                }
            ],
        },
    ]
}

PNPM_REPORT = {
    "advisories": {
        "1": {
            "github_advisory_id": "GHSA-dddd-eeee-ffff",
            "cves": ["CVE-2099-0002"],
            "module_name": "left-pad",
            "severity": "high",
            "patched_versions": "<0.0.0",
            "findings": [{"version": "1.3.0"}],
        },
        "2": {
            "github_advisory_id": "GHSA-gggg-hhhh-iiii",
            "cves": [],
            "module_name": "minor-pkg",
            "severity": "low",
            "patched_versions": ">=2.0.0",
            "findings": [{"version": "1.0.0"}],
        },
    }
}


def _entry(**overrides: object) -> str:
    fields: dict[str, object] = {
        "id": "GHSA-aaaa-bbbb-cccc",
        "package": "urllib3",
        "ecosystem": "python",
        "reason": "not reachable",
        "reviewed": "2026-05-01",
        "expires": "2026-09-01",
    } | overrides
    lines = [f"  {'- ' if i == 0 else '  '}{k}: {v}" for i, (k, v) in enumerate(fields.items())]
    return "\n".join(lines)


def _allowlist(tmp_path: Path, *entries: str) -> Path:
    path = tmp_path / "allowlist.yaml"
    path.write_text("entries:\n" + "\n".join(entries) + "\n" if entries else "entries: []\n")
    return path


def test_pip_audit_report_is_parsed_with_aliases_and_fixes() -> None:
    findings = check.parse_pip_audit(PIP_REPORT)
    assert len(findings) == 1
    assert findings[0].package == "urllib3"
    assert "CVE-2099-0001" in findings[0].ids
    assert findings[0].fixes == ("1.0.1",)


def test_pnpm_audit_report_is_parsed_and_filtered_by_severity() -> None:
    findings = check.parse_pnpm_audit(PNPM_REPORT, "high")
    assert [f.package for f in findings] == ["left-pad"]
    assert findings[0].ids == ("GHSA-dddd-eeee-ffff", "CVE-2099-0002")
    assert findings[0].fixes == ()
    assert findings[0].version == "1.3.0"
    assert len(check.parse_pnpm_audit(PNPM_REPORT, "low")) == 2


def test_allowlisted_finding_passes_as_a_warning(tmp_path: Path) -> None:
    entries = check.load_allowlist(_allowlist(tmp_path, _entry()))
    findings = check.parse_pip_audit(PIP_REPORT)
    failures, warnings = check.evaluate(findings, entries, "python", TODAY)
    assert failures == []
    assert len(warnings) == 1
    assert "not reachable" in warnings[0]


def test_unknown_finding_fails(tmp_path: Path) -> None:
    entries = check.load_allowlist(_allowlist(tmp_path, _entry(id="GHSA-zzzz-zzzz-zzzz")))
    failures, warnings = check.evaluate(check.parse_pip_audit(PIP_REPORT), entries, "python", TODAY)
    assert len(failures) == 1
    assert "not in the allowlist" in failures[0]
    assert warnings == []


def test_entry_for_another_package_or_ecosystem_does_not_match(tmp_path: Path) -> None:
    entries = check.load_allowlist(
        _allowlist(tmp_path, _entry(package="other"), _entry(ecosystem="npm"))
    )
    failures, _ = check.evaluate(check.parse_pip_audit(PIP_REPORT), entries, "python", TODAY)
    assert len(failures) == 1


def test_expired_entry_fails_even_without_a_finding(tmp_path: Path) -> None:
    entries = check.load_allowlist(_allowlist(tmp_path, _entry(expires="2026-05-31")))
    failures, _ = check.evaluate([], entries, "python", TODAY)
    assert len(failures) == 1
    assert "expired" in failures[0]


def test_expired_entry_of_the_other_ecosystem_is_not_reported(tmp_path: Path) -> None:
    entries = check.load_allowlist(
        _allowlist(tmp_path, _entry(ecosystem="npm", expires="2026-05-31"))
    )
    failures, _ = check.evaluate([], entries, "python", TODAY)
    assert failures == []


def test_entry_missing_a_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "allowlist.yaml"
    path.write_text("entries:\n  - id: GHSA-aaaa-bbbb-cccc\n    package: urllib3\n")
    with pytest.raises(ValueError, match="missing"):
        check.load_allowlist(path)


def test_main_reads_both_report_formats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    allowlist = _allowlist(
        tmp_path,
        _entry(expires="2999-01-01"),
        _entry(id="CVE-2099-0002", package="left-pad", ecosystem="npm", expires="2999-01-01"),
    )
    pip_report = tmp_path / "pip-audit.json"
    pip_report.write_text(json.dumps(PIP_REPORT))
    pnpm_report = tmp_path / "pnpm-audit.json"
    pnpm_report.write_text(json.dumps(PNPM_REPORT))

    assert check.main(["python", str(pip_report), "--allowlist", str(allowlist)]) == 0
    assert check.main(["npm", str(pnpm_report), "--allowlist", str(allowlist)]) == 0
    assert capsys.readouterr().out.count("warning:") == 2

    empty = _allowlist(tmp_path)
    assert check.main(["python", str(pip_report), "--allowlist", str(empty)]) == 1
    assert check.main(["npm", str(pnpm_report), "--allowlist", str(empty)]) == 1


def test_main_fails_on_an_unreadable_report(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert check.main(["python", str(missing), "--allowlist", str(_allowlist(tmp_path))]) == 2
