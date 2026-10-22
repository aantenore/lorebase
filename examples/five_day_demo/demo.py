#!/usr/bin/env python3
"""Helpers of the five-day demo that are awkward in shell. `run.sh` calls them.

    uv run python examples/five_day_demo/demo.py COMMAND [ARGS]

The Keeper comes from the environment, like for the `lorebase` command line:
LOREBASE_URL, LOREBASE_API_KEY (optional) and LOREBASE_DOMAIN (default `supply`). Commands print
for people on standard output; the few that return a value for the script print only that value.

Day 1   infer, curate
Day 3   risk-model, team-csv, conflicts, resolutions, conflict-id
Day 4   export-suppliers, csv-set, last-run, wait-run, webhook-register, receiver, wait-lines,
        changes
Day 5   agent-setup, agent, limits, audit, agent-proposal
Checks  assert-day1 ... assert-day5, extraction-proposal, assert-items, assert-extraction,
        assert-restore, timings
Any     jget

A failed assertion exits with code 1 (run.sh stops, `make demo` and the CI job fail).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import hmac
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import yaml

from lorebase_client import LorebaseClient
from lorebase_core.model import Schema, dump_schema, load_schema

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("DEMO_DATA_DIR", ROOT / "examples" / "supply" / "data"))
CURATED_SCHEMA = Path(os.environ.get("DEMO_SCHEMA", ROOT / "examples" / "supply" / "schema.yaml"))
DOMAIN = os.environ.get("LOREBASE_DOMAIN", "supply")

# --- The story of day 3: what the two teams change. Edit here to change the demo. ---------------

RISK_RESCORES = {"S-1001": "80", "S-1002": "55"}  # team risk rescores two suppliers ...
RISK_BANDS = (("low", 40), ("medium", 70), ("high", 101))  # ... and bands every score
RISK_NEW_SUPPLIER = {
    "supplier_code": "S-1031",
    "legal_name": "Norvik GmbH",
    "country": "DE",
    "city": "Hamburg",
    "tier": "2",
    "risk_score": "35",
    "status": "active",
    "onboarded_on": "2026-01-10",
    "vat_number": "DE000000031",
}
TIER_FLIPS = ("S-1001", "S-1002", "S-1003", "S-1004")  # team procurement flips tier 1 and 2 ...
TIER_RISK_SCORES = {"S-1001": "45"}  # ... disagrees with team risk on one score ...
TIER_NEW_SUPPLIERS = (  # ... and adds the supplier team risk added too, plus one more
    {
        "supplier_code": "S-1031",
        "legal_name": "Norvik Fasteners Sp. z o.o.",
        "country": "DE",
        "city": "Berlin",
        "tier": "3",
        "risk_score": "50",
        "status": "active",
        "onboarded_on": "2026-01-12",
        "vat_number": "DE000000032",
    },
    {
        "supplier_code": "S-1032",
        "legal_name": "Orlane Gaskets s.r.o.",
        "country": "CZ",
        "city": "Brno",
        "tier": "3",
        "risk_score": "22",
        "status": "active",
        "onboarded_on": "2026-01-12",
        "vat_number": "CZ000000033",
    },
)
RISK_BAND_ATTRIBUTE = {
    "description": "Risk band derived from risk_score.",
    "type": {"enum": ["low", "medium", "high"]},
    "required": True,
    "default": "medium",
}
# Resolutions: the one Conflict decided by hand, and the choice applied to all the others.
HAND_RESOLVED = {"key": "S-1001", "attribute": "risk_score", "value": "75"}
BULK_CHOICE = "ours"

# --- Day 4 -------------------------------------------------------------------------------------

SOURCE_COLUMNS = [
    "supplier_code",
    "legal_name",
    "country",
    "city",
    "tier",
    "risk_score",
    "status",
    "onboarded_on",
    "vat_number",
]

# --- Day 5 -------------------------------------------------------------------------------------

AGENT_OPERATION = "flag_supplier_risk"
AGENT_PRINCIPAL = "agent-risk"  # created by stack.sh (lorebase_keeper principal create)
AGENT_BRANCHES = "agent/*"  # where the agent may branch and open Proposals; main stays out of reach
EXPIRY_WINDOW_DAYS = 90
REFERENCE_DATE = date(2026, 1, 15)  # the date examples/supply is generated for
LIMIT_PROBES = 8


def client(api_key: str | None = None) -> LorebaseClient:
    """A client from the environment, or with another key."""
    if api_key is None:
        return LorebaseClient()
    return LorebaseClient(base_url=os.environ["LOREBASE_URL"], api_key=api_key)


def say(text: str = "") -> None:
    print(text, flush=True)


def table(headers: Sequence[str], rows: Iterable[Sequence[object]], indent: str = "") -> None:
    grid = [[str(c) for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in grid)) for i, h in enumerate(headers)]
    for line in (list(headers), *grid):
        say(indent + "  ".join(c.ljust(w) for c, w in zip(line, widths, strict=True)).rstrip())


def rel(path: str | Path) -> str:
    """A path relative to the repository root when it lies inside it."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def clip(text: object, width: int = 48) -> str:
    if text is None:
        return "(none)"
    value = str(text)
    return value if len(value) <= width else value[: width - 1] + "…"


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, restval="")
        writer.writeheader()
        writer.writerows(rows)


# --- Any ---------------------------------------------------------------------------------------


def cmd_jget(args: argparse.Namespace) -> None:
    """Print one member of a JSON document read from standard input (dotted path)."""
    value: Any = json.load(sys.stdin)
    for part in args.path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    say(value if isinstance(value, str) else json.dumps(value))


# --- Day 1 -------------------------------------------------------------------------------------


