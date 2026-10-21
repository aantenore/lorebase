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
Day 5   agent-setup, agent, limits, audit
Any     jget
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
AGENT_PRINCIPAL = "agent-risk"  # created by bootstrap_admin.py
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
    """A key for the agent: role agent-reader, plus the Operation grant for flag_supplier_risk.

    The Operation is restricted to the subject `agent-risk`; bootstrap_admin.py created the
    service principal of that name, so the key is issued on it.
    """
    with client() as c:
        reader = c.grants.create(DOMAIN, AGENT_PRINCIPAL, "agent-reader")
        writer = c.grants.create(
            DOMAIN, AGENT_PRINCIPAL, "operator", scope_type="operation", scope_ref=AGENT_OPERATION
        )
        created = c.api_keys.create(DOMAIN, args.name, principal_id=AGENT_PRINCIPAL, ttl_days=7)
    key_file = Path(args.key_file)
    key_file.touch(mode=0o600)
    key_file.write_text(created.key, encoding="utf-8")
    say(f"service principal {created.principal_id}, key {created.prefix}... (7 days)")
    say(f"grants: {reader.role} on the Domain, {writer.role} on Operation {writer.scope_ref}")
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

    async def call(tool: str, arguments: dict[str, Any], lines: int = 14) -> str:
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
        return text

    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        say(
            f"Herald over stdio exposes {len(listed.tools)} tools: "
            + ", ".join(sorted(t.name for t in listed.tools))
        )
        say()
        say(f"Question: which active contracts end by {cutoff}, and who is the supplier?")
        await call(
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
        say()
        say("Context pack for the first supplier at risk (no model is called):")
        await call(
            "lorebase_context",
            {"records": [args.supplier], "depth": 1, "token_budget": 1200},
            lines=18,
        )
        say()
        say("Governed write on a work branch, dry run first (the default of lorebase_execute):")
        parameters = {
            "supplier": args.supplier.split("/", 1)[1],
            "severity": "high",
            "reason": "Contract ends within 90 days and the part is single sourced",
        }
        dry = await call(
            "lorebase_execute",
            {
                "operation": AGENT_OPERATION,
                "parameters": parameters,
                "branch": args.branch,
                "dry_run": True,
            },
        )
        found = re.search(r"\bhead ([0-9a-f]{64})", dry)
        say()
        say("Then for real, pinned to the head the dry run reported, safe to retry:")
        await call(
            "lorebase_execute",
            {
                "operation": AGENT_OPERATION,
                "parameters": parameters,
                "branch": args.branch,
                "dry_run": False,
                "expected_head": found.group(1) if found else None,
                "idempotency_key": f"demo-{AGENT_OPERATION}-{parameters['supplier']}",
            },
        )
        say()
        say("A critical flag is a different story: the Operation policy sends it to review.")
        await call(
            "lorebase_execute",
            {
                "operation": AGENT_OPERATION,
                "parameters": {**parameters, "severity": "critical"},
                "branch": args.branch,
                "dry_run": False,
            },
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
    asyncio.run(agent_session(args))


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
        say(f"{LIMIT_PROBES} context packs in a row (this Domain allows a burst of 4 per class):")
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
        say()
        who = http.get(f"{root}/auth/whoami").json()
    quota = who.get("quota") or {}
    say(f"whoami for the agent key: {who['display_name']} ({who['type']}, {who['method']})")
    for name in ("requests", "context_tokens"):
        use = quota.get(name, {})
        say(f"  {name} today: {use.get('used')} of {use.get('limit')}")
    say(f"  quota resets at {quota.get('resets_at')} (UTC midnight)")


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
    add("limits", cmd_limits, ("key_file", {}), ("--supplier", {"default": "Supplier/S-1001"}))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
