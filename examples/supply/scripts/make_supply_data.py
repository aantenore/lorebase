#!/usr/bin/env python3
"""Generate the demo ``supply`` dataset as CSV files, deterministically.

One CSV per Kind (``supplier.csv``, ``part.csv`` ...) and one per explicit Relation
(``supplied_by.csv`` ...). Record files use the Attribute api names of ``schema.yaml`` as
header; Link files start with ``from_key,to_key`` followed by the link Attributes. The Relation
``issued_to`` is backed by the ``supplier`` Attribute of ``purchase_order.csv``, so it has no
file of its own.

The default output (``--scale 1``) is the committed ``data/`` directory. Larger scales multiply
every row count and give a bigger dataset with the same shape, for load and performance runs.

    python examples/supply/scripts/make_supply_data.py                    # rewrite data/
    python examples/supply/scripts/make_supply_data.py --scale 200 --out /tmp/supply-big

All names are invented; any resemblance to real companies is a coincidence.
"""

from __future__ import annotations

import argparse
import csv
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

DEFAULT_SEED = 20260115
DEFAULT_AS_OF = "2026-01-15"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data"

#: Row counts at scale 1; every count is multiplied by ``--scale``.
BASE_COUNTS = {
    "suppliers": 30,
    "parts": 40,
    "plants": 20,
    "orders": 50,
    "contracts": 25,
    "flags": 20,
}
#: Share of Parts that have a second supplier and a second stocking plant (1 in N).
SECOND_SOURCE_ONE_IN = 5

#: Key shapes: prefix and first number.
KEY_START = {"supplier": 1001, "part": 10001, "plant": 1, "order": 200001, "contract": 3001}
KEY_PREFIX = {"supplier": "S-", "part": "P-", "plant": "PL-", "order": "PO-", "contract": "C-"}
KEY_WIDTH = {"plant": 2}

# Name material -------------------------------------------------------------------------------

NAME_HEADS = [
    "Bre",
    "Hal",
    "Tor",
    "Kes",
    "Mar",
    "Ors",
    "Ilm",
    "Zen",
    "Quor",
    "Tars",
    "Velk",
    "Nim",
    "Kav",
    "Droz",
    "Pel",
    "Sund",
    "Ulm",
    "Yver",
    "Lok",
    "Fand",
    "Gest",
    "Wex",
    "Bard",
    "Rav",
]
NAME_TAILS = [
    "vik",
    "mar",
    "dane",
    "tek",
    "lund",
    "ton",
    "vane",
    "rix",
    "nova",
    "ra",
    "dor",
    "sen",
    "bro",
    "kel",
    "wick",
    "stal",
    "gard",
    "ine",
    "ello",
    "mont",
]
PRODUCT_WORDS = [
    "Bearings",
    "Fasteners",
    "Seals",
    "Hydraulics",
    "Castings",
    "Drives",
    "Controls",
    "Filtration",
    "Tooling",
    "Components",
    "Alloys",
    "Valves",
    "Sensors",
    "Precision",
    "Industries",
]

#: country -> (weight, cities, legal forms, currency)
COUNTRIES: dict[str, tuple[int, tuple[str, ...], tuple[str, ...], str]] = {
    "IT": (5, ("Turin", "Milan", "Bologna", "Brescia", "Verona"), ("S.p.A.", "S.r.l."), "EUR"),
    "DE": (5, ("Stuttgart", "Nuremberg", "Dresden", "Hamburg", "Cologne"), ("GmbH", "AG"), "EUR"),
    "FR": (2, ("Lyon", "Toulouse", "Lille", "Nantes"), ("SAS", "SA"), "EUR"),
    "ES": (2, ("Bilbao", "Valencia", "Zaragoza"), ("S.A.", "S.L."), "EUR"),
    "PL": (3, ("Wroclaw", "Gdansk", "Poznan"), ("Sp. z o.o.", "S.A."), "PLN"),
    "CZ": (2, ("Brno", "Ostrava", "Plzen"), ("s.r.o.", "a.s."), "CZK"),
    "NL": (1, ("Eindhoven", "Rotterdam"), ("B.V.",), "EUR"),
    "SE": (1, ("Gothenburg", "Malmo"), ("AB",), "SEK"),
    "TR": (2, ("Bursa", "Izmir"), ("A.S.",), "EUR"),
    "PT": (1, ("Porto", "Braga"), ("Lda",), "EUR"),
}
PLANT_COUNTRIES = ("IT", "DE", "FR", "ES", "PL", "CZ")
PLANT_TYPES = {
    "assembly": "Assembly",
    "machining": "Machining",
    "distribution_hub": "Distribution Hub",
    "warehouse": "Warehouse",
}

