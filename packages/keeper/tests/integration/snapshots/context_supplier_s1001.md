# Context: supply/main
revision 6eba81e945132f5452cf74bb3b8adcfbdc3d9546dec31e45650846b3b9fff1ac; estimated 1458 of 1500 tokens; trimmed: lineage(2), recent_changes(7)

## Records
### Supplier/S-1001: Kavsen Alloys Sp. z o.o.
- supplier_code: S-1001
- legal_name: Kavsen Alloys Sp. z o.o.
- city: Poznan
- country: PL
- onboarded_on: 2024-02-26
- risk_score: 90
- status: active
- tier: 1
- vat_number: PL000007919
- (last changed in 6eba81e94513)

## Neighbors
| ref | title | depth | values |
| --- | --- | --- | --- |
| Contract/C-3001 | Framework supply agreement 2023 - Kavsen Alloys Sp. z o.o. | 1 | contract_number=C-3001; title=Framework supply agreement 2023 - Kavsen Alloys Sp. z o.o.; annual_value=922000.00; approved_at=2023-01-27T09:30:00.000000Z; approved_by=contract-manager-3; auto_renew=false; currency=PLN; end_date=2026-02-13; incoterm=DDP; payment_terms_days=30; start_date=2023-02-14; status=active |
| Part/P-10028 | Radial shaft seal 25x42x7 | 1 | part_number=P-10028; name=Radial shaft seal 25x42x7; category=seal; hazardous=false; lifecycle=active; summary=Stock item for machine tool spindles; radial shaft seal 25x42x7.; unit_cost=24.01; weight_kg=0.081 |
| PurchaseOrder/PO-200002 | PO-200002 | 1 | po_number=PO-200002; currency=PLN; due_on=2026-01-05; ordered_on=2025-12-01; quantity=5000; status=received; supplier=S-1001; unit_price=24.40 |
| PurchaseOrder/PO-200023 | PO-200023 | 1 | po_number=PO-200023; currency=PLN; due_on=2025-10-22; ordered_on=2025-09-22; quantity=250; status=received; supplier=S-1001; unit_price=22.84 |

## Links
### issued_to (PurchaseOrder -> Supplier)
- PurchaseOrder/PO-200002 -> Supplier/S-1001
- PurchaseOrder/PO-200023 -> Supplier/S-1001
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
### Kind PurchaseOrder
An order placed with a supplier for one part, delivered to one plant.
key: po_number; title: po_number
- po_number (string, required): Purchase order number, for example PO-200001.
- currency (enum(EUR|USD|GBP|PLN|CZK|SEK)): ISO 4217 currency code of the order.
- due_on (date): Date the goods are due at the plant.
- ordered_on (date, required): Date the order was placed.
- quantity (integer, required): Ordered quantity in units.
- status (enum(draft|issued|shipped|received|cancelled), required): Order lifecycle stage.
- supplier (ref(Supplier), required): Key of the supplier the order is issued to. Backs the Relation issued_to.
- unit_price (decimal(10,2)): Agreed unit price in the order currency.
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
- issued_to: PurchaseOrder -> Supplier, many_to_one, inverse purchase_orders. The supplier an order is issued to; derived from PurchaseOrder.supplier.
- supplied_by: Part -> Supplier, many_to_many, inverse supplies. A supplier can supply the part.
  - is_primary (boolean): True for the preferred supplier of the part.
  - lead_time_days (integer): Typical days from order to delivery.
- with_supplier: Contract -> Supplier, many_to_one, inverse contracts. The supplier party to a contract.

## Recent changes
| revision | at | record | change | attributes | by | origin | message |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 6eba81e94513 | 2026-10-01T08:00:04Z | Supplier/S-1001 | modified | risk_score | usr_editor | operation | Raise the risk of S-1001 |