def cmd_infer(args: argparse.Namespace) -> None:
    """Propose a schema from the CSV files of the data directory (POST schema:infer)."""
    files = {p.name: p.read_bytes() for p in sorted(DATA_DIR.glob("*.csv"))}
    with client() as c:
        proposal = c.schema.infer_files(DOMAIN, files)
    Path(args.out).write_text(proposal["yaml"], encoding="utf-8")
    document = yaml.safe_load(proposal["yaml"])
    roles = [t["role"] for t in proposal["tables"]]
    say(
        f"read {len(files)} CSV files: {roles.count('kind')} Kinds, "
        f"{roles.count('relation')} Relations, {sum(t['rows'] for t in proposal['tables'])} rows"
    )
    rows = [
        (name, ", ".join(kind.get("primary_key") or ["(minted)"]), len(kind["attributes"]))
        for name, kind in document["kinds"].items()
    ]
    table(("kind", "key", "attributes"), rows, "  ")
    say()
    rows = [
        (
            name,
            f"{rel['from_kind']} -> {rel['to_kind']}",
            rel.get("cardinality", "many_to_many"),
            len(rel.get("attributes") or {}),
        )
        for name, rel in document["relations"].items()
    ]
    table(("relation", "ends", "cardinality", "link attrs"), rows, "  ")
    confidences = sorted(e["confidence"] for e in proposal["evidence"])
    say()
    say(
        f"{len(proposal['evidence'])} decisions, lowest confidence {confidences[0]:.2f}; "
        f"{proposal['issue_count']} issue(s):"
    )
    for issue in proposal["issues"]:
        say(f"  {issue['code']}: {issue['message']}")
    say(f"proposal written to {rel(args.out)} (nothing was changed in the Domain)")


def cmd_curate(args: argparse.Namespace) -> None:
    """Rebase the curated schema on the elements the Domain already has.

    The inferred elements got their own ids; the curated document carries other ids for the
    same api names. Dropping the ids of elements that already exist (Kinds, Relations, Traits,
    their Attributes) lets `load_schema(base=...)` keep the existing ones, so applying the
    curated schema updates the inferred model instead of replacing it.
    """
    with client() as c:
        base = load_schema(c.schema.get_yaml(DOMAIN, branch=args.branch))
    document = yaml.safe_load(CURATED_SCHEMA.read_text(encoding="utf-8"))
    for section in ("kinds", "relations", "traits"):
        existing = {el.api_name: el for el in getattr(base, section).values()}
        for name, element in (document.get(section) or {}).items():
            current = existing.get(name)
            if current is None:
                continue
            element.pop("id", None)
            known = {a.api_name for a in getattr(current, "attributes", {}).values()}
            for attr_name, attribute in (element.get("attributes") or {}).items():
                if attr_name in known:
                    attribute.pop("id", None)
    rebased: Schema = load_schema(yaml.safe_dump(document, sort_keys=False), base=base)
    Path(args.out).write_text(dump_schema(rebased), encoding="utf-8")
    say(f"curated schema, ids of existing elements kept, written to {rel(args.out)}")


# --- Day 3 -------------------------------------------------------------------------------------


def cmd_risk_model(args: argparse.Namespace) -> None:
    """Add the Attribute `risk_band` to Supplier in a schema document (IN to OUT)."""
    document = yaml.safe_load(Path(args.source).read_text(encoding="utf-8"))
    document["kinds"]["Supplier"]["attributes"]["risk_band"] = dict(RISK_BAND_ATTRIBUTE)
    Path(args.out).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    say(f"schema with the new Attribute Supplier.risk_band written to {rel(args.out)}")


def band(score: str) -> str:
    return next(name for name, below in RISK_BANDS if int(score) < below)


def cmd_team_csv(args: argparse.Namespace) -> None:
    """Write the Supplier file one team imports on its branch (an import replaces whole rows)."""
    fields, rows = read_csv(DATA_DIR / "supplier.csv")
    if args.team == "risk":
        for row in rows:
            row["risk_score"] = RISK_RESCORES.get(row["supplier_code"], row["risk_score"])
            row["risk_band"] = band(row["risk_score"])
        rows.append({**RISK_NEW_SUPPLIER, "risk_band": band(RISK_NEW_SUPPLIER["risk_score"])})
        fields = [*fields, "risk_band"]
        say(
            f"team risk: rescored {', '.join(f'{k} to {v}' for k, v in RISK_RESCORES.items())}, "
            f"banded every supplier, added {RISK_NEW_SUPPLIER['supplier_code']}"
        )
    else:
        for row in rows:
            if row["supplier_code"] in TIER_FLIPS:
                row["tier"] = "2" if row["tier"] == "1" else "1"
            row["risk_score"] = TIER_RISK_SCORES.get(row["supplier_code"], row["risk_score"])
        rows.extend(dict(s) for s in TIER_NEW_SUPPLIERS)
        say(
            f"team procurement: flipped the tier of {', '.join(TIER_FLIPS)}, "
            f"S-1001 score {TIER_RISK_SCORES['S-1001']}, added "
            f"{', '.join(s['supplier_code'] for s in TIER_NEW_SUPPLIERS)}"
        )
    write_csv(Path(args.out), fields, rows)


def attribute_names(schema: Schema) -> dict[str, str]:
    return {
        attribute_id: attribute.api_name
        for kind in schema.kinds.values()
        for attribute_id, attribute in kind.attributes.items()
    }


