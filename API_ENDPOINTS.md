# API Endpoints

**128 operations across 5 apps**, verified against the live OpenAPI schema
(`/api/schema/`) in both directions: every path read from `urls.py`/`views.py`
appears in the generated schema, and every schema path is accounted for here.
Nothing is estimated or sampled — this is a complete list.

Live, always-current version of this: **`/api/docs/`** — generated from the
code, so it cannot go stale the way this file can. Re-verify against it
(or `/api/schema/`) before trusting this file for anything load-bearing;
it reflects the codebase as reviewed, not a live feed.

| App | Operations |
|---|---:|
| `accounts` (`/api/auth/`) | 18 |
| `catalog` (`/api/catalog/`) | 62 |
| `inventory` (`/api/inventory/`) | 19 |
| `orders` (`/api/orders/`) | 11 |
| `procurement` (`/api/procurement/`) | 18 |

Plus 3 utility paths outside these counts (`/api/`, `/api/schema/`,
`/api/docs/`) and the Django admin at `/admin/`.

---

## Roles

| Short | Role |
|---|---|
| **PL** | Program Lead |
| **OM** | Operations Manager |
| **WH** | Warehouse Staff |
| **SCH** | School Staff |
| **FIN** | Finance Department |

"Leads" means PL and OM together — they have identical access almost
everywhere in this API. Where a table says an entry is scoped ("own
warehouse", "own school"), that scoping happens at the *queryset* level, not
just the permission check: the role passes the gate but only sees its own
rows, and a cross-site `{id}` typically 404s rather than 403s so existence
isn't leaked.

---

## Conventions

**Base URL** — `http://127.0.0.1:8000` in development.

**Authentication** — every endpoint except `login`/`refresh`/`verify`
requires a bearer token:

```
Authorization: Bearer <access_token>
```

Access tokens last 30 minutes. Refresh tokens last 12 hours and **rotate** —
the one you send is blacklisted, so sending it twice returns 401. That is
correct behaviour, not a bug.

**Sign in with an email address.** There is no username anywhere in this API.

**List responses are paginated**, 50 per page:

```json
{ "count": 32, "next": "...?page=2", "previous": null, "results": [ ... ] }
```

**Money is a string**, never a number — `"25000.00"`. Floating point cannot
represent `0.1` exactly, and a rounding error on a price becomes a wrong
invoice. Parse it with a decimal library, not `parseFloat`.

**Dates are `YYYY-MM-DD`.** Endpoints that read pricing or stock accept
`?on=` / `?as_of=` to ask "as at this date"; the default is today.

**Two-step documents.** Receipts, warehouse transfers, and inventory
adjustments are all entered, then posted, as separate calls — a
`POST .../post-to-ledger/` or `.../post_to_inventory/` style action distinct
from the `POST` that created the draft. This lets a warehouse clerk key in
what arrived, check it against the paper packing list, correct a typo, and
only then commit it to the ledger. A document can only be posted once;
posting twice is refused (it would double the stock movement).

**Errors**

| Code | Meaning |
|---|---|
| 400 | Your input was rejected. The body names the field |
| 401 | No token, or it expired. Sign in or refresh |
| 403 | Signed in, but your role is not allowed to do this |
| 404 | Not found (also returned instead of 403 for a cross-site `{id}`, so existence isn't leaked) |
| 409 | The row is still in use by something else. Nothing you change about the request will help |
| 429 | Rate limited. Login is 10/min per email address |

A 400 body is keyed by field, with `non_field_errors` for whole-record rules:

```json
{ "non_field_errors": ["This garment already has a price covering part of that period."] }
```

A 409 names what is blocking the delete, so the message can be acted on:

```json
{
  "detail": "This cannot be deleted while other records still refer to it. Still in use by: Namayemba PS.",
  "in_use_by": ["Namayemba PS (Primary School)"]
}
```

---

## Deleting

Master data splits in two, and the line is **does the row carry meaning that
outlives it**.

**Cannot be deleted — 405.** Retire, correct, or withdraw instead:

| Resource | Instead, do this | Why |
|---|---|---|
| Users | `POST /auth/users/{id}/deactivate/` | Sign-in history and every stock movement point at them |
| SKUs | `PATCH {"is_active": false}` | The control number is printed on documents and never reissued |
| Prices | `PATCH` to correct it | A March invoice reprints at March's price only while that row exists |
| Reason codes | `PATCH {"is_active": false}` | A past adjustment still points at the code used to make it |
| Warehouse transfers, inventory adjustments, receipts | n/a — no delete route exists at all | Two-step documents are never removed once drafted; a mistake is corrected before posting or reversed with a new document after |
| Group orders, production orders | n/a — no delete route; `PATCH {"status": "CANCELLED"}` instead | An order funds a Tailoring Center; withdrawing it is a status change, not an erasure |
| School orders | n/a — no delete route; `POST .../cancel/` instead, and only while on Hold | A parent already has the invoice number |

**Can be deleted** — garments, sizes, tailoring centers, warehouses, schools,
minimum stock levels, kits, kit items. These are configuration, and deleting
is how a typo gets fixed. The database still refuses any row that is
referenced elsewhere, with a **409** naming what is using it — except
`KitItem`, which cascades: deleting a `Kit` removes its bill-of-materials
lines with no 409, the one deliberate exception to this codebase's usual
`PROTECT`-everywhere default.

---

## `accounts` — 18 operations

### Tokens (no token required)

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/auth/login/` | Exchange email + password for `access` + `refresh` tokens, plus the whole user record |
| `POST` | `/api/auth/refresh/` | Exchange a refresh token for a new `access` **and a new `refresh`** — the old refresh token is now dead |
| `POST` | `/api/auth/verify/` | 200 if a token is still valid, 401 if not |

```json
{ "email": "sharon@asone.test", "password": "..." }
```

```json
{
  "access": "eyJ...",
  "refresh": "eyJ...",
  "user": {
    "id": 5,
    "email": "julius@asone.test",
    "first_name": "Julius",
    "last_name": "Okello",
    "role": "WAREHOUSE_STAFF",
    "role_display": "Warehouse Staff",
    "warehouse": { "id": 2, "name": "Namayemba" },
    "school": null,
    "is_active": true,
    "must_change_password": false,
    "access": {
      "scope": "assigned_warehouse",
      "functions": {
        "table_updates": false,
        "production_orders": false,
        "warehouse_receiving_and_shipping": true,
        "inventory_adjustments": false,
        "school_orders": false,
        "backorder_transfers": true,
        "financial_reports": false
      }
    }
  }
}
```

> **`must_change_password: true` means route straight to the change-password
> screen.** That account gets 403 on everything except reading itself,
> setting a password, and signing out.

401 on a wrong password **and** on an unknown address — the two responses are
identical, so neither reveals whether an account exists.

### The signed-in user

| Method | Path | Does | Read | Write |
|---|---|---|---|---|
| `GET` | `/api/auth/me/` | Your own record — same shape as `login`'s `user` object | any signed-in user, self only (works even mid password-change) | — |
| `PATCH` | `/api/auth/me/` | Change your own name/email (role and site are **not** writable here) | — | any signed-in user, self only |
| `POST` | `/api/auth/password/change/` | Change your own password (current password required); rotates tokens and signs out every other session | — | any signed-in user (this is how the password-change gate clears) |
| `POST` | `/api/auth/logout/` | Blacklist a refresh token, signing that session out. Returns **205** | — | any signed-in user |

### Reference data

| Method | Path | Does | Read | Write |
|---|---|---|---|---|
| `GET` | `/api/auth/roles/` | The five roles and the seven columns of AsOne's access matrix | any signed-in user **not** currently on a forced password change — worth confirming with the client, since this is otherwise-harmless reference data a "set your password" screen could use | — |

```json
[
  {
    "value": "WAREHOUSE_STAFF",
    "label": "Warehouse Staff",
    "summary": "One warehouse. Enters receipts from the Tailoring Centers...",
    "caveat": "Cannot see the other warehouse's stock, change master data...",
    "scope": "assigned_warehouse",
    "requires_site": "warehouse",
    "functions": { "...": true }
  }
]
```

> **Do not hardcode roles in the frontend.** Use `requires_site` to decide
> whether a user form shows a warehouse picker, a school picker, or neither,
> and `access.functions` to decide which navigation items to draw.
>
> Hiding a menu is cosmetic. The server re-checks every request, so a 403 is
> always possible and must be handled.

### User administration — **leads (PL, OM) only**, everyone else 403

| Method | Path | Does | Read | Write |
|---|---|---|---|---|
| `GET` | `/api/auth/users/` | List. Filter `?role=` `?is_active=` `?warehouse=` `?school=` | leads | — |
| `POST` | `/api/auth/users/` | Add a user; returns the plaintext password once | — | leads |
| `GET` | `/api/auth/users/{id}/` | One user | leads | — |
| `PATCH` | `/api/auth/users/{id}/` | Edit name, email, role, site — blocks a lead from changing their own role or deactivating themself | — | leads |
| `POST` | `/api/auth/users/{id}/set-password/` | Set someone else's password (or auto-generate one); no current password needed, since the point is nobody has it. Signs that account out everywhere | — | leads |
| `POST` | `/api/auth/users/{id}/activate/` | Restore access | — | leads |
| `POST` | `/api/auth/users/{id}/deactivate/` | Remove access, sign out everywhere (cannot target yourself) | — | leads |
| `POST` | `/api/auth/users/{id}/sign-out/` | Retire sessions; account stays usable | — | leads |

`PUT` and `DELETE` on `/users/{id}/` are both **405** — see [Deleting](#deleting).

```json
{
  "first_name": "Miriam",
  "last_name": "Achieng",
  "email": "miriam@asone.test",
  "role": "SCHOOL_STAFF",
  "school": 1,
  "password": "a-strong-passphrase"
}
```

Warehouse staff need `warehouse`; school staff need `school`; the
all-locations roles take neither — check `requires_site` from `/roles/`.
Sending the wrong one is a 400. The account must replace the password at
first sign-in unless you send `must_change_password: false`.

### Login attempts (read-only)

| Method | Path | Does | Read | Write |
|---|---|---|---|---|
| `GET` | `/api/auth/login-attempts/` | Every sign-in attempt, success or fail, newest first. Filter `?email=` `?succeeded=` `?user=` | leads | — |
| `GET` | `/api/auth/login-attempts/{id}/` | One attempt | leads | — |

No create/update/delete route exists — these rows are written only as a
byproduct of `/login/` itself.

---

## `catalog` — 62 operations

Write is **identical across all ten resources in this app: leads (PL, OM)
only** — this is the "Table Updates" column of AsOne's access matrix, and it
never varies by table. Read varies per resource instead, so only read access
is called out row by row below.

### Sites — F10, F11, F12 (full CRUD)

| Path | Feature | Read |
|---|---|---|
| `/api/catalog/tailoring-centers/` | F10 | leads, WH |
| `/api/catalog/warehouses/` | F11 | leads, WH |
| `/api/catalog/schools/` | F12 | leads, WH, SCH |

Each supports `GET` (list, with `?search=`), `POST`, `GET /{id}/`,
`PUT /{id}/`, `PATCH /{id}/`, `DELETE /{id}/` (409 if referenced elsewhere).

```json
{ "id": 2, "name": "Namayemba", "address": "",
  "primary_tailoring_center": 1, "primary_tailoring_center_name": "Idudi" }
```

Schools carry `level` (`PS` or `HS`), `level_display`, and
`primary_warehouse`. Filter with `?level=` and `?primary_warehouse=`.

### Products — F05, F06, F07

#### Sizes — F05 (full CRUD)

`/api/catalog/sizes/` — **read: leads only.** `GET`, `POST`, `GET /{id}/`,
`PUT`, `PATCH`, `DELETE` (409 if a SKU still uses it).

#### Garments — F05 (full CRUD + 2 actions)

`/api/catalog/garments/` — **read: leads only.**

```json
{ "id": 1, "name": "White Shirt", "school_level": "BOTH",
  "school_level_display": "Both", "colour": "White", "is_active": true,
  "current_price": "25000.00", "sku_count": 5 }
```

`current_price` is **null**, not `0`, when a garment has no price today — a
missing price is a data gap, not a free uniform. Filter with
`?school_level=` and `?is_active=`; search with `?search=`.

> **Read here is narrower than the SKU that mirrors this same price**:
> `/skus/` exposes the identical read-through `unit_price` to Warehouse
> Staff, School Staff and Finance, but `/garments/` does not. The code's own
> comment flags this as worth confirming with the client rather than
> assuming it's deliberate.

`DELETE` on a garment is a plain 409 if a SKU or price still references it
(no 405 override here, unlike SKUs and prices themselves).

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/catalog/garments/{id}/prices/` | This garment's full price history, newest first |
| `POST` | `/api/catalog/garments/{id}/reprice/` | **The correct way to change a price.** Closes the current open-ended price on `active_from` and opens a new one |

```json
{ "unit_price": "28000.00", "active_from": "2027-01-01" }
```

> Repricing **adds history, it never rewrites it**. An invoice raised in
> March still costs out at March's price when reprinted in September.

#### SKUs — F06 (CRUD minus DELETE)

`/api/catalog/skus/` — **read: leads, WH, SCH, FIN (all five roles).**

```json
{ "id": 12, "number": "100015", "garment": 3, "garment_name": "Blue Tunic (PS)",
  "size": 2, "size_name": "10", "description": "Blue Tunic Blue size 10 (PS)",
  "is_active": true, "unit_price": "30000.00" }
```

> **`number` is read-only.** It is assigned by the system, never reused, and
> printed on pick lists — sending one on create is ignored.
>
> **SKUs cannot be deleted** (405). Set `is_active: false` to retire one.
>
> **`unit_price` reads through to the garment.** A SKU has no price of its
> own, because price does not vary by size.

Filter `?garment=` `?size=` `?is_active=` `?garment__school_level=`;
search `?search=` over number and description.

#### Minimum stock levels — F07 (full CRUD)

`/api/catalog/minimum-stock-levels/` — **read: leads, WH.**

```json
{ "id": 1, "sku": 12, "sku_number": "100015",
  "sku_description": "Blue Tunic Blue size 10 (PS)",
  "warehouse": 2, "warehouse_name": "Namayemba", "minimum_quantity": 120 }
```

One row per SKU per warehouse. Filter `?warehouse=` `?sku=`. `DELETE` is a
plain 409 if referenced (nothing currently protects against removing a
floor, so this should always succeed cleanly).

### Pricing — F08

#### `/api/catalog/prices/` (CRUD minus DELETE) — read: leads, SCH, FIN

The whole pricing table, for corrections and auditing.

```json
{ "id": 1, "garment": 1, "garment_name": "White Shirt",
  "unit_price": "25000.00", "active_date": "2026-01-01",
  "expiration_date": "2027-01-01" }
```

> **`expiration_date` is exclusive** — the first day the price *no longer*
> applies. A price running `2026-01-01 → 2027-01-01` covers all of 2026 and
> none of 2027. This makes consecutive prices meet exactly instead of
> overlapping on the changeover day.

**Prices cannot be deleted** (405). Correct a mistaken row with `PATCH` —
deleting one would silently rewrite what a past invoice reprints at.

Two prices covering the same day for one garment are **impossible** — the
database refuses them. You get a 400:

```json
{ "non_field_errors": ["This garment already has a price covering part of that period. Close the existing price first."] }
```

#### Price list reports (`GET`-only) — read: all five roles

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/catalog/price-lists/?level=PS\|HS&on=` | The published price list for one level on a date (F15/F51) |
| `GET` | `/api/catalog/price-lists/gaps/?on=&level=` | Active garments with **no** price on that date — run this before publishing a list, or a garment silently disappears from what schools can order. **Leads and Finance only** |

```json
[
  { "garment_id": 3, "garment": "Blue Tunic (PS)", "colour": "Blue", "unit_price": "30000.00" },
  { "garment_id": 7, "garment": "Socks", "colour": "White", "unit_price": "5000.00" }
]
```

Garments marked `BOTH` appear on each list. Unpriced garments are **omitted,
not shown at zero** — a line with no price is worse than no line.

> **School Staff cannot pass `?level=`** on this endpoint — they're forced
> to their own school's level, and asking for the other level is a 403, not
> a silent correction to the right one.

### Kits — F09 (full CRUD, both resources)

| Path | Does | Read |
|---|---|---|
| `/api/catalog/kits/` | The kit shell — `kit_number`, `name`, `school_level`, computed `current_price` (sum of components) and `item_count` | leads, SCH, FIN |
| `/api/catalog/kit-items/` | The bill-of-materials lines — `kit`, `sku`, `quantity`. A **separate resource**, not nested under kit writes | leads, SCH, FIN |

`kit-items` rejects a retired SKU or a school-level mismatch at creation.
Deleting a `Kit` **cascades** to its `KitItem` rows with no 409 — the one
deliberate exception to this codebase's `PROTECT`-everywhere default,
because a bill-of-materials line has no meaning apart from its kit.

---

## `inventory` — 19 operations

### Reports (`GET`-only)

| Method | Path | Does | Read |
|---|---|---|---|
| `GET` | `/api/inventory/stock-levels/` | F47: units on hand per SKU/warehouse, summed live from the ledger — there is no stored quantity column. `?warehouse=` `?as_of=` `?include_zero=` | leads, WH (own warehouse only), FIN |
| `GET` | `/api/inventory/reorder-alerts/` | F50: SKUs at or below their warehouse's minimum. `?warehouse=` `?as_of=` | leads, WH (own warehouse). **Not Finance** |

> F47 and F50 deliberately disagree on whether Finance can read — worth
> confirming that's intentional rather than an oversight, since they answer
> closely related questions.

### Stock movement ledger — F48 (read-only)

| Method | Path | Does | Read |
|---|---|---|---|
| `GET` | `/api/inventory/movements/` | The append-only audit trail. `?sku=` `?warehouse=` `?movement_type=` `?document_number=` | leads (all warehouses), WH (own warehouse only), FIN (all warehouses) |
| `GET` | `/api/inventory/movements/{id}/` | One ledger row | same |

No write route exists at all — `StockMovement` refuses `save()` on an
existing row and refuses `delete()` outright at the model level. The only
way a row is ever written is as a byproduct of posting a receipt, transfer,
or adjustment.

### Reason codes — F13 (CRUD minus PUT/DELETE)

`/api/inventory/reason-codes/` — Return, Warehouse Transfer, Pick up or
Loss, Damaged, and the two count-correction codes, "and others as needed."

| Method | Read | Write |
|---|---|---|
| `GET` (list, detail, `?is_active=`, `?search=`) | leads, FIN | — |
| `POST`, `PATCH` | — | leads only (Finance can read this table but not maintain it) |

No `PUT`, no `DELETE` — retire with `PATCH {"is_active": false}`; a past
adjustment still points at the code that was used to make it.

### Warehouse transfers — F25 (create + post, no PUT/DELETE)

`/api/inventory/transfers/` — stock moving between the two warehouses, no
money moving.

| Method | Path | Does | Read | Write |
|---|---|---|---|---|
| `GET` | `/transfers/` | List. `?from_warehouse=` `?to_warehouse=` `?posted_at=` | leads, FIN. **Not Warehouse Staff** — a transfer touches two sites, and a clerk only sees one | — |
| `POST` | `/transfers/` | Draft a transfer — does **not** move stock yet. Refused up front if the source warehouse doesn't hold enough | — | leads, FIN (same roles as read — no extra gate on who may draft) |
| `GET` | `/transfers/{id}/` | Detail | leads, FIN | — |
| `PATCH` | `/transfers/{id}/` | Header-only edit (from/to warehouse, date, reason code, notes) — `lines` is fixed once created | — | leads, FIN |
| `POST` | `/transfers/{id}/post-to-ledger/` | Commits: two ledger rows at the same value, one out of the source, one into the destination. Re-checks stock at post time too, since time passes between drafting and posting | — | leads, FIN (same role gate as create) |

### Inventory adjustments — F23, F26, F27 (create + post, no PUT/DELETE)

`/api/inventory/adjustments/` — the one document type that Returns and
Damages both reuse unchanged, just with a different reason code.

| Method | Path | Does | Read | Write |
|---|---|---|---|---|
| `GET` | `/adjustments/` | List. `?warehouse=` `?sku=` `?reason_code=` `?posted_at=` | **Finance only** — not even leads. See open question Q3 in the client's own pack: the access matrix gives adjustments to Finance alone, which is coded exactly as specified | — |
| `POST` | `/adjustments/` | Draft an adjustment — does **not** touch the ledger yet. Refused if the SKU has no price on the date, or a decreasing code would take stock below zero | — | Finance only |
| `GET` | `/adjustments/{id}/` | Detail | Finance only | — |
| `PATCH` | `/adjustments/{id}/` | Edit any draft field (warehouse, SKU, quantity, reason code, date, notes) | — | Finance only |
| `POST` | `/adjustments/{id}/post-to-ledger/` | Commits: one ledger row, sign taken from the reason code's direction, valued at the SKU's price **on the adjustment date**, re-looked-up at post time in case a reprice happened in between | — | Finance only |

### Physical count correction — F24 (custom action, not a document type)

| Method | Path | Does | Write |
|---|---|---|---|
| `POST` | `/api/inventory/adjustments/correct-count/` | The actual F24: compares a physical count to the system's figure and posts the **difference** itself — the caller supplies only what was counted, not a signed quantity. Posts immediately, no separate create/post step. Returns 201 with the new adjustment, or 200 with `"adjustment": null` if the count matched exactly | Finance only |

```json
{ "warehouse": 1, "sku": 42, "counted_quantity": 520, "adjustment_date": "2026-11-16" }
```

> **Known risk**: this looks up the `CORR_UP`/`CORR_DOWN` reason codes with
> `ReasonCode.objects.get(code=...)` — no `is_active` filter, and no handling
> if the code is missing. A database seeded only by migrations (no
> `seed_demo`) has neither code yet, so the first count correction anyone
> runs on it would raise an unhandled 500 rather than a clear error.

---

## `orders` — 11 operations

All under `/api/orders/school-orders/`, plus one report.

| Method | Path | Does | Read | Write |
|---|---|---|---|---|
| `GET` | `/school-orders/` | List. `?status=` `?order_date=` `?search=` (number, student name) | SCH (own school only), FIN (every school). **Not leads** — see below | — |
| `POST` | `/school-orders/` | F30–33: place an order. Kit lines explode to their component SKUs at write time; every price is locked at the order date; lands on **Hold** | — | SCH only — the school is taken from the clerk's own account, never from the request body |
| `GET` | `/school-orders/{id}/` | Detail | SCH (own school), FIN (any school) | — |
| `PATCH` | `/school-orders/{id}/` | Edits `student_name`/`order_date`/`notes` only — a plain field edit, not a modelled feature. `status`, `school`, and the cancellation fields are read-only here | — | SCH only |
| `GET` | `/school-orders/{id}/demand/` | One row per SKU the warehouse must pick, summed across kit and individual lines | SCH (own school), FIN (any school) | — |
| `GET` | `/school-orders/{id}/availability/` | F37: needed vs. available vs. shortfall per SKU at the order's own warehouse. Reserves nothing | leads, WH (own warehouse). **Not the school that placed the order, not Finance** | — |
| `GET` | `/school-orders/{id}/pick-list/` | F38: printable pick sheet — the same underlying data as `demand/`, gated to a different audience | leads, WH (own warehouse) | — |
| `POST` | `/school-orders/{id}/pick/` | F39: reserves stock per line, Available → Picked. Refused if any line is short, or the order is cancelled/already picked/shipped | — | leads, WH (own warehouse) |
| `GET` | `/school-orders/{id}/invoice/` | F34: the order as a document a school hands to a parent — kit lines regrouped with subtotals | SCH (own school), FIN (any school) | — |
| `POST` | `/school-orders/{id}/cancel/` | F36: withdraw an unpaid order — only while still on Hold. Records who, when, and why | — | SCH only |
| `GET` | `/api/orders/reports/on-hold/` | F53: raised-but-unpaid invoices, oldest first | leads, FIN (every school), SCH (own school only) | — |

`PATCH` and the two `pick`/`cancel` actions are the only writes; `PUT` and
`DELETE` are both 405 — a school order is withdrawn with `cancel/`, never
deleted, because the school has already handed the number to a parent.

> **Program Lead and Operations Manager cannot list or retrieve a raw
> school order at all.** Their only visibility into this resource is the
> F53 on-hold report and the two warehouse-fulfilment actions
> (`availability/`, `pick-list/`, `pick/`) — `/school-orders/` and
> `/school-orders/{id}/` themselves are School-Staff-and-Finance only.
>
> **`demand/` and `pick-list/` return identical data** (the same
> `order_demand()` call underneath) but are gated to two disjoint
> audiences — School Staff/Finance on one, leads/Warehouse Staff on the
> other. Worth confirming with the client whether that split is intentional
> or these were meant to be the same endpoint.

---

## `procurement` — 18 operations

### Group orders — F16 (create + header-amend, no PUT/DELETE)

`/api/procurement/group-orders/` — the consolidated requirement across all
three Tailoring Centers.

| Method | Path | Does | Read | Write |
|---|---|---|---|---|
| `GET` | `/group-orders/` | List. `?status=` `?order_date=` | leads, FIN. **Not Warehouse or School Staff** | — |
| `POST` | `/group-orders/` | Raise a group order with its lines in one transaction. A line without an explicit price copies the garment's current price and fixes it there | — | leads only |
| `GET` | `/group-orders/{id}/` | Detail | leads, FIN | — |
| `PATCH` | `/group-orders/{id}/` | F18: header-only amend (`status`, `due_in_warehouse_date`, `notes`) — sending `lines` is rejected outright rather than silently ignored. This is also how a group order is **cancelled**: there is no separate cancel action, just `{"status": "CANCELLED"}` | — | leads only |
| `GET` | `/group-orders/{id}/reconciliation/` | Group-order lines vs. the sum of its production orders — reported, not enforced. A negative `difference` means the TCs were asked for less than the requirement | leads, FIN | — |

### Production orders — F17, F18, F22 (create + header-amend, no PUT/DELETE)

`/api/procurement/production-orders/` — one warehouse's order on one TC.

| Method | Path | Does | Read | Write |
|---|---|---|---|---|
| `GET` | `/production-orders/` | List. `?status=` `?tailoring_center=` `?warehouse=` `?group_order=` | leads, FIN (all warehouses), WH (own warehouse only) | — |
| `POST` | `/production-orders/` | Raise one (`group_order` is optional — a reorder or emergency order has none) | — | leads only. Warehouse Staff can *read* their own warehouse's orders but cannot create or amend them |
| `GET` | `/production-orders/{id}/` | Detail | leads, FIN, WH (own warehouse) | — |
| `PATCH` | `/production-orders/{id}/` | F18: same header-only amend/cancel mechanism as group orders | — | leads only |
| `GET` | `/production-orders/open/` | F22: orders not yet closed. Currently means status = Open only — nothing auto-closes an order once fully received, so this can list an order everyone considers done | leads, FIN, WH (own warehouse) | — |
| `GET` | `/production-orders/{id}/outstanding/` | Ordered minus received (posted receipts only), per SKU | leads, WH (own warehouse). **Finance excluded** — this one endpoint uses the receiving permission class rather than the master-data one every sibling above uses | — |

### Receipts — F19, F20, F21 (create + post, no PUT/DELETE)

`/api/procurement/receipts/` — what arrived, checked against the TC's
handwritten packing list before anything is committed.

| Method | Path | Does | Read | Write |
|---|---|---|---|---|
| `GET` | `/receipts/` | List. `?production_order=` `?posted_at=` | leads, WH (own warehouse). **Finance excluded from the raw list** — its only receipts visibility is the costed report below | — |
| `POST` | `/receipts/` | Record a delivery — does **not** touch stock yet. Every SKU on it must already be on the production order; the server also checks the clerk's own warehouse against the order's warehouse | — | leads, WH (own warehouse) |
| `GET` | `/receipts/{id}/` | Detail | leads, WH (own warehouse) | — |
| `PATCH` | `/receipts/{id}/` | Header-only edit (`packing_list_number`, `date_received`, `notes`, `production_order`) — `lines` is fixed at creation | — | leads, WH (own warehouse) |
| `POST` | `/receipts/{id}/post_to_inventory/` | F21: commits — one ledger row per line, valued at **the production order's agreed price**, not today's price list | — | leads, WH (own warehouse) |

> **Naming inconsistency**: this action is `post_to_inventory` (underscored)
> while the structurally identical actions on transfers and adjustments are
> `post-to-ledger` (hyphenated). Confirmed live in the schema — worth
> normalising if a generated client is ever handed to the frontend team.

```json
{
  "production_order": 4,
  "packing_list_number": "IDUDI-2026-041",
  "date_received": "2026-10-10",
  "lines": [
    { "sku": 12, "quantity_received": 480, "quantity_on_packing_list": 500,
      "discrepancy_note": "20 short — TC ran out of size 10 fabric" }
  ]
}
```

### Finance reports — F55, F56 (`GET`-only)

| Method | Path | Does |
|---|---|---|
| `GET` | `/reports/group-orders-costed/` | F55: what was committed to the TCs, valued at order-time prices. `?from=` `?to=` `?include_cancelled=`; response has a `totals` object alongside `orders` |
| `GET` | `/reports/receipts-costed/` | F56: what each TC actually delivered and its cost — valued at the agreed TC price, counted at what actually arrived, posted receipts only. `?from=` `?to=` `?tailoring_center=` `?warehouse=` `?detail=true` for receipt-level rows |

Read: **leads and Finance only** on both.

---

## Utility

| Path | Purpose |
|---|---|
| `GET /api/` | Directory of the API |
| `GET /api/docs/` | Interactive documentation |
| `GET /api/schema/` | OpenAPI document, for generating a client |
| `/admin/` | Django admin, used by Central Office |

---

## Known oddities worth confirming with the client

None of these are bugs in the sense of "doesn't match the spec" — the spec
(the client's own access matrix, p.9) is ambiguous or silent on each of
them, and the code picked a reading. Listed here so a reading nobody
actually decided doesn't quietly become the reading everybody assumed.

1. **Garment reads are narrower than SKU reads**, even though a SKU exposes
   the exact same price. `/catalog/garments/` is leads-only; `/catalog/skus/`
   is open to Warehouse Staff, School Staff and Finance too.
2. **`GET /auth/roles/` is blocked for a user on a forced password
   change**, unlike `/auth/me/`, `/auth/password/change/`, and
   `/auth/logout/`, which all explicitly allow it. This is reference data a
   "set your password" screen could plausibly want.
3. **`/orders/school-orders/{id}/demand/` and `.../pick-list/` return
   identical data** through two different, non-overlapping audiences
   (School Staff/Finance vs. leads/Warehouse Staff).
4. **Program Lead and Operations Manager cannot read the school-orders
   resource at all** — only the on-hold report and the fulfilment actions.
5. **`/inventory/stock-levels/` and `/inventory/reorder-alerts/` disagree on
   Finance access** (the first allows it, the second doesn't) despite
   answering closely related questions.
6. **`/procurement/production-orders/{id}/outstanding/` excludes Finance**
   while every other production-order endpoint allows it — it uses the
   receiving permission class rather than the master-data one its siblings
   use.
7. **`post_to_inventory` vs. `post-to-ledger`** — the same "commit this
   draft document" action is spelled two different ways depending on the
   app.
8. **`POST /inventory/adjustments/correct-count/`** raises an unhandled 500
   on a database where `CORR_UP`/`CORR_DOWN` haven't been seeded or have
   been deactivated — see the note under F24 above.

---

## Feature coverage

Endpoints existing is not the same as a feature being finished. This tracks
against the client's F01–F63 checklist; "done" below means the backend
behaviour is complete, not that any frontend screen exists — **there is no
frontend in this repository to evaluate.**

**Done**: F01–F17, F19–F21, F23, F25–F34, F36–F39, F47, F48, F50, F51, F53,
F55, F56, F61 (no offline entry exists, matching the requirement).

> **F04 (user recorded on every transaction) is a correction, not a
> reshuffle.** The previous version of this file marked it incomplete,
> waiting on "the inventory ledger, which does not exist yet." That ledger
> exists now — `created_by` is a required, `PROTECT`ed field on every
> transactional model (`StockMovement`, `InventoryAdjustment`,
> `WarehouseTransfer`, `Receipt`, `GroupOrder`/`ProductionOrder`,
> `SchoolOrder`) — so F04 has moved from "not complete" to "done."

**Partial**: F18 (header amend only, no line editing — "Should", not
"Must"), F22 (nothing auto-closes a fully-received order), F24 (correct
logic, but see the reason-code risk above), F60/F62/F63 (backend-ready,
no frontend to render them).

**Not built at all**: F09's ordering path exists but F35 (Hold → Released,
blocked on the client's own open question about what "School Monitor"
is), F40 (packing list print), F41 (Pick → Shipped — deliberately absent,
tied to the client's own "Shipped ???" chart), F42 (weekly shipment
batching), F43–F46 (the entire backorder subsystem — a permission class
exists for it, nothing is wired to it), F49, F52, F54, F57, F58 (costed
adjustments report) do not exist. F59 (branding) is unverifiable with no
frontend present.