APPLICATIONS = (
    "conveyor drives",
    "hydraulic presses",
    "packaging lines",
    "pump assemblies",
    "gearboxes",
    "robot joints",
    "compressor stations",
    "machine tool spindles",
)

#: category -> (base names, variants, unit cost range, weight range in kg)
PART_CATALOG: dict[
    str, tuple[tuple[str, ...], tuple[str, ...], tuple[float, float], tuple[float, float]]
] = {
    "bearing": (
        ("Deep groove ball bearing", "Tapered roller bearing", "Needle roller bearing"),
        ("6204", "6305", "32008", "NK25", "6001"),
        (3.5, 48.0),
        (0.05, 2.4),
    ),
    "fastener": (
        ("Hex bolt", "Socket cap screw", "Lock nut", "Flat washer"),
        ("M6x30", "M8x40", "M10x60", "M12x80", "M16"),
        (0.05, 2.5),
        (0.005, 0.2),
    ),
    "seal": (
        ("Radial shaft seal", "O-ring", "Mechanical seal"),
        ("25x42x7", "40x62x8", "NBR 70 30x3", "FKM 90 18x2.5"),
        (0.4, 35.0),
        (0.002, 0.3),
    ),
    "gasket": (
        ("Flange gasket", "Spiral wound gasket"),
        ("DN25 PN16", "DN50 PN40", "DN100 PN16", "DN150 PN25"),
        (1.5, 60.0),
        (0.02, 0.6),
    ),
    "valve": (
        ("Ball valve", "Check valve", "Solenoid valve", "Pressure relief valve"),
        ("DN15", "DN25 24 V", "G1/2", "DN40"),
        (12.0, 420.0),
        (0.3, 6.5),
    ),
    "filter": (
        ("Hydraulic filter element", "Air filter cartridge", "Oil filter"),
        ("10 micron", "25 micron", "H-360", "F-120"),
        (4.0, 160.0),
        (0.1, 3.0),
    ),
    "sensor": (
        ("Inductive proximity sensor", "Pressure transmitter", "Temperature probe"),
        ("M12 PNP", "0-250 bar", "PT100 150 mm", "M18 NO"),
        (9.0, 280.0),
        (0.03, 0.9),
    ),
    "motor": (
        ("Servo motor", "Induction motor", "Gear motor"),
        ("0.75 kW", "2.2 kW", "5.5 kW", "11 kW"),
        (140.0, 2400.0),
        (4.0, 65.0),
    ),
    "hose": (
        ("Hydraulic hose assembly", "Pneumatic tube", "Braided hose"),
        ("DN10 2 m", "DN16 1.5 m", "DN20 3 m", "8x6 mm"),
        (6.0, 190.0),
        (0.2, 4.5),
    ),
}

FLAG_REASONS = {
    "low": (
        "Two late delivery notices on recent orders.",
        "Minor quality deviation reported at incoming inspection.",
        "Certificate of conformity renewed late.",
    ),
    "medium": (
        "On-time delivery fell below 85 percent over the last quarter.",
        "Repeated packaging damage on inbound shipments.",
        "Price increase request outside the contract index clause.",
    ),
    "high": (
        "Sole-source supplier with a negative financial outlook.",
        "Audit found open critical findings on process control.",
        "Capacity constraint announced for the next two quarters.",
    ),
    "critical": (
        "Production halted at the supplier site after a plant incident.",
        "Insolvency proceedings announced.",
        "Export restriction blocks shipments of a sole-source part.",
    ),
}


# Output tables -------------------------------------------------------------------------------

COLUMNS: dict[str, tuple[str, ...]] = {
    "supplier": (
        "supplier_code",
        "legal_name",
        "country",
        "city",
        "tier",
        "risk_score",
        "status",
        "onboarded_on",
        "vat_number",
    ),
    "part": (
        "part_number",
        "name",
        "category",
        "summary",
        "unit_cost",
        "weight_kg",
        "lifecycle",
        "hazardous",
    ),
    "plant": ("plant_code", "name", "plant_type", "country", "city", "capacity_per_month"),
    "purchase_order": (
        "po_number",
        "supplier",
        "status",
        "ordered_on",
        "due_on",
        "quantity",
        "unit_price",
        "currency",
    ),
    "contract": (
        "contract_number",
        "title",
        "status",
        "start_date",
        "end_date",
        "annual_value",
        "currency",
        "incoterm",
        "payment_terms_days",
        "auto_renew",
        "approved_by",
        "approved_at",
    ),
    "risk_flag": ("key", "severity", "reason", "raised_at", "raised_by"),
    "supplied_by": ("from_key", "to_key", "is_primary", "lead_time_days"),
    "stocked_at": ("from_key", "to_key", "quantity"),
    "for_part": ("from_key", "to_key"),
    "delivered_to": ("from_key", "to_key"),
    "with_supplier": ("from_key", "to_key"),
    "flags": ("from_key", "to_key"),
}

