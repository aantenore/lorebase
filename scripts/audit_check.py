#!/usr/bin/env python3
"""Dependency advisory gate (docs/release.md, CONTRIBUTING.md).

    audit_check.py python build/audit/pip-audit.json    pip-audit JSON report
    audit_check.py npm build/audit/pnpm-audit.json      `pnpm audit --json` report
                   [--min-severity high]

Every advisory in the report must be listed in the allowlist (`.github/audit-allowlist.yaml`
by default) for its package and ecosystem, or the check fails. An allowlisted advisory is
printed as a warning. An allowlist entry of the checked ecosystem whose `expires` date has
passed also fails the check, so an accepted risk is looked at again before the date. An entry
matches on the advisory id or any of its aliases (GHSA, CVE, PYSEC).

Each entry has: id, package, ecosystem (python or npm), reason, reviewed (date) and expires (date).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = ROOT / ".github" / "audit-allowlist.yaml"
ECOSYSTEMS = ("python", "npm")
SEVERITIES = ("info", "low", "moderate", "high", "critical")
ENTRY_FIELDS = ("id", "package", "ecosystem", "reason", "reviewed", "expires")
NO_FIX = ("", "<0.0.0")


@dataclass(frozen=True)
class Finding:
    package: str
    version: str
    ids: tuple[str, ...]
    fixes: tuple[str, ...]

    def label(self) -> str:
        fix = ", ".join(self.fixes) or "none yet"
        return f"{self.package} {self.version} {self.ids[0]} (fix: {fix})"


@dataclass(frozen=True)
class Entry:
    id: str
    package: str
    ecosystem: str
    reason: str
    reviewed: dt.date
    expires: dt.date

    def label(self) -> str:
        return f"{self.id} for {self.package} ({self.ecosystem})"


def _date(value: Any, field: str, where: str) -> dt.date:
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise ValueError(f"{where}: {field} must be a date (YYYY-MM-DD)")
    return value


def load_allowlist(path: Path) -> list[Entry]:
    """The entries of the allowlist file; a malformed entry raises ValueError."""
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = document.get("entries") or []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: entries must be a list")
    entries = []
    for index, item in enumerate(raw, start=1):
        where = f"{path} entry {index}"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be a mapping")
        missing = [f for f in ENTRY_FIELDS if not item.get(f)]
        if missing:
            raise ValueError(f"{where}: missing {', '.join(missing)}")
        if item["ecosystem"] not in ECOSYSTEMS:
            raise ValueError(f"{where}: ecosystem must be one of {', '.join(ECOSYSTEMS)}")
        reviewed = _date(item["reviewed"], "reviewed", where)
        expires = _date(item["expires"], "expires", where)
        if expires < reviewed:
            raise ValueError(f"{where}: expires is before reviewed")
        entries.append(
            Entry(
                id=str(item["id"]),
                package=str(item["package"]),
                ecosystem=item["ecosystem"],
                reason=str(item["reason"]),
                reviewed=reviewed,
                expires=expires,
            )
        )
    return entries


def parse_pip_audit(report: dict[str, Any]) -> list[Finding]:
    findings = []
    for dep in report.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            ids = (vuln["id"], *vuln.get("aliases", []))
            fixes = tuple(vuln.get("fix_versions", []))
            findings.append(Finding(dep["name"], dep["version"], ids, fixes))
    return findings


def parse_pnpm_audit(report: dict[str, Any], min_severity: str) -> list[Finding]:
    threshold = SEVERITIES.index(min_severity)
    findings = []
    for advisory in report.get("advisories", {}).values():
        if SEVERITIES.index(advisory.get("severity", "info")) < threshold:
            continue
        ids = (advisory["github_advisory_id"], *advisory.get("cves", []))
        patched = advisory.get("patched_versions") or ""
        fixes = () if patched in NO_FIX else (patched,)
        version = ", ".join(sorted({f["version"] for f in advisory.get("findings", [])}))
        findings.append(Finding(advisory["module_name"], version or "?", ids, fixes))
    return findings


def evaluate(
    findings: list[Finding], entries: list[Entry], ecosystem: str, today: dt.date
) -> tuple[list[str], list[str]]:
    """The failures and the warnings for the findings of one ecosystem."""
    scoped = [e for e in entries if e.ecosystem == ecosystem]
    failures = [
        f"allowlist entry {e.label()} expired on {e.expires}" for e in scoped if e.expires < today
    ]
    warnings = []
    for finding in findings:
        match = next(
            (e for e in scoped if e.package == finding.package and e.id in finding.ids),
            None,
        )
        if match is None:
            failures.append(f"advisory not in the allowlist: {finding.label()}")
        elif match.expires >= today:
            warnings.append(
                f"allowlisted until {match.expires}: {finding.label()} - {match.reason}"
            )
    return failures, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on advisories that are not allowlisted.")
    parser.add_argument("ecosystem", choices=ECOSYSTEMS)
    parser.add_argument("report", type=Path, help="audit report in JSON")
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--min-severity", choices=SEVERITIES, default="high", help="npm only")
    args = parser.parse_args(argv)

    try:
        entries = load_allowlist(args.allowlist)
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.ecosystem == "python":
        findings = parse_pip_audit(report)
    else:
        findings = parse_pnpm_audit(report, args.min_severity)

    today = dt.datetime.now(dt.UTC).date()
    failures, warnings = evaluate(findings, entries, args.ecosystem, today)
    for line in warnings:
        print(f"warning: {line}")
    for line in failures:
        print(f"error: {line}", file=sys.stderr)
    if not failures:
        detail = f"{len(findings)} advisories, all allowlisted" if findings else "no advisories"
        print(f"{args.ecosystem}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
