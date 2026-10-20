# Context: supply/main
revision cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc; estimated 520 of 700 tokens; trimmed: lineage(2), recent_changes(2), far_neighbor_values(2), far_neighbors(2), schema_descriptions(1); changes since aaaaaaaaaaaa

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

## Links
### supplied_by (Part -> Supplier)
- Part/P-10028 -> Supplier/S-1001 (is_primary=true; lead_time_days=21)
### with_supplier (Contract -> Supplier)
- Contract/C-3001 -> Supplier/S-1001

## Schema
### Kind Contract
key: contract_number; title: title
- contract_number (string, required)
- title (string, required)
- annual_value (decimal(12,2))
- approved_at (timestamp)
- approved_by (string)
- auto_renew (boolean)
- currency (enum(EUR|USD|GBP|PLN|CZK|SEK))
- end_date (date)
- incoterm (enum(EXW|FCA|DAP|DDP))
- payment_terms_days (integer)
- start_date (date, required)
- status (enum(draft|active|expired|terminated))
### Kind Part
key: part_number; title: name
- part_number (string, required)
- name (string, required)
- category (enum(bearing|fastener|seal|gasket|valve|filter|sensor|motor|hose), required)
- hazardous (boolean)
- lifecycle (enum(active|phase_out|obsolete))
- single_sourced (boolean, derived)
- summary (text)
- supplier_count (integer, derived)
- unit_cost (decimal(10,2))
- weight_kg (decimal(8,3))
### Kind Supplier
key: supplier_code; title: legal_name
- supplier_code (string, required)
- legal_name (string, required)
- city (string)
- country (string, required)
- onboarded_on (date)
- risk_score (integer)
- status (enum(active|on_hold|terminated))
- tier (enum(1|2|3))
- vat_number (string)
### Relations
- supplied_by: Part -> Supplier, many_to_many, inverse supplies
  - is_primary (boolean)
  - lead_time_days (integer)
- with_supplier: Contract -> Supplier, many_to_one, inverse contracts