Row = dict[str, str]


@dataclass(frozen=True)
class Settings:
    scale: int
    seed: int
    as_of: date


def key(kind: str, index: int) -> str:
    width = KEY_WIDTH.get(kind, 1)
    return f"{KEY_PREFIX[kind]}{KEY_START[kind] + index:0{width}d}"


def money(value: float, places: int = 2) -> str:
    return str(Decimal(str(value)).quantize(Decimal(1).scaleb(-places)))


def timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def ulid(moment: datetime, rng: random.Random) -> str:
    """A ULID for ``moment`` with seeded randomness (Crockford base32, 26 characters)."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    number = (int(moment.timestamp() * 1000) << 80) | rng.getrandbits(80)
    return "".join(alphabet[(number >> shift) & 31] for shift in range(125, -1, -5))


def build_suppliers(settings: Settings, rng: random.Random) -> list[Row]:
    stems = [head + tail for head in NAME_HEADS for tail in NAME_TAILS]
    rng.shuffle(stems)
    countries = list(COUNTRIES)
    weights = [COUNTRIES[country][0] for country in countries]
    rows: list[Row] = []
    for index in range(BASE_COUNTS["suppliers"] * settings.scale):
        country = rng.choices(countries, weights)[0]
        _, cities, forms, _ = COUNTRIES[country]
        tier = rng.choices(["1", "2", "3"], [2, 4, 4])[0]
        risk_floor = {"1": 5, "2": 15, "3": 25}[tier]
        status = rng.choices(["active", "on_hold", "terminated"], [90, 7, 3])[0]
        name = f"{stems[index % len(stems)]} {rng.choice(PRODUCT_WORDS)} {rng.choice(forms)}"
        rows.append(
            {
                "supplier_code": key("supplier", index),
                "legal_name": name,
                "country": country,
                "city": rng.choice(cities),
                "tier": tier,
                "risk_score": str(rng.randrange(risk_floor, risk_floor + 60)),
                "status": status,
                "onboarded_on": (
                    settings.as_of - timedelta(days=rng.randrange(200, 3000))
                ).isoformat(),
                "vat_number": f"{country}{(index + 1) * 7919 % 10**9:09d}",
            }
        )
    return rows


def build_parts(settings: Settings, rng: random.Random) -> list[Row]:
    categories = list(PART_CATALOG)
    rows: list[Row] = []
    for index in range(BASE_COUNTS["parts"] * settings.scale):
        category = (
            categories[index % len(categories)]
            if index < len(categories)
            else rng.choice(categories)
        )
        bases, variants, (cost_low, cost_high), (weight_low, weight_high) = PART_CATALOG[category]
        base, variant = rng.choice(bases), rng.choice(variants)
        rows.append(
            {
                "part_number": key("part", index),
                "name": f"{base} {variant}",
                "category": category,
                "summary": f"Stock item for {rng.choice(APPLICATIONS)}; {base.lower()} {variant}.",
                "unit_cost": money(rng.uniform(cost_low, cost_high)),
                "weight_kg": money(rng.uniform(weight_low, weight_high), 3),
                "lifecycle": rng.choices(["active", "phase_out", "obsolete"], [85, 10, 5])[0],
                "hazardous": "true" if rng.random() < 0.05 else "false",
            }
        )
    return rows


def build_plants(settings: Settings, rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    for index in range(BASE_COUNTS["plants"] * settings.scale):
        country = PLANT_COUNTRIES[index % len(PLANT_COUNTRIES)]
        city = rng.choice(COUNTRIES[country][1])
        plant_type = rng.choice(list(PLANT_TYPES))
        rows.append(
            {
                "plant_code": key("plant", index),
                "name": f"{city} {PLANT_TYPES[plant_type]}",
                "plant_type": plant_type,
                "country": country,
                "city": city,
                "capacity_per_month": str(rng.randrange(20, 400) * 100),
            }
        )
    return rows


def second_sources(count: int, rng: random.Random) -> set[int]:
    return set(rng.sample(range(count), count // SECOND_SOURCE_ONE_IN))


def build_sourcing(
    parts: Sequence[Row], suppliers: Sequence[Row], plants: Sequence[Row], rng: random.Random
) -> tuple[list[Row], list[Row]]:
    """``supplied_by`` and ``stocked_at`` Links: one source and plant per Part, some with two."""
    doubled_suppliers = second_sources(len(parts), rng)
    doubled_plants = second_sources(len(parts), rng)
    supplied: list[Row] = []
    stocked: list[Row] = []
    for index, part in enumerate(parts):
        first = rng.randrange(len(suppliers))
        chosen = [first]
        if index in doubled_suppliers:
            chosen.append((first + 1 + rng.randrange(len(suppliers) - 1)) % len(suppliers))
        for position, supplier_index in enumerate(chosen):
            supplied.append(
                {
                    "from_key": part["part_number"],
                    "to_key": suppliers[supplier_index]["supplier_code"],
                    "is_primary": "true" if position == 0 else "false",
                    "lead_time_days": str(rng.randrange(7, 91)),
                }
            )
        first_plant = rng.randrange(len(plants))
        chosen_plants = [first_plant]
        if index in doubled_plants:
            chosen_plants.append((first_plant + 1 + rng.randrange(len(plants) - 1)) % len(plants))
        for plant_index in chosen_plants:
            stocked.append(
                {
                    "from_key": part["part_number"],
                    "to_key": plants[plant_index]["plant_code"],
                    "quantity": str(rng.randrange(5, 500) * 10),
                }
            )
    return supplied, stocked


def build_orders(
    settings: Settings,
    parts: Sequence[Row],
    suppliers: Sequence[Row],
    supplied: Sequence[Row],
    stocked: Sequence[Row],
    rng: random.Random,
) -> tuple[list[Row], list[Row], list[Row]]:
    """Purchase orders plus their ``for_part`` and ``delivered_to`` Links.

    Each order buys a Part from one of its suppliers and ships it to a plant that stocks it.
    """
    part_cost = {part["part_number"]: float(part["unit_cost"]) for part in parts}
    country = {s["supplier_code"]: s["country"] for s in suppliers}
    plants_of: dict[str, list[str]] = {}
    for link in stocked:
        plants_of.setdefault(link["from_key"], []).append(link["to_key"])
    orders: list[Row] = []
    for_part: list[Row] = []
    delivered_to: list[Row] = []
    for index in range(BASE_COUNTS["orders"] * settings.scale):
        link = rng.choice(supplied)
        number = key("order", index)
        ordered_on = settings.as_of - timedelta(days=rng.randrange(0, 240))
        due_on = ordered_on + timedelta(days=int(link["lead_time_days"]) + rng.randrange(0, 15))
        if due_on < settings.as_of - timedelta(days=5):
            status = rng.choices(["received", "cancelled"], [85, 15])[0]
        elif ordered_on > settings.as_of - timedelta(days=10):
            status = rng.choices(["draft", "issued"], [30, 70])[0]
        else:
            status = rng.choices(["issued", "shipped"], [60, 40])[0]
        orders.append(
            {
                "po_number": number,
                "supplier": link["to_key"],
                "status": status,
                "ordered_on": ordered_on.isoformat(),
                "due_on": due_on.isoformat(),
                "quantity": str(rng.choice([50, 100, 200, 250, 500, 1000, 2500, 5000])),
                "unit_price": money(part_cost[link["from_key"]] * rng.uniform(0.9, 1.15)),
                "currency": COUNTRIES[country[link["to_key"]]][3],
            }
        )
        for_part.append({"from_key": number, "to_key": link["from_key"]})
        delivered_to.append({"from_key": number, "to_key": rng.choice(plants_of[link["from_key"]])})
    return orders, for_part, delivered_to


def build_contracts(
    settings: Settings, suppliers: Sequence[Row], rng: random.Random
) -> tuple[list[Row], list[Row]]:
    """Contracts and ``with_supplier`` Links; a share of them expires within 90 days."""
    rows: list[Row] = []
    links: list[Row] = []
    for index in range(BASE_COUNTS["contracts"] * settings.scale):
        supplier = suppliers[index % len(suppliers)]
        shape = index % 6
        term_years = rng.randrange(1, 6)
        if shape == 0:  # active, expiring within 90 days
            end = settings.as_of + timedelta(days=rng.randrange(10, 89))
            start = end - timedelta(days=365 * term_years)
            status = "active"
        elif shape == 1:  # expired
            end = settings.as_of - timedelta(days=rng.randrange(30, 400))
            start = end - timedelta(days=365 * term_years)
            status = "expired"
        elif shape == 2:  # draft, not started
            start = settings.as_of + timedelta(days=rng.randrange(15, 120))
            end = start + timedelta(days=365 * term_years)
            status = "draft"
        else:  # active, long running
            end = settings.as_of + timedelta(days=rng.randrange(120, 1200))
            start = end - timedelta(days=365 * term_years)
            status = "active"
        if shape == 4 and index % 12 == 4:
            status = "terminated"
        approved = status != "draft"
        approved_at = datetime.combine(start, time(9, 30), UTC) - timedelta(
            days=rng.randrange(5, 40)
        )
        number = key("contract", index)
        rows.append(
            {
                "contract_number": number,
                "title": f"Framework supply agreement {start.year} - {supplier['legal_name']}",
                "status": status,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "annual_value": money(rng.randrange(40, 2000) * 1000.0),
                "currency": COUNTRIES[supplier["country"]][3],
                "incoterm": rng.choice(["EXW", "FCA", "DAP", "DDP"]),
                "payment_terms_days": str(rng.choice([30, 45, 60, 90])),
                "auto_renew": "true" if rng.random() < 0.4 else "false",
                "approved_by": f"contract-manager-{rng.randrange(1, 4)}" if approved else "",
                "approved_at": timestamp(approved_at) if approved else "",
            }
        )
        links.append({"from_key": number, "to_key": supplier["supplier_code"]})
    return rows, links


def build_flags(
    settings: Settings, suppliers: Sequence[Row], rng: random.Random
) -> tuple[list[Row], list[Row]]:
    """Risk flags on the riskier half of the suppliers, oldest first, with ULID keys."""
    risky = sorted(suppliers, key=lambda s: -int(s["risk_score"]))[: max(1, len(suppliers) // 2)]
    moments = sorted(
        datetime.combine(settings.as_of, datetime.min.time(), UTC)
        - timedelta(days=rng.randrange(0, 200), seconds=rng.randrange(0, 86_400))
        for _ in range(BASE_COUNTS["flags"] * settings.scale)
    )
    rows: list[Row] = []
    links: list[Row] = []
    for moment in moments:
        supplier = rng.choice(risky)
        score = int(supplier["risk_score"])
        severity = (
            "critical" if score > 80 else "high" if score > 60 else rng.choice(["low", "medium"])
        )
        flag_key = ulid(moment, rng)
        rows.append(
            {
                "key": flag_key,
                "severity": severity,
                "reason": rng.choice(FLAG_REASONS[severity]),
                "raised_at": timestamp(moment),
                "raised_by": f"analyst-{rng.randrange(1, 5)}",
            }
        )
        links.append({"from_key": flag_key, "to_key": supplier["supplier_code"]})
    return rows, links


def build_dataset(settings: Settings) -> dict[str, list[Row]]:
    """All tables of the dataset, keyed by file stem."""
    rng = random.Random(settings.seed)
    suppliers = build_suppliers(settings, rng)
    parts = build_parts(settings, rng)
    plants = build_plants(settings, rng)
    supplied, stocked = build_sourcing(parts, suppliers, plants, rng)
    orders, for_part, delivered_to = build_orders(
        settings, parts, suppliers, supplied, stocked, rng
    )
    contracts, with_supplier = build_contracts(settings, suppliers, rng)
    flag_rows, flag_links = build_flags(settings, suppliers, rng)
    return {
        "supplier": suppliers,
        "part": parts,
        "plant": plants,
        "purchase_order": orders,
        "contract": contracts,
        "risk_flag": flag_rows,
        "supplied_by": supplied,
        "stocked_at": stocked,
        "for_part": for_part,
        "delivered_to": delivered_to,
        "with_supplier": with_supplier,
        "flags": flag_links,
    }


def write_dataset(tables: dict[str, list[Row]], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        with (out / f"{name}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS[name], lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate the demo supply dataset as CSV files.")
    parser.add_argument("--scale", type=int, default=1, help="row count multiplier (default 1)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="random seed")
    parser.add_argument("--as-of", default=DEFAULT_AS_OF, help="reference date, YYYY-MM-DD")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    args = parser.parse_args(argv)
    if args.scale < 1:
        parser.error("--scale must be at least 1")
    tables = build_dataset(Settings(args.scale, args.seed, date.fromisoformat(args.as_of)))
    write_dataset(tables, args.out)
    for name, rows in tables.items():
        print(f"{name}.csv: {len(rows)} rows")


if __name__ == "__main__":
    main()