def open_conflicts(proposal: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    with client() as c:
        items = [i.model_dump() for i in c.proposals.iterate_conflicts(DOMAIN, proposal)]
        names = attribute_names(load_schema(c.schema.get_yaml(DOMAIN, branch="main")))
    return items, names


def cmd_conflicts(args: argparse.Namespace) -> None:
    """The Conflicts of a Proposal with Attribute names and the three values."""
    items, names = open_conflicts(args.proposal)
    rows = [
        (
            i["id"][:12],
            i["status"],
            i["kind"],
            i["key"],
            names.get(i.get("attribute") or "", "-"),
            clip(i.get("base"), 24),
            clip(i.get("ours"), 28),
            clip(i.get("theirs"), 28),
        )
        for i in items
    ]
    table(("id", "status", "code", "record", "attribute", "base", "main", "branch"), rows)


def find_conflict(items: list[dict[str, Any]], names: dict[str, str]) -> dict[str, Any]:
    for item in items:
        attribute = names.get(item.get("attribute") or "")
        if item["key"] == HAND_RESOLVED["key"] and attribute == HAND_RESOLVED["attribute"]:
            return item
    raise SystemExit("the Conflict to resolve by hand is not in the Proposal")


def cmd_conflict_id(args: argparse.Namespace) -> None:
    """Print the id of the Conflict the demo resolves by hand."""
    items, names = open_conflicts(args.proposal)
    say(find_conflict(items, names)["id"])


def cmd_resolutions(args: argparse.Namespace) -> None:
    """Write the bulk resolution file: BULK_CHOICE for every Conflict but the one by hand."""
    items, names = open_conflicts(args.proposal)
    by_hand = find_conflict(items, names)["id"]
    plan = [
        {"fingerprint": i["fingerprint"], "choice": BULK_CHOICE}
        for i in items
        if i["id"] != by_hand
    ]
    Path(args.out).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    say(f"{len(plan)} Conflicts take '{BULK_CHOICE}' (keep the Record team risk added)")
    say(f"resolutions written to {rel(args.out)}")


# --- Day 4 -------------------------------------------------------------------------------------


def cmd_export_suppliers(args: argparse.Namespace) -> None:
    """Write the approved-suppliers CSV the Source reads from the Suppliers of `main`."""
    with client() as c:
        records = list(c.records.iterate(DOMAIN, "Supplier", branch="main"))
    rows = [{k: str(r.values.get(k, "")) for k in SOURCE_COLUMNS} for r in records]
    write_csv(Path(args.out), SOURCE_COLUMNS, sorted(rows, key=lambda r: r["supplier_code"]))
    say(f"procurement exported {len(rows)} approved suppliers to {rel(args.out)}")


def cmd_csv_set(args: argparse.Namespace) -> None:
    """Change cells of a CSV: `KEY COLUMN=VALUE ...` (the first column is the key)."""
    path = Path(args.file)
    fields, rows = read_csv(path)
    changes = dict(item.split("=", 1) for item in args.changes)
    for row in rows:
        if row[fields[0]] == args.key:
            for column, value in changes.items():
                say(f"  {args.key}.{column}: {row[column]} -> {value}")
                row[column] = value
            break
    else:
        raise SystemExit(f"no row {args.key} in {path}")
    write_csv(path, fields, rows)


def latest_run(source: str) -> dict[str, Any] | None:
    with client() as c:
        runs = c.sources.runs(DOMAIN, source, limit=1).items
    return runs[0].model_dump(mode="json") if runs else None


def cmd_last_run(args: argparse.Namespace) -> None:
    """Print the id of the newest run of a Source, or `none`."""
    run = latest_run(args.source)
    say(run["id"] if run else "none")


def cmd_wait_run(args: argparse.Namespace) -> None:
    """Wait for a run newer than `--after` to finish and print what it did."""
    deadline = time.monotonic() + args.timeout
    run = None
    while time.monotonic() < deadline:
        run = latest_run(args.source)
        if run and run["id"] != args.after and run["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.5)
    else:
        raise SystemExit(f"no finished run of {args.source} after {args.timeout}s")
    stats = run["stats"]
    counts = stats.get("counts", {})
    say(
        f"run {run['id'][:16]} {run['status']}: read {stats['rows']['read']} rows, "
        f"added {counts.get('added')}, changed {counts.get('changed')}, "
        f"unchanged {counts.get('unchanged')}, removed {counts.get('removed')}"
    )
    if run["status"] == "failed":
        raise SystemExit(f"failed: {(run.get('error') or {}).get('message')}")
    if run.get("proposal_id"):
        say(
            f"proposal {run['proposal_id']} {stats.get('proposal_status')} "
            f"(merge policy result: {stats.get('merge_status')})"
        )
    else:
        say("no changes: no branch commit, no Proposal")


def cmd_webhook_register(args: argparse.Namespace) -> None:
    """Register the webhook of the receiver; the signing secret goes to a file."""
    with client() as c:
        for hook in c.webhooks.list(DOMAIN).items:
            c.webhooks.delete(DOMAIN, hook.id)
        created = c.webhooks.create(
            DOMAIN,
            args.url,
            events=["revision.committed"],
            branches=[args.branch],
            description="five-day demo receiver",
        )
    secret = Path(args.secret_file)
    secret.touch(mode=0o600)
    secret.write_text(created.secret, encoding="utf-8")
    say(f"webhook {created.id}: {created.events} on {created.branches} -> {created.url}")


def signature_ok(secret: str, header: str, body: bytes, tolerance_s: int = 300) -> bool:
    """The check of docs/agents.md: HMAC-SHA256 over `<t>.<body>` keyed with sha256(secret)."""
    parts = dict(item.split("=", 1) for item in header.split(","))
    if abs(time.time() - int(parts["t"])) > tolerance_s:
        return False
    key = hashlib.sha256(secret.encode()).digest()
    expected = hmac.new(key, f"{parts['t']}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])


def receiver_handler(secret_file: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                secret = secret_file.read_text(encoding="utf-8")
                valid = signature_ok(secret, self.headers.get("Lorebase-Signature", ""), body)
            except (OSError, KeyError, ValueError):
                valid = False
            if not valid:
                self.send_response(401)
                self.end_headers()
                say("delivery rejected: bad signature")
                return
            event = json.loads(body)
            kinds = ",".join(f"{k}:{n}" for k, n in event.get("kinds", {}).items())
            say(
                f"delivery {self.headers.get('Lorebase-Delivery', '?')[:12]} "
                f"{self.headers.get('Lorebase-Event')} signature=ok "
                f"branch={event['branch']} revision={event['revision'][:12]} "
                f"author={event['author']} kinds={kinds or '-'} message={event['message']!r}"
            )
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_: object) -> None:  # one line per delivery is printed above
            return

    return Handler


def cmd_receiver(args: argparse.Namespace) -> None:
    """A tiny webhook receiver: verify the signature of each delivery and print it."""
    server = ThreadingHTTPServer((args.bind, args.port), receiver_handler(Path(args.secret_file)))
    say(f"receiver listening on {args.bind}:{args.port}")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


def cmd_wait_lines(args: argparse.Namespace) -> None:
    """Wait until a log file has at least N lines containing TEXT, then print them."""
    deadline = time.monotonic() + args.timeout
    path = Path(args.file)
    while True:
        lines = [x for x in path.read_text(encoding="utf-8").splitlines() if args.text in x]
        if len(lines) >= args.count:
            for line in lines:
                say(line)
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"only {len(lines)} of {args.count} deliveries after {args.timeout}s")
        time.sleep(0.5)


def cmd_changes(args: argparse.Namespace) -> None:
    """The change feed of a branch after a Revision (GET branches/{b}/changes)."""
    with client() as c:
        feed = c.agents.changes(DOMAIN, args.branch, since=args.since)
    rows = [
        (
            e.generation,
            e.revision[:12],
            e.author,
            ",".join(f"{k}:{n}" for k, n in e.kinds.items()) or "-",
            clip(e.message, 44),
        )
        for e in feed.items
    ]
    table(("gen", "revision", "author", "kinds", "message"), rows)
    say(f"head {feed.head[:12]}, next_since {feed.next_since[:12]}, more={feed.more}")


# --- Day 5 -------------------------------------------------------------------------------------


def cmd_agent_setup(args: argparse.Namespace) -> None:
    """A key for the agent: role agent-reader, the Operation grant for flag_supplier_risk and
    the editor role on the branches `agent/*` (its task branches and their Proposals).

    The Operation is restricted to the subject `agent-risk`; stack.sh created the service
    principal of that name (`python -m lorebase_keeper principal create`), so the key is issued
    on it.
    """
    with client() as c:
        reader = c.grants.create(DOMAIN, AGENT_PRINCIPAL, "agent-reader")
        writer = c.grants.create(
            DOMAIN, AGENT_PRINCIPAL, "operator", scope_type="operation", scope_ref=AGENT_OPERATION
        )
        branches = c.grants.create(
            DOMAIN, AGENT_PRINCIPAL, "editor", scope_type="branch", scope_ref=AGENT_BRANCHES
        )
        created = c.api_keys.create(DOMAIN, args.name, principal_id=AGENT_PRINCIPAL, ttl_days=7)
    key_file = Path(args.key_file)
    key_file.touch(mode=0o600)
    key_file.write_text(created.key, encoding="utf-8")
    say(f"service principal {created.principal_id}, key {created.prefix}... (7 days)")
    say(f"grants: {reader.role} on the Domain, {writer.role} on Operation {writer.scope_ref},")
    say(f"        {branches.role} on branches {branches.scope_ref}")
    say(f"the secret is in {rel(args.key_file)} and is not printed")


def text_of(result: Any) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def agent_session(args: argparse.Namespace) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    key = args.key
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "LOREBASE_URL": os.environ["LOREBASE_URL"],
        "LOREBASE_API_KEY": key,
        "LOREBASE_DOMAIN": DOMAIN,
        "LOREBASE__LOGGING__LEVEL": "WARNING",
    }
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "lorebase_herald"], env=environment
    )
    cutoff = REFERENCE_DATE + timedelta(days=EXPIRY_WINDOW_DAYS)

    async def call(tool: str, arguments: dict[str, Any], lines: int = 14) -> tuple[str, bool]:
        say(f"> {tool} {json.dumps(arguments, separators=(',', ':'))}")
        started = time.perf_counter()
        result = await session.call_tool(tool, arguments)
        elapsed = (time.perf_counter() - started) * 1000
        text = text_of(result)
        shown = text.splitlines()
        for line in shown[:lines]:
            say("  " + clip(line, 140))
        if len(shown) > lines:
            say(f"  ... {len(shown) - lines} more lines")
        outcome = "error" if result.is_error else "ok"
        say(f"  [{outcome}, {elapsed:.0f} ms through Herald and Keeper]")
        return text, bool(result.is_error)

    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        names = sorted(t.name for t in listed.tools)
        say(f"Herald over stdio exposes {len(names)} tools: " + ", ".join(names))
        check(
            {"lorebase_query", "lorebase_context", "lorebase_execute"} <= set(names),
            "Herald exposes the query, context and execute tools",
        )
        say()
        say(f"Question: which active contracts end by {cutoff}, and who is the supplier?")
        found, failed = await call(
            "lorebase_query",
            {
                "query": {
                    "kind": "Contract",
                    "where": {
                        "and": [
                            {"attr": "end_date", "op": "lte", "value": cutoff.isoformat()},
                            {"attr": "status", "op": "eq", "value": "active"},
                        ]
                    },
                    "include": {"with_supplier": {}},
                    "limit": 3,
                }
            },
        )
        rows = re.search(r"\b(\d+) rows", found)
        check(not failed and rows is not None and int(rows[1]) > 0, "the query returns rows")
        check("with_supplier: Supplier/" in found, "each Contract comes with its supplier")
        say()
        say("Context pack for the first supplier at risk (no model is called):")
        pack, failed = await call(
            "lorebase_context",
            {"records": [args.supplier], "depth": 1, "token_budget": 1200},
            lines=18,
        )
        check(
            not failed and f"### {args.supplier}" in pack, "the context pack describes the Record"
        )
        say()
        say("Governed write on a work branch, dry run first (the default of lorebase_execute):")
        parameters = {
            "supplier": args.supplier.split("/", 1)[1],
            "severity": "high",
            "reason": "Contract ends within 90 days and the part is single sourced",
        }
        dry, failed = await call(
            "lorebase_execute",
            {
                "operation": AGENT_OPERATION,
                "parameters": parameters,
                "branch": args.branch,
                "dry_run": True,
            },
        )
        head = re.search(r"\bhead ([0-9a-f]{64})", dry)
        check(not failed and head is not None and "dry_run" in dry, "the dry run reports the head")
        check("0 violations" in dry, "the dry run finds no violation")
        say()
        say("Then for real, pinned to the head the dry run reported, safe to retry:")
        done, failed = await call(
            "lorebase_execute",
            {
                "operation": AGENT_OPERATION,
                "parameters": parameters,
                "branch": args.branch,
                "dry_run": False,
                "expected_head": head.group(1) if head else None,
                "idempotency_key": f"demo-{AGENT_OPERATION}-{parameters['supplier']}",
            },
        )
        check(not failed and done.startswith("committed"), "the flag is committed on the branch")
        say()
        say("A critical flag is a different story: the Operation policy sends it to review.")
        refused, failed = await call(
            "lorebase_execute",
            {
                "operation": AGENT_OPERATION,
                "parameters": {**parameters, "severity": "critical"},
                "branch": args.branch,
                "dry_run": False,
            },
        )
        check(failed and "review-required" in refused, "a critical flag is refused: review needed")
        say()
        say("The agent asks for a Proposal instead: a task name and its intent.")
        proposed, failed = await call(
            "lorebase_execute",
            {
                "operation": AGENT_OPERATION,
                "parameters": {**parameters, "severity": "critical"},
                "dry_run": False,
                "task": "critical-flag-s-1001",
                "intent": "S-1001 is single sourced and its agreement ends soon",
            },
        )
        check(
            not failed and re.search(r"\bprp_[0-9A-Z]{26}\b", proposed) is not None,
            "Keeper ran it on the agent's task branch and opened a Proposal",
        )


