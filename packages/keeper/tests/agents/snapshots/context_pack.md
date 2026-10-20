# Context: supply/main
revision cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc; estimated 1325 of 4000 tokens; trimmed: none; changes since aaaaaaaaaaaa

## Records
### Supplier/S-1001: Kavsen Alloys Sp. z o.o.
- supplier_code: S-1001
- legal_name: Kavsen Alloys Sp. z o.o.
- country: PL
- city: Poznan
- tier: 1
- risk_score: 90
- status: active
- (last changed in bbbbbbbbbbbb)

## Neighbors
| ref | title | depth | values |
| --- | --- | --- | --- |
| Contract/C-3001 | Framework supply agreement 2023 - Kavsen Alloys Sp. z o.o. | 1 | contract_number=C-3001; status=active; currency=PLN |
| Part/P-10028 | Radial shaft seal 25x42x7 | 1 | part_number=P-10028; category=seal; unit_cost=24.01 |
| Plant/PL-03 | Wroclaw assembly | 2 | plant_code=PL-03; plant_type=assembly; country=PL |
| Plant/PL-05 | Lyon hub | 2 | plant_code=PL-05; plant_type=distribution_hub |

## Links
### stocked_at (Part -> Plant)
- Part/P-10028 -> Plant/PL-03 (quantity=120)
- Part/P-10028 -> Plant/PL-05 (quantity=40)
### supplied_by (Part -> Supplier)
- Part/P-10028 -> Supplier/S-1001 (is_primary=true; lead_time_days=21)
### with_supplier (Contract -> Supplier)
- Contract/C-3001 -> Supplier/S-1001

## Schema
### Kind Contract
A framework supply agreement with a supplier.
key: contract_number; title: title
- contract_number (string, required): Contract number, for example C-3001.
- title (string, required): Contract title.
- annual_value (decimal(12,2)): Expected yearly spend in the contract currency.
- approved_at (timestamp): When the contract was approved.
- approved_by (string): Principal that approved the contract.
- auto_renew (boolean): True when the contract renews unless a party gives notice.
- currency (enum(EUR|USD|GBP|PLN|CZK|SEK)): ISO 4217 currency code of the contract.
- end_date (date): Last day of validity.
- incoterm (enum(EXW|FCA|DAP|DDP)): Delivery terms (Incoterms 2020).
- payment_terms_days (integer): Days from invoice to payment.
- start_date (date, required): First day of validity.
- status (enum(draft|active|expired|terminated)): Contract lifecycle stage.
### Kind Part
A component bought from suppliers and held in stock.
key: part_number; title: name
- part_number (string, required): Part number, for example P-10001.
- name (string, required): Short catalogue name including the main size or rating.
- category (enum(bearing|fastener|seal|gasket|valve|filter|sensor|motor|hose), required): Commodity group of the part.
- hazardous (boolean): True when shipping the part needs dangerous goods handling.
- lifecycle (enum(active|phase_out|obsolete)): Lifecycle stage of the part.
- single_sourced (boolean, derived): True when exactly one supplier can supply the part. Derived.
- summary (text): One sentence on what the part is used for.
- supplier_count (integer, derived): Number of suppliers that can supply the part. Derived.
- unit_cost (decimal(10,2)): Standard unit cost in euro.
- weight_kg (decimal(8,3)): Unit weight in kilograms.
### Kind Plant
A Corvane production or distribution site that stocks and consumes parts.
key: plant_code; title: name
- plant_code (string, required): Plant code, for example PL-01.
- name (string, required): Site name.
- capacity_per_month (integer): Handling capacity in units per month.
- city (string): City of the site.
- country (string, required): ISO 3166-1 alpha-2 country code of the site.
- plant_type (enum(assembly|machining|distribution_hub|warehouse), required): Main activity of the site.
### Kind Supplier
A company that supplies parts to Corvane Industries.
key: supplier_code; title: legal_name
- supplier_code (string, required): Supplier code as issued by the ERP, for example S-1001.
- legal_name (string, required): Registered legal name including the legal form.
- city (string): City of the registered office.
- country (string, required): ISO 3166-1 alpha-2 country code of the registered office.
- onboarded_on (date): Date the supplier was approved for purchasing.
- risk_score (integer): Supplier risk score from 0 (safe) to 100 (critical), maintained by risk analysts.
- status (enum(active|on_hold|terminated)): Commercial status of the supplier relationship.
- tier (enum(1|2|3)): Sourcing tier, 1 strategic, 2 preferred, 3 transactional.
- vat_number (string): VAT registration number.
### Relations
- stocked_at: Part -> Plant, many_to_many, inverse stocks. A plant keeps the part in stock.
  - quantity (integer, required): Units on hand.
- supplied_by: Part -> Supplier, many_to_many, inverse supplies. A supplier can supply the part.
  - is_primary (boolean): True for the preferred supplier of the part.
  - lead_time_days (integer): Typical days from order to delivery.
- with_supplier: Contract -> Supplier, many_to_one, inverse contracts. The supplier party to a contract.

## Recent changes
| revision | at | record | change | attributes | by | origin | message |
| --- | --- | --- | --- | --- | --- | --- | --- |
| aaaaaaaaaaaa | 2026-10-01T08:00:03Z | Supplier/S-1001 | added | supplier_code, legal_name, country, city, tier, risk_score | usr_loader | import | Import the supply example data |
| bbbbbbbbbbbb | 2026-10-01T08:00:04Z | Supplier/S-1001 | modified | risk_score | usr_editor | operation | Raise the risk of S-1001 |

## Lineage
- Supplier/S-1001: supplier_code, legal_name, country, city, tier, status <- import by usr_loader at 2026-10-01T08:00:03Z
- Supplier/S-1001: risk_score <- operation by usr_editor at 2026-10-01T08:00:04Z
