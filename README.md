# The ePIC Hardware Database

This is a Django implementation of the Hardware Database inspired by *The Component
Database User Guide* (Argonne National Laboratory). The Hardware Database is the
candidate central repository for documenting, organizing, and tracking components used
in ePIC and EIC project.

---

## Table of Contents

1. [Overview](#overview)
2. [Project Structure](#project-structure)
3. [Quick Start](#quick-start)
4. [Data Model](#data-model)
   - [Institutions and Locations](#institutions-and-locations)
   - [Domain 1 — Component Catalog](#domain-1--component-catalog)
   - [Domain 2 — Component Inventory](#domain-2--component-inventory)
   - [Domain 3 — Designs](#domain-3--designs)
   - [Cross-Domain: Properties and Logs](#cross-domain-properties-and-logs)
   - [Ownership](#ownership)
5. [Django Admin](#django-admin)
6. [Web UI](#web-ui)
7. [Python Client, CLI, and MCP Server](#python-client-cli-and-mcp-server)
8. [Seed Data](#seed-data)
9. [Schema Diagram](#schema-diagram)
10. [Design Decisions](#design-decisions)

---

## Overview

This database captures three interrelated domains:

| Domain | Purpose |
|--------|---------|
| **Component Catalog** | Reference library of every component *type* — custom-fabricated or commercial — with metadata, drawings, vendors, and properties. |
| **Component Inventory** | Physical instances of catalog items, each with a unique tag, tracked to a specific room, cabinet, or shelf at a specific institution. |
| **Design Library** | Bill-of-Materials groupings: named assemblies of components and sub-assemblies, with hierarchical nesting and installed-instance tracking — plus reusable **Design Templates** for planning an assembly before any of it physically exists. |

A flexible **Properties** system attaches arbitrary typed metadata to any
domain item. A unified **Log** system records maintenance, inspection, and
lifecycle events across all domains.

---

## Project Structure

```
epic-hdb/
├── manage.py
├── hdb_project/                 # Django project (settings, root URLconf)
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
├── hdb/                         # The "hdb" Django app — all models, views, admin
│   ├── models.py                # All data models
│   ├── admin.py                 # Django admin configuration
│   ├── views_web.py             # Server-rendered web UI views
│   ├── urls_web.py              # Web UI URL routes
│   ├── views.py / serializers.py  # Optional REST API (only if djangorestframework is installed)
│   ├── migrations/
│   │   └── 0001_initial.py
│   ├── management/commands/
│   │   └── seed_hdb.py          # Sample data loader
│   └── templates/cdb/           # Web UI templates — NOTE: this directory is still
│                                 # named "cdb" (pre-dating the project's cdb→hdb
│                                 # rename); the app itself is fully "hdb", only this
│                                 # template subdirectory's name is a known, pending
│                                 # cleanup, not a functional issue.
├── client/                      # hdb_client library, CLI, and MCP server — see client/README.md
│   ├── hdb.py                   # Command-line interface
│   ├── mcp_server.py            # MCP server (for AI assistants / MCP clients)
│   ├── smoke_test.py            # End-to-end test for the MCP server
│   ├── requirements.txt
│   ├── README.md                # Full client/CLI/MCP reference — usage mechanics live here
│   └── hdb_client/              # Programmatic query client (package)
│       ├── client.py            # HDBClient (combined entry point)
│       ├── catalog.py           # CatalogClient
│       ├── inventory.py         # InventoryClient
│       ├── designs.py           # DesignClient (designs + design templates)
│       ├── locations.py         # LocationClient
│       ├── systems.py           # SystemClient
│       ├── access.py            # Ownership/permission checks shared by all writes
│       └── serializers.py       # Plain-dict output shaping (no DRF dependency)
├── data/                        # Sample input files for the client (YAML)
│   ├── new_crystal.yaml
│   └── btof_stave_templates.yaml
└── assets/
    ├── docs/The_Legacy_Component_Database_User_Guide.pdf   # The original ANL inspiration
    └── images/                  # Reference photos used by seed_hdb.py
```

---

## Quick Start

**Requirements:** Python 3.10+, Django 4.x or 5.x, plus `qrcode[pil]` (the
web UI's inventory QR-code page needs it — everything else only needs Django).

```bash
pip install django "qrcode[pil]"

# Apply all migrations (creates db.sqlite3):
python manage.py migrate

# Load sample ePIC detector data:
python manage.py seed_hdb

# Start the development server:
python manage.py runserver

# Web UI:
#   http://127.0.0.1:8000/           (login, then Dashboard)
# Admin interface:
#   http://127.0.0.1:8000/admin/
# Either way, log in as:
#   Username: admin   Password: admin
#   (or: maxim / maxim — also a superuser, see Seed Data below)
```

To use the Python client from the Django shell, add `client/` to the path first:

```bash
PYTHONPATH=client python manage.py shell
```

```python
from hdb_client import HDBClient
client = HDBClient()

# Find where a component is located (pass a UUID primary key)
client.where_is("5a2c5c0e-479b-4e2f-a7cb-caea37435506")

# Get a full Bill of Materials
client.designs.bom("BEMC tower")
```

See [Python Client, CLI, and MCP Server](#python-client-cli-and-mcp-server)
below for where to go for the complete reference.

---

## Data Model

### Institutions and Locations

**`Institution`** is the top-level geographic anchor, representing a
collaborating lab or facility (e.g. BNL, CUA, UIC). Every location
must belong to an institution, enabling inventory tracking across multiple
sites.

| Field | Description |
|-------|-------------|
| `name` | Full name (unique) |
| `abbreviation` | Short code, e.g. `BNL` |
| `country` / `city` | Site geography |
| `url` | Homepage |

**`Location`** represents a physical place within an institution, organized
in a self-referential hierarchy: Building → Room → Cabinet → Shelf → Other.

| Field | Description |
|-------|-------------|
| `name` | Location name |
| `location_type` | `building`, `room`, `cabinet`, `shelf`, `other` |
| `institution` | FK to owning Institution (required — deleting an Institution with Locations still attached is blocked, not cascaded) |
| `parent` | FK to parent Location (self-referential) |

`Location.full_path()` (used by `__str__`) returns the full slash-separated
path, e.g.: `BNL / Bldg,510A`.

Every `User` also has a **`UserProfile`** with a mandatory `institution` FK —
a user's home institution, used to group "who's at which site" (e.g. the
`users_at_institution()` client method).

---

### Domain 1 — Component Catalog

**`Component`** — one entry per unique component *type*.

| Field | Description |
|-------|-------------|
| `name` | Unique together with `project` |
| `alternate_name` | Optional secondary name |
| `model_number` | Vendor or internal model number |
| `description` | Free-text description |
| `project` | e.g. `ePIC` (default) |
| `technical_system` | FK → `TechnicalSystem` |
| `sources` | M2M → `Source` via `ComponentSource` |
| *(OwnedModel)* | Ownership + timestamps — see [Ownership](#ownership) |

**`TechnicalSystem`** — an engineering subsystem (e.g. `BEMC-CRYSTAL`,
`BTOF-Sensor`). Acts as a chapter in the catalog, and carries its own `group`
FK (a Django `Group`) identifying the team responsible for it —
`ComponentInstance` inherits its `technical_system` from its `Component`
automatically if not set explicitly.

`TechnicalSystem.name` and `Group.name` are independent, unrelated strings —
there's no naming convention or code that ties them together, and nothing
requires a `TechnicalSystem` to be named after its `group` (or vice versa).
This repo's seed data just happens to *look* related because it follows an
informal `<GROUP>-<ROLE>` convention (`BEMC-CRYSTAL`/`BEMC-PM` → group
`BEMC`; `BTOF-Sensor`/`BTOF-Readout` → group `BTOF`; `PFRICH-HRPPD` → group
`PFRICH`) purely for human readability. The actual link between the two
models is only the one nullable FK, `TechnicalSystem.group`, set once at
creation — see [Domain 1](#domain-1--component-catalog) above and the
Django-admin note below for how that field is (and isn't) kept in sync.

**`Source`** — vendor or manufacturer. The `ComponentSource` through-table
adds `part_number`, `cost`, and `role` (`vendor` / `manufacturer` / `both`)
per (component, source) pair.

---

### Domain 2 — Component Inventory

**`ComponentInstance`** — one row per physical item.

| Field | Description |
|-------|-------------|
| `tag` | Human-readable label |
| `serial_number` | Vendor serial number |
| `component` | FK → `Component` (catalog type) — `PROTECT`ed, so a Component with instances can't be deleted out from under them |
| `technical_system` | FK → `TechnicalSystem`, auto-inherited from `component` if left unset |
| `location` | FK → `Location` (where it currently is) |
| *(OwnedModel)* | Ownership + timestamps |

Each instance inherits all catalog-level properties from its parent
`Component`, and may additionally carry its own instance-specific properties
that override the inherited default for the same `(property_type, tag)` pair
(`ComponentInstance.effective_properties()` merges the two).

---

### Domain 3 — Designs

This is the most structurally interesting part of the schema, because it's
actually **two related but distinct models** — mixing up their capabilities
is the easiest way to be surprised by this part of the database. This
section is the authoritative explanation; `client/README.md` covers the
`hdb_client`/CLI *mechanics* for working with both and links back here for
the *why*.

#### `Design` — a real, buildable assembly

| Field | Description |
|-------|-------------|
| `name` | Unique design name |
| `description` | Functional description |
| `project` | e.g. `ePIC` |
| `template` | FK → `DesignTemplate`, nullable — set if this design was instantiated from one |
| `location` | FK → `Location`, nullable — a design lives in exactly one place; placeholder-filling only offers inventory stored there |
| *(OwnedModel)* | Ownership + timestamps |

**`DesignElement`** — one slot within a design.

| Field | Description |
|-------|-------------|
| `element_name` | Unique within the design |
| `component` | FK → `Component`, nullable — set for a leaf element |
| `child_design` | FK → `Design`, nullable — set for a sub-assembly element (**real nesting**, arbitrarily deep) |
| `quantity` | Number of this element required (applies to either kind) |

Exactly one of `component` or `child_design` is set per element
(`element_type()` returns `"COMPONENT"` or `"DESIGN"` based on which).

**`DesignElementInstance`** — a separate join table, *not* a field on
`DesignElement`: one row per physical `ComponentInstance` installed into one
slot. A `DesignElement` with `quantity=4` (e.g. "SiPM x 4") can accept up to
four of these, each pointing at a distinct instance. A `ComponentInstance`
can be installed in at most one slot anywhere in the whole database at a
time — enforced by a database-level `unique` constraint on `instance`, not
just a UI check.

#### `DesignTemplate` — a reusable blueprint

| Field | Description |
|-------|-------------|
| `name` | Globally unique |
| `description` | Free-text |
| `project` | e.g. `ePIC` |
| *(OwnedModel)* | Ownership + timestamps |

**`DesignTemplateElement`** — one placeholder line.

| Field | Description |
|-------|-------------|
| `element_name` | Unique within the template |
| `component` | FK → `Component`, **required** — a template placeholder always names a component *type*, never a specific instance |
| `quantity` | Number needed |

#### The key structural difference: templates can't nest

`DesignElement` can point at another `Design` (`child_design`) — real,
unlimited-depth nesting is native to the schema. `DesignTemplateElement`
has **no equivalent `child_template` field**: its `component` FK is required
and always points at a `Component`, never at another `DesignTemplate`. A
template can only ever say "this slot needs N of component X" — never "this
slot needs another sub-template."

| | `Design` | `DesignTemplate` |
|---|---|---|
| Represents | A real assembly, instantiated or in progress | A reusable pattern, not a design itself |
| Element model | `DesignElement` | `DesignTemplateElement` |
| An element can point at | A `Component` **or** another `Design` | A `Component` **only** |
| Nests its own kind? | Yes, natively (`child_design`) | No — no schema field for it |
| Physical tracking | `DesignElementInstance` pins real `ComponentInstance`s into slots | None — always abstract |
| Locking | None of its own | **Locked** once any `Design` references it as `template` |
| Who can delete it | Any `owner_group` member, or a superuser | **Superuser only**, and only while unlocked |

**The workaround**, used throughout `data/btof_stave_templates.yaml`: model
each intermediate assembly level as **both its own catalog `Component` and
its own `DesignTemplate` of the identical name**. Any BOM-style traversal
(the web UI's template BOM, or `hdb_client`'s `template_bom()`) can then
follow that name match to recurse — for each element, it checks whether a
`DesignTemplate` exists whose `name` equals the element's `component.name`,
and if so, descends into it. This achieves the same effect as `child_design`
nesting, through a naming convention instead of a schema field:

```
DesignTemplate "BTOF Stave"
  └─ element "Half-Stave Assemblies"  x144  → Component "BTOF Half-Stave"
       │  (a DesignTemplate of the same name exists, so traversal recurses)
       └─ DesignTemplate "BTOF Half-Stave"
            ├─ element "Stavelet Modules"  x8  → Component "BTOF Stavelet"
            │    └─ DesignTemplate "BTOF Stavelet"
            │         ├─ "AC-LGAD Sensors"    x4 → Component "AC-LGAD Sensor"        (leaf)
            │         ├─ "FCFD Readout ASICs" x4 → Component "FCFDv2 Readout"        (leaf)
            │         ├─ "Interposer Boards"  x4 → Component "BTOF Interposer Board" (leaf)
            │         └─ "Flex Cable (FPC)"   x1 → Component "BTOF Flex Cable (FPC)" (leaf)
            ├─ "Peripheral Board"      x1 → Component "BTOF Peripheral Board"       (leaf)
            └─ "Cooling Pipe Segment"  x1 → Component "BTOF Cooling Pipe Segment"   (leaf)
```

A component is a "leaf" only because no `DesignTemplate` happens to share its
name — there's no explicit flag; it's inferred fresh on every traversal.
This has a real benefit: **every intermediate assembly (a stavelet, a
half-stave, a stave) is independently serial-trackable as its own
`ComponentInstance`** once actually built, same as any leaf part — a schema
that just added a bare `child_template` field wouldn't give you that, since
an abstract sub-template reference has nothing to attach a serial number to.

#### Instantiation and locking

A `DesignTemplate` starts **unlocked**: any member of its `owner_group` (or
a superuser) can add, remove, or resize its placeholders. Instantiating it —
via "New from Template" on the Designs page, or programmatically — creates a
real `Design` with `template` set, and one `DesignElement` copied from each
`DesignTemplateElement` at that moment.

The instant **any** `Design` exists with that `template`, the template
**locks**: every editing and deletion control disappears, checked
server-side (`template.designs.exists()`), not just hidden in the UI. This
guarantees "instantiated from BEMC tower" always means the same bill of
placeholders, no matter when you look. The lock isn't a stored flag, so it's
automatically reversible — delete the last `Design` built from a template
and it becomes editable (and deletable) again immediately. `Design` deletion
itself carries none of these restrictions.

**Deleting a template** is further restricted to **superusers only** (even
more restrictive than editing, which any `owner_group` member can do) —
because removing a template removes it for every group member at once. See
`client/README.md` for the exact CLI/API/web-UI mechanics and real output.

One nuance worth internalizing: deleting an **intermediate-level** template
in a multi-level set (e.g. "BTOF Stavelet" while "BTOF Half-Stave" and "BTOF
Stave" still exist) is not an error. The next BOM traversal simply finds no
matching `DesignTemplate` for that element and treats it as a leaf instead —
the parent template stays otherwise intact, just one level shallower at that
branch. Re-loading the original YAML (idempotent) restores the deleted level
exactly and re-enables the recursion.

---

### Cross-Domain: Properties and Logs

**`PropertyValue`** — flexible key/value metadata attachable to any domain
item. A single table serves all four targets via optional FK columns.

| Field | Description |
|-------|-------------|
| `property_type` | FK → `PropertyType` (predefined, admin-extensible) |
| `tag` | Optional label for this value |
| `value` | String value |
| `file` | Optional uploaded file (for `document`/`image` handler types) |
| `units` | Optional unit string |
| `is_dynamic` | True if value varies per instance (e.g. an inspection result) |
| `component` / `component_instance` / `design` / `design_element` | Target FK — exactly one is set |

**`PropertyType`** defines the schema for a property. The `handler` field
specifies typed behaviour:

| Handler | Behaviour |
|---------|-----------|
| `pdmlink` | Integrates with PDMLink engineering drawing system |
| `component_design` | Links to a component design document |
| `traveler_template` | Links to an eTraveler inspection template |
| `traveler_instance` | Links to a filled-out eTraveler form |
| `document` | Attach any file |
| `image` | Attach a viewable image (shown in gallery) |
| `http_link` | Store a URL |
| `currency` | Numeric value with `#.##` formatting |
| `boolean` | True/false checkbox |
| `date` | Date picker |

**`LogEntry`** — lifecycle event log, attachable to Component,
ComponentInstance, or Design.

| Field | Description |
|-------|-------------|
| `topic` | `general` (blank), `installation`, `inventory`, `design`, `maintenance`, `inspection`, `repair`, `decommission`, `other` |
| `entry` | Free-text log message |
| `attachment` | Optional file upload |
| `logged_by` | FK → Django User |
| `timestamp` | Auto-set on creation |

There's a `/logs/` page (`log_list`) that browses and filters all of them,
and each Component/Instance/Design detail page shows its own via
`related_name="log_entries"`. As of this writing there's no "Add Log Entry"
form anywhere in the web UI — `log_list` is read-only, and a handful of
actions (e.g. deleting an inventory item) create a `LogEntry` automatically
as a side effect — so a free-form entry currently has to go through the
Django admin or direct ORM/client access.

---

### Ownership

Every domain model that can be written to (`Component`, `ComponentInstance`,
`Design`, `DesignTemplate`) inherits from the abstract `OwnedModel`, which
supplies:

| Field | Description |
|-------|-------------|
| `owner_user` | Individual owner (Django User) |
| `owner_group` | Owning group (Django `Group`) |
| `group_writeable` | Whether group members can edit |
| `created_by` / `created_on` | Creation audit trail |
| `modified_by` / `modified_on` | Modification audit trail |

**`Group`** — a named team or subsystem (this repo's seed data uses `BEMC`,
`BTOF`, and `PFRICH`). A user may write to a row if they're its
`owner_user`, they're Django staff/superuser, or they're a member of
`owner_group` *and* `group_writeable` is `True` — see
`client/hdb_client/access.py` for the exact rule, shared by every write
path (web UI, client library, CLI, and MCP server alike).

---

## Django Admin

The admin interface at `/admin/` provides full CRUD access to every model:

- **Institution** pages include an inline table of all their locations.
- **Component** pages include inline tables for sources, properties,
  inventory instances, and log entries, plus a computed instance count.
- **ComponentInstance** pages include inline properties and logs, plus an
  Institution column in the list view for quick site identification.
- **TechnicalSystem** and **Source** have their own simple list/search pages.
  A `TechnicalSystem`'s `group` field is a plain dropdown of existing
  `Group`s (no free-text entry), but the "+" icon next to it opens a popup
  to create a new `Group` on the spot without leaving the page.
- **DesignTemplate** pages include an inline table of placeholders
  (`DesignTemplateElement`) and a computed placeholder count.
- **Design** pages include inline design elements, properties, and logs,
  plus a computed element count.
- **DesignElement** pages include inline properties and inline installed
  instances (`DesignElementInstance`).
- **User** admin is customized to show institution and group columns, with
  an inline `UserProfile` (institution) editor.
- All list views support filtering and search.

---

## Web UI

A server-rendered web UI (separate from `/admin/`) lives at `/`, built from
`hdb/views_web.py` + `hdb/urls_web.py`. After logging in at `/`, it offers:

| Page | What it's for |
|------|----------------|
| Dashboard | At-a-glance counts across all domains |
| Components | Catalog browsing/search, add properties, create instances, transfer ownership |
| Inventory | Instance browsing/search, QR code page, delete (if not installed in a design), transfer ownership, update location/identifiers |
| Design Templates | Browse, create, edit placeholders, and (superuser) delete unlocked templates |
| Designs | Browse, "New from Template", assign/unassign installed instances, update location, delete |
| Systems | Technical systems with component/instance counts |
| Institutions | Institution list |
| Users | User list with institution/group info |
| Logs | Read-only browse/filter over every `LogEntry` |

Every write action goes through the same ownership rule described in
[Ownership](#ownership) above — group membership, `owner_user`, or
staff/superuser status — enforced server-side on every POST, not just by
hiding buttons in the template.

---

## Python Client, CLI, and MCP Server

`client/` holds three thin, interchangeable ways to talk to HDB
programmatically, all built on the same `hdb_client` query/write layer:

| Layer | File(s) | Use it when… |
|---|---|---|
| Python library | `client/hdb_client/` | Writing a script or Django-adjacent tool; direct ORM-speed access, in-process |
| CLI | `client/hdb.py` | Querying/updating from a terminal or shell script |
| MCP server | `client/mcp_server.py` | Letting an AI assistant or other MCP client query/update HDB over HTTP |

A minimal example (see [Quick Start](#quick-start) above for the full
bootstrap needed outside `manage.py shell`):

```python
from hdb_client import HDBClient
client = HDBClient()                    # or HDBClient(user=some_django_user) for writes

client.search_all("Crystal")
client.where_is(instance_pk)
client.designs.bom("BEMC tower")
client.designs.template_bom("BTOF Stave")   # see Domain 3 above for what this recurses through
```

`HDBClient` aggregates six sub-clients (`locations`, `systems`, `catalog`,
`inventory`, `designs`) plus the two cross-domain helpers shown above.

**For the complete method-by-method reference, the CLI command list
(including `load-template`/`bom-template`/`delete-template` for working
with Design Templates), the YAML formats, and MCP server setup/auth/tools —
see [`client/README.md`](client/README.md).** That document is the
authoritative usage reference for all three layers; this section is
intentionally just an orientation, kept short so it can't drift out of sync
with the client code the way a duplicated method list would.

---

## Seed Data

`python manage.py seed_hdb` loads a realistic ePIC detector dataset
(verified against the current schema — counts below are exact, not
approximate):

| Object | Count | Examples |
|--------|-------|---------|
| Institutions | 4 | BNL, CUA, UIC, UH (University of Hawaii) |
| Locations | 3 | Storage Room (CUA), Test Lab (UIC), Bldg,510A (BNL) |
| Groups | 3 | BEMC, BTOF, PFRICH |
| Users | 8 | `admin`, `maxim` (superusers); `crafts` (BEMC), `gnigmat` (BTOF), `ottjenni` (BTOF), `ullrich` (BEMC+BTOF), `bpage` (PFRICH), `ayk` (PFRICH) |
| Technical Systems | 5 | BEMC-CRYSTAL, BEMC-PM, BTOF-Sensor, BTOF-Readout, PFRICH-HRPPD |
| Components | 4 | PbWO4 Crystal, Hamamatsu S14160-3010PS, AC-LGAD Sensor, FCFDv2 Readout |
| Instances | 26 | 3 crystals, 16 SiPMs, 2 AC-LGAD sensors, 5 FCFDv2 readout ASICs |
| Design Templates | 1 | BEMC tower (blueprint: 1 crystal + 4 SiPMs) |
| Designs | 1 | BEMC tower (instantiated from its own template) |
| Property Types | 7 | Weight, Datasheet, Timing Constant, Length, Width, Height, Image |
| Sources | 0 | — |

Every account's initial password matches its username (e.g. `crafts`/`crafts`),
set only the first time each account is created. The command is
**idempotent** — safe to run repeatedly, never duplicates or clobbers
existing records (`get_or_create()` throughout).

Separately, `data/btof_stave_templates.yaml` provides a much larger,
multi-level Design Template set (the ePIC BTOF Stave → Half-Stave →
Stavelet hierarchy) — it's **not** loaded by `seed_hdb`, but by
`client/hdb.py load-template`. See
[Python Client, CLI, and MCP Server](#python-client-cli-and-mcp-server)
above and `client/README.md` for how.

---

## Schema Diagram

```
Institution ──► Location (self-FK: building → room → cabinet → shelf)
                    │
                    └──────────────────────────────────────┐
                                                             ▼
Group ──► TechnicalSystem ──► Component ──► ComponentInstance
                                   │                │
                                   ├─► ComponentSource ──► Source
                                   │
                                   ├─► PropertyValue  (FK: component)
                                   ├─► PropertyValue  (FK: component_instance)
                                   ├─► LogEntry        (FK: component)
                                   └─► LogEntry        (FK: component_instance)

DesignTemplate ──► DesignTemplateElement ──► Component
      │   (flat only — no template-to-template nesting; see Domain 3)
      │
      │   instantiate
      ▼
    Design ──► DesignElement ──┬─► Component                 (leaf)
      │             │          └─► Design (child_design)      (real nesting)
      │             │
      │             └─► DesignElementInstance ──► ComponentInstance
      │
      ├─► PropertyValue  (FK: design)          ├─► PropertyValue  (FK: design_element)
      └─► LogEntry        (FK: design)

UserProfile ──► User, Institution
```

---

## Design Decisions

**`OwnedModel` abstract base** — every writable domain model inherits a
consistent set of ownership (`owner_user`, `owner_group`, `group_writeable`)
and audit fields (`created_by/on`, `modified_by/on`), matching the CDB User
Guide's ownership model.

**`Institution` as a first-class model** — rather than a free-text field or
a special location type, `Institution` is its own model with country, city,
and URL. Every `Location` has a mandatory FK to an `Institution`, enabling
`location__institution__abbreviation=...`-style queries across the whole
inventory.

**Single `PropertyValue` table** — four nullable FK columns (`component`,
`component_instance`, `design`, `design_element`) let one table serve all
domains with no duplication of schema, matching the CDB's philosophy that
any object can carry any property.

**`DesignElement` dual-target pattern** — each element sets either
`component` (leaf) or `child_design` (sub-assembly), never both. This
enables unlimited BOM nesting depth for real `Design`s.

**Deliberately flat `DesignTemplate`s, with a naming-convention workaround
for multi-level hierarchies** — rather than adding a `child_template` field
to mirror `child_design`, a template placeholder can only reference a
catalog `Component`. Multi-level template hierarchies are represented by
giving each intermediate assembly level both its own `Component` and its
own identically-named `DesignTemplate` — see [Domain 3](#domain-3--designs)
for the full reasoning, including why this also makes every intermediate
assembly independently serial-trackable, which a schema-level
`child_template` field wouldn't.

**Superuser-only template deletion** — editing an unlocked `DesignTemplate`
is open to any `owner_group` member (matching every other write in the
system), but *deleting* one is superuser-only. Removing a template removes
it for every group member at once, not just the deleter's own records — a
deliberately stricter bar than the usual ownership rule.

**QuerySet-returning client methods** — `hdb_client`'s domain clients return
lazy Django QuerySets wherever possible. Callers can chain additional
filters, annotations, or `values()` calls without re-querying the database.
Methods that return dicts or lists (for JSON-safe API/MCP output) are
explicitly documented as such in `client/README.md`.

---

## Note for testers/developers

To completely reset the database, use these commands:
```bash
# from your project root (where manage.py lives)
rm -f db.sqlite3
rm -rf hdb/migrations
python manage.py makemigrations hdb
python manage.py migrate
python manage.py seed_hdb
```