def cmd_audit(args: argparse.Namespace) -> None:
    """The newest entries of the audit log of the Domain."""
    with client() as c:
        entries = c.audit.list(DOMAIN, limit=args.limit).items
    rows = [
        (e.seq, f"{e.at:%H:%M:%S}", e.actor, e.action, clip(e.target or "-", 30)) for e in entries
    ]
    table(("seq", "at", "actor", "action", "target"), rows)


def cmd_agent(args: argparse.Namespace) -> None:
    """Run Herald over stdio with a scripted MCP client (the `mcp` SDK)."""
    args.key = Path(args.key_file).read_text(encoding="utf-8").strip()
    try:
        asyncio.run(agent_session(args))
    except BaseExceptionGroup as group:  # the MCP task group wraps a failed assertion
        stops = [e for e in group.exceptions if isinstance(e, SystemExit)]
        if stops:
            raise stops[0] from None
        raise


def cmd_limits(args: argparse.Namespace) -> None:
    """Rate-limit headers, a refused request and the quota of the agent's key."""
    key = Path(args.key_file).read_text(encoding="utf-8").strip()
    root = os.environ["LOREBASE_URL"].rstrip("/") + "/api/v1"
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(headers=headers, timeout=30) as http:
        response = http.get(f"{root}/domains/{DOMAIN}/kinds/Supplier/records", params={"limit": 1})
        say(f"GET records -> {response.status_code}")
        for name in (
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "RateLimit-Reset",
            "Lorebase-Revision",
        ):
            say(f"  {name}: {response.headers.get(name, '-')[:16]}")
        say()
        check(
            response.status_code == 200 and "RateLimit-Remaining" in response.headers,
            "a read answers 200 with the rate-limit headers",
        )
        say()
        say(f"{LIMIT_PROBES} context packs in a row (this Domain allows a burst of 4 per class):")
        statuses: list[int] = []
        retry_after = None
        for number in range(1, LIMIT_PROBES + 1):
            response = http.post(
                f"{root}/domains/{DOMAIN}/context", json={"refs": [args.supplier], "depth": 0}
            )
            detail = f"remaining {response.headers.get('RateLimit-Remaining', '-')}"
            if response.status_code == 429:
                body = response.json()
                detail = (
                    f"{body.get('type', '').rsplit(':', 1)[-1]}, Retry-After "
                    f"{response.headers.get('Retry-After')} s, class {body.get('class')}"
                )
            say(f"  {number}: {response.status_code} {detail}")
            statuses.append(response.status_code)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
        check(statuses[0] == 200 and 429 in statuses, "the burst is served, then refused", statuses)
        check(retry_after is not None, "a refused request says when to retry (Retry-After)")
        say()
        who = http.get(f"{root}/auth/whoami").json()
    quota = who.get("quota") or {}
    say(f"whoami for the agent key: {who['display_name']} ({who['type']}, {who['method']})")
    for name in ("requests", "context_tokens"):
        use = quota.get(name, {})
        say(f"  {name} today: {use.get('used')} of {use.get('limit')}")
    say(f"  quota resets at {quota.get('resets_at')} (UTC midnight)")
    check(
        who["type"] == "service" and quota.get("requests", {}).get("used", 0) > 0,
        "whoami shows the agent as a service principal with its quota in use",
    )


# --- Assertions and timings --------------------------------------------------------------------
#
# run.sh calls `assert-*` after each day. A failed assertion names what was expected and exits
# with code 1, so the run fails (the CI workflow relies on it).

RECORD_FILES = {
    "Supplier": "supplier.csv",
    "Part": "part.csv",
    "Plant": "plant.csv",
    "Contract": "contract.csv",
    "PurchaseOrder": "purchase_order.csv",
    "RiskFlag": "risk_flag.csv",
}
LINK_FILES = {
    "supplied_by": "supplied_by.csv",
    "stocked_at": "stocked_at.csv",
    "with_supplier": "with_supplier.csv",
    "for_part": "for_part.csv",
    "delivered_to": "delivered_to.csv",
    "flags": "flags.csv",
}
# The contract in documents/, as the rules of extraction tier 0 find it.
EXTRACTED_CONTRACT = {"key": "C-3099", "supplier": "S-1013", "incoterm": "DAP", "currency": "EUR"}


def check(condition: bool, label: str, detail: object = "") -> None:
    """Print a passed assertion, or stop the run with exit code 1."""
    if not condition:
        raise SystemExit(
            f"assertion failed: {label}" + (f" (got {detail})" if detail != "" else "")
        )
    say(f"  check ok: {label}")


def api_names(items: dict[str, Any]) -> list[str]:
    return sorted(item.api_name for item in items.values())


def merged_proposals(c: LorebaseClient, **filters: Any) -> list[Any]:
    return list(c.proposals.iterate(DOMAIN, status="merged", **filters))


def count_rows(name: str) -> int:
    return len(read_csv(DATA_DIR / name)[1])


def cmd_assert_day1(args: argparse.Namespace) -> None:
    """Day 1: main holds the curated schema, got it through a Proposal, and is protected."""
    expected = load_schema(CURATED_SCHEMA.read_text(encoding="utf-8"))
    with client() as c:
        main = load_schema(c.schema.get_yaml(DOMAIN, branch="main"))
        branch = c.branches.get(DOMAIN, "main")
        proposals = merged_proposals(c, target="main")
    for section in ("kinds", "relations", "traits", "operations", "derivations", "sources"):
        got, want = api_names(getattr(main, section)), api_names(getattr(expected, section))
        check(got == want, f"schema of main: {len(want)} {section}", got)
    check(branch.protected, "main is protected")
    check(
        any(p.source_ref == "model/initial" for p in proposals),
        "the schema reached main through the merged Proposal of model/initial",
    )


def cmd_assert_day2(args: argparse.Namespace) -> None:
    """Day 2: every CSV row is a Record or a Link on main; derived values, query and search work."""
    with client() as c:
        for kind, name in RECORD_FILES.items():
            got = sum(1 for _ in c.records.iterate(DOMAIN, kind, branch="main"))
            check(got == count_rows(name), f"{kind}: {count_rows(name)} Records imported", got)
        for relation, name in LINK_FILES.items():
            got = sum(1 for _ in c.records.iterate_relation_links(DOMAIN, relation, branch="main"))
            check(got == count_rows(name), f"{relation}: {count_rows(name)} Links imported", got)
        parts = c.records.query(
            DOMAIN,
            {
                "kind": "Part",
                "where": {"attr": "single_sourced", "op": "eq", "value": True},
                "aggregate": {"group_by": "category"},
            },
        )
        total = sum(row.count for row in parts.aggregate.counts) if parts.aggregate else 0
        check(total > 0, "the derived value single_sourced answers a query", total)
        hits = c.records.search(DOMAIN, {"q": "bearing", "kinds": ["Part"], "limit": 3}).items
        check(len(hits) > 0, "lexical search finds Parts for 'bearing'", len(hits))
        loaded = [p for p in merged_proposals(c, target="main") if p.source_ref == "data/initial"]
        check(len(loaded) == 1, "the data reached main through the Proposal of data/initial")


def cmd_extraction_proposal(args: argparse.Namespace) -> None:
    """Wait for the Proposal Scribe opens for an uploaded document and print its id."""
    deadline = time.monotonic() + args.timeout
    with client() as c:
        while time.monotonic() < deadline:
            for proposal in c.proposals.iterate(DOMAIN, origin="extraction", status="open"):
                if args.filename in proposal.title:
                    say(proposal.id)
                    return
            time.sleep(0.5)
    raise SystemExit(f"no extraction Proposal for {args.filename} after {args.timeout}s")


def cmd_assert_items(args: argparse.Namespace) -> None:
    """The items extracted from the contract: the new Contract and its Link, found by rules."""
    with client() as c:
        items = list(c.proposals.iterate_items(DOMAIN, args.proposal))
    check(len(items) == 2, "two items are proposed: the Contract and its Link", len(items))
    by_type = {i.type: i for i in items}
    contract, link = by_type.get("record"), by_type.get("link")
    check(
        contract is not None and (contract.kind, contract.key) == ("Contract", "C-3099"),
        "item 1 is the new Contract C-3099",
    )
    check(link is not None and link.relation == "with_supplier", "item 2 is a with_supplier Link")
    check(all(i.decision == "pending" for i in items), "both items wait for a decision")
    check(all(i.tier == 0 for i in items), "both come from tier 0 (rules, no model)")
    check(
        all(i.confidence is not None and 0 < i.confidence < 1 for i in items),
        "both carry a confidence below the auto-accept threshold",
    )


def cmd_assert_extraction(args: argparse.Namespace) -> None:
    """After the merge: the Contract and its Link are on main with extraction lineage."""
    want = EXTRACTED_CONTRACT
    with client() as c:
        found = c.records.get(DOMAIN, "Contract", want["key"], branch="main", lineage=True).record
        links = list(
            c.records.iterate_relation_links(
                DOMAIN, "with_supplier", branch="main", from_key=want["key"]
            )
        )
        proposal = c.proposals.get(DOMAIN, args.proposal)
    check(found.values.get("incoterm") == want["incoterm"], "Contract C-3099 has incoterm DAP")
    check(found.values.get("currency") == want["currency"], "Contract C-3099 has currency EUR")
    targets = [link.to_key for link in links]
    check(targets == [want["supplier"]], "C-3099 is linked to supplier S-1013", targets)
    origins = {x.origin_type for x in (found.lineage or {}).values()}
    check("extraction" in origins, "the values of C-3099 have extraction lineage", origins)
    check(proposal.status == "merged", "the extraction Proposal is merged", proposal.status)
    check(proposal.origin == "extraction", "the Proposal has origin extraction", proposal.origin)


def cmd_assert_day3(args: argparse.Namespace) -> None:
    """Day 3: both teams' changes are on main, Conflicts are resolved, history is intact."""
    original = {row["supplier_code"]: row for row in read_csv(DATA_DIR / "supplier.csv")[1]}
    added = [RISK_NEW_SUPPLIER["supplier_code"], *(s["supplier_code"] for s in TIER_NEW_SUPPLIERS)]
    checked = ["S-1001", "S-1003", *added]
    with client() as c:
        proposal = c.proposals.get(DOMAIN, args.proposal)
        conflicts = list(c.proposals.iterate_conflicts(DOMAIN, args.proposal))
        sources = {p.source_ref for p in merged_proposals(c, target="main")}
        now = {
            key: c.records.get(DOMAIN, "Supplier", key, branch="main").record.values
            for key in checked
        }
        before = c.records.get(DOMAIN, "Supplier", "S-1001", at=args.day2_head).record.values
    check(proposal.status == "merged", "the Proposal of cleanup/tiers is merged", proposal.status)
    check({"model/risk", "cleanup/tiers"} <= sources, "both teams' branches reached main")
    check(len(conflicts) > 0, "the merge raised Conflicts", len(conflicts))
    states = sorted({str(x.status) for x in conflicts})
    check("open" not in states, "no Conflict is left open", states)
    s1001 = now["S-1001"]
    check(
        str(s1001.get("risk_score")) == HAND_RESOLVED["value"],
        f"S-1001 risk_score is {HAND_RESOLVED['value']}, the value decided by hand",
        s1001.get("risk_score"),
    )
    check(s1001.get("risk_band") == band(HAND_RESOLVED["value"]), "S-1001 has the risk band")
    s1003, was = now["S-1003"], original["S-1003"]
    flipped = "2" if was["tier"] == "1" else "1"
    check(str(s1003.get("tier")) == flipped, "S-1003 has the tier team procurement set")
    check(
        s1003.get("risk_band") == band(was["risk_score"]),
        "S-1003 has the risk band team risk added, with no Conflict",
        s1003.get("risk_band"),
    )
    for code in (
        RISK_NEW_SUPPLIER["supplier_code"],
        *(s["supplier_code"] for s in TIER_NEW_SUPPLIERS),
    ):
        check(bool(now[code]), f"supplier {code}, added on a branch, is on main")
    check(
        str(before.get("risk_score")) == original["S-1001"]["risk_score"]
        and "risk_band" not in before,
        "time travel: S-1001 at the day 2 head has its day 2 score and no risk_band",
        before.get("risk_score"),
    )


def cmd_assert_day4(args: argparse.Namespace) -> None:
    """Day 4: the Source synced its changes through a Proposal that merged itself."""
    with client() as c:
        runs = c.sources.runs(DOMAIN, "approved.suppliers", limit=5).items
        proposals = merged_proposals(c, source="source/approved.suppliers")
        city = c.records.get(DOMAIN, "Supplier", "S-1005", branch="main").record.values
        tier = c.records.get(DOMAIN, "Supplier", "S-1008", branch="main").record.values
        feed = c.agents.changes(DOMAIN, "main", since=args.since)
    check(len(runs) >= 2, "the Source ran twice", len(runs))
    check(all(r.status == "succeeded" for r in runs), "every run succeeded")
    counts = runs[0].stats.get("counts", {})
    check(counts.get("changed") == 2, "the second run found the two changed rows", counts)
    check(len(proposals) == 1, "that run's Proposal merged by the merge policy", len(proposals))
    check(city.get("city") == "Madrid", "S-1005 is in Madrid on main", city.get("city"))
    check(str(tier.get("tier")) == "1", "S-1008 has tier 1 on main", tier.get("tier"))
    check(
        any("Supplier" in e.kinds for e in feed.items),
        "the change feed lists the Supplier changes",
        len(feed.items),
    )


def cmd_agent_proposal(args: argparse.Namespace) -> None:
    """Print the id of the one open Proposal an agent raised (the review-required write)."""
    with client() as c:
        found = [p for p in c.proposals.iterate(DOMAIN, status="open") if p.author == args.author]
    if len(found) != 1:
        raise SystemExit(f"expected one open Proposal by {args.author}, found {len(found)}")
    say(found[0].id)


def cmd_assert_day5(args: argparse.Namespace) -> None:
    """Day 5: the agent's writes reached main only through merged Proposals, and are audited."""
    key = Path(args.key_file).read_text(encoding="utf-8").strip()
    with client() as c:
        flags = list(c.records.iterate(DOMAIN, "RiskFlag", branch="main"))
        merged = merged_proposals(c, target="main")
        actions = {e.action for e in c.audit.iterate(DOMAIN)}
        open_proposals = [p.title for p in c.proposals.iterate(DOMAIN, status="open")]
        head = c.branches.get(DOMAIN, "main").head
    existing = count_rows("risk_flag.csv")
    check(
        len(flags) == existing + 2,
        f"main holds the {existing} flags of day 2 and the 2 the agent raised",
        len(flags),
    )
    raised = [f for f in flags if f.values.get("raised_by") == AGENT_PRINCIPAL]
    check(len(raised) == 2, "both new flags are marked raised_by the agent", len(raised))
    branches = {p.source_ref for p in merged}
    check("agent/flags" in branches, "the flag on agent/flags reached main through a Proposal")
    check(
        any(b.startswith(f"agent/{AGENT_PRINCIPAL}/") for b in branches),
        "the agent's own Proposal, from its task branch, was reviewed and merged",
    )
    check(not open_proposals, "no Proposal is left open", open_proposals)
    check("operation.execute" in actions, "the audit log has the agent's operation.execute")
    check(
        {"proposal.create", "proposal.review", "proposal.merge"} <= actions,
        "the audit log has Proposal create, review and merge",
    )
    refused = httpx.post(
        os.environ["LOREBASE_URL"].rstrip("/")
        + f"/api/v1/domains/{DOMAIN}/operations/{AGENT_OPERATION}/execute",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "branch": "main",
            "dry_run": False,
            "parameters": {"supplier": "S-1002", "severity": "high", "reason": "direct write"},
        },
        timeout=30,
    )
    check(
        refused.status_code in {403, 409},
        "a direct write by the agent to main is refused",
        refused.status_code,
    )
    with client() as c:
        check(c.branches.get(DOMAIN, "main").head == head, "main did not move")


def cmd_assert_restore(args: argparse.Namespace) -> None:
    """The restored database has the head and the Record count of the live one."""
    live, restored = args.live.split(), args.restored.split()
    check(len(live) == 3 and len(restored) == 3, "both summaries have head, Revisions, Records")
    check(live[0] == restored[0], "the restored head of main is the live head", restored[0][:12])
    check(live[2] == restored[2], "the restored Record count is the live count", restored[2])


def cmd_timings(args: argparse.Namespace) -> None:
    """Print the seconds each day took, from a file of `LABEL<TAB>SECONDS` lines."""
    path = Path(args.file)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    rows = [line.split("\t") for line in lines if line]
    total = sum(int(seconds) for _, seconds in rows)
    say()
    say("Timing per day")
    table(("day", "seconds"), [*rows, ("total", total)], indent="  ")
    if args.status != 0:
        say(f"The run FAILED (exit code {args.status}) during: {args.during or 'preparation'}")


# --- Command line ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(
        name: str,
        func: Callable[[argparse.Namespace], None],
        *arguments: tuple[str, dict[str, Any]],
    ):
        command = sub.add_parser(name, help=func.__doc__)
        for flag, options in arguments:
            command.add_argument(flag, **options)
        command.set_defaults(func=func)

    add("jget", cmd_jget, ("path", {}))
    add("infer", cmd_infer, ("out", {}))
    add("curate", cmd_curate, ("out", {}), ("--branch", {"default": "main"}))
    add("risk-model", cmd_risk_model, ("source", {}), ("out", {}))
    add("team-csv", cmd_team_csv, ("team", {"choices": ["risk", "tiers"]}), ("out", {}))
    add("conflicts", cmd_conflicts, ("proposal", {}))
    add("conflict-id", cmd_conflict_id, ("proposal", {}))
    add("resolutions", cmd_resolutions, ("proposal", {}), ("out", {}))
    add("export-suppliers", cmd_export_suppliers, ("out", {}))
    add("csv-set", cmd_csv_set, ("file", {}), ("key", {}), ("changes", {"nargs": "+"}))
    add("last-run", cmd_last_run, ("source", {}))
    add(
        "wait-run",
        cmd_wait_run,
        ("source", {}),
        ("--after", {"default": "none"}),
        ("--timeout", {"type": float, "default": 60.0}),
    )
    add(
        "webhook-register",
        cmd_webhook_register,
        ("url", {}),
        ("secret_file", {}),
        ("--branch", {"default": "main"}),
    )
    add(
        "receiver",
        cmd_receiver,
        ("--port", {"type": int, "required": True}),
        ("--bind", {"default": "127.0.0.1"}),
        ("--secret-file", {"required": True}),
    )
    add(
        "wait-lines",
        cmd_wait_lines,
        ("file", {}),
        ("text", {}),
        ("--count", {"type": int, "default": 1}),
        ("--timeout", {"type": float, "default": 30.0}),
    )
    add("changes", cmd_changes, ("branch", {}), ("--since", {"default": None}))
    add("agent-setup", cmd_agent_setup, ("name", {}), ("key_file", {}))
    add(
        "agent",
        cmd_agent,
        ("key_file", {}),
        ("--supplier", {"default": "Supplier/S-1001"}),
        ("--branch", {"default": "agent/flags"}),
    )
    add("audit", cmd_audit, ("--limit", {"type": int, "default": 8}))
    add("agent-proposal", cmd_agent_proposal, ("--author", {"default": AGENT_PRINCIPAL}))
    add("assert-day1", cmd_assert_day1)
    add("assert-day2", cmd_assert_day2)
    add(
        "extraction-proposal",
        cmd_extraction_proposal,
        ("filename", {}),
        ("--timeout", {"type": float, "default": 60.0}),
    )
    add("assert-items", cmd_assert_items, ("proposal", {}))
    add("assert-extraction", cmd_assert_extraction, ("proposal", {}))
    add("assert-day3", cmd_assert_day3, ("proposal", {}), ("day2_head", {}))
    add("assert-day4", cmd_assert_day4, ("--since", {"required": True}))
    add("assert-day5", cmd_assert_day5, ("key_file", {}))
    add("assert-restore", cmd_assert_restore, ("live", {}), ("restored", {}))
    add(
        "timings",
        cmd_timings,
        ("file", {}),
        ("--status", {"type": int, "default": 0}),
        ("--during", {"default": ""}),
    )
    add("limits", cmd_limits, ("key_file", {}), ("--supplier", {"default": "Supplier/S-1001"}))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
