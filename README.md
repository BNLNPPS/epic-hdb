# Hardware Database (HDB) — Django Implementation

A Django implementation of the Hardware Database described in *The Component
Database User Guide* (Argonne National Laboratory / ePIC). The CDB is the
central repository for documenting, organizing, and tracking components used
in a particle physics accelerator or detector project.

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
6. [Python Client (`cdb_client`)](#python-client-cdb_client)
   - [LocationClient](#locationclient)
   - [CatalogClient](#catalogclient)
   - [InventoryClient](#inventoryclient)
   - [DesignClient](#designclient)
   - [CDBClient (combined)](#cdbclient-combined)
7. [Seed Data](#seed-data)
8. [Schema Diagram](#schema-diagram)
9. [Design Decisions](#design-decisions)

---

## Overview

The CDB captures three interrelated domains:

| Domain | Purpose |
|--------|---------|
| **Component Catalog** | Reference library of every component *type* — custom-fabricated or commercial — with metadata, drawings, vendors, and properties. |
| **Component Inventory** | Physical instances of catalog items, each with a unique tag, tracked to a specific room, cabinet, or shelf at a specific institution. |
| **Design Library** | Bill-of-Materials groupings: named assemblies of components and sub-assemblies, with hierarchical nesting and installed-instance tracking. |

A flexible **Properties** system attaches arbitrary typed metadata to any
domain item. A unified **Log** system records maintenance, inspection, and
lifecycle events across all domains.

---

## Project Structure

```
epiCDB/
├── manage.py
├── cdb_project/
│   ├── settings.py
│   └── urls.py
├── cdb/
│   ├── models.py               # All data models
│   ├── admin.py                # Django admin configuration
│   ├── migrations/
│   │   └── 0001_initial.py
│   └── management/
│       └── commands/
│           └── seed_cdb.py     # Sample data loader
└── client/
    ├── cdb.py                  # Command-line interface
    └── cdb_client/             # Programmatic query client (package)
        ├── client.py           # CDBClient (combined entry point)
        ├── catalog.py          # CatalogClient
        ├── inventory.py        # InventoryClient
        ├── designs.py          # DesignClient
        └── locations.py        # LocationClient
```

---

## Quick Start

**Requirements:** Python 3.10+, Django 4.x or 5.x.

```bash
pip install django

# Apply all migrations (creates db.sqlite3):
python manage.py migrate

# Load sample ePIC detector data:
python manage.py seed_cdb

# Start the development server:
python manage.py runserver

# Open the admin interface:
#   http://127.0.0.1:8000/admin/
#   Username: admin   Password: admin
```

To use the Python client from the Django shell, add `client/` to the path first:

```bash
PYTHONPATH=client python manage.py shell
```

```python
from cdb_client import CDBClient
client = CDBClient()

# Find where a component is located (pass a UUID primary key)
client.where_is("5a2c5c0e-479b-4e2f-a7cb-caea37435506")

# Get a full Bill of Materials
client.designs.bom("BEMC tower")
```

---

## Data Model

### Institutions and Locations

**`Institution`** is the top-level geographic anchor, representing a
collaborating lab or facility (e.g. BNL, CERN, Fermilab). Every location
must belong to an institution, enabling inventory tracking across multiple
sites.

| Field | Description |
|-------|-------------|
| `name` | Full name (unique) |
| `abbreviation` | Short code, e.g. `BNL` |
| `country` / `city` | Site geography |
| `url` | Homepage |

**`Location`** represents a physical place within an institution, organized
in a self-referential hierarchy: Building → Room → Cabinet → Shelf.

| Field | Description |
|-------|-------------|
| `name` | Location name |
| `location_type` | `building`, `room`, `cabinet`, `shelf`, `other` |
| `institution` | FK to owning Institution |
| `parent` | FK to parent Location (self-referential) |

`Location.__str__()` returns the full slash-separated path, e.g.:
`BNL / Building 510 / Room 382`.

---

### Domain 1 — Component Catalog

**`Component`** — one entry per unique component *type*.

| Field | Description |
|-------|-------------|
| `name` | Unique within a project |
| `model_number` | Vendor or internal model number |
| `description` | Free-text description |
| `project` | e.g. `ePIC` |
| `technical_system` | FK → `TechnicalSystem` (e.g. Tracking, Calorimetry) |
| `sources` | M2M → `Source` via `ComponentSource` |
| *(OwnedModel)* | Ownership + timestamps |

**`TechnicalSystem`** — engineering subsystem (Tracking, Vacuum, Controls, …).
Acts as a chapter in the catalog.

**`Source`** — vendor or manufacturer. The `ComponentSource` through-table
adds `part_number`, `cost`, and `role` (`vendor` / `manufacturer` / `both`).

---

### Domain 2 — Component Inventory

**`ComponentInstance`** — one row per physical item.

| Field | Description |
|-------|-------------|
| `tag` | Human-readable label |
| `serial_number` | Vendor serial number |
| `component` | FK → `Component` (catalog type) |
| `location` | FK → `Location` (where it currently is) |
| *(OwnedModel)* | Ownership + timestamps |

Each instance inherits all catalog-level properties from its parent
Component, and may additionally carry its own instance-specific properties
(e.g. an inspection report, a QA grade, a date put in service).

---

### Domain 3 — Designs

**`Design`** — a named assembly that fulfils a functional requirement.
Examples: *ePIC SVT Layer 1 Module*, *ePIC DAQ MicroTCA Crate*.

| Field | Description |
|-------|-------------|
| `name` | Unique design name |
| `description` | Functional description |
| `project` | e.g. `ePIC` |
| *(OwnedModel)* | Ownership + timestamps |

**`DesignElement`** — one slot within a design.

| Field | Description |
|-------|-------------|
| `element_name` | Unique name within the design (from naming convention) |
| `component` | FK → `Component` (leaf element) |
| `child_design` | FK → `Design` (sub-assembly — enables unlimited nesting) |
| `installed_instance` | FK → `ComponentInstance` (which physical item occupies this slot) |
| `quantity` | Number of this element required |

Exactly one of `component` or `child_design` is set per element. The
`installed_instance` field tracks which specific inventory item is currently
installed at each slot.

---

### Cross-Domain: Properties and Logs

**`PropertyValue`** — flexible key/value metadata attachable to any domain
item. A single table serves all four targets via optional FK columns.

| Field | Description |
|-------|-------------|
| `property_type` | FK → `PropertyType` (predefined, admin-extensible) |
| `tag` | Optional label for this value |
| `value` | String value |
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

---

### Ownership

Every domain model (Component, ComponentInstance, Design) inherits from the
abstract `OwnedModel`, which supplies:

| Field | Description |
|-------|-------------|
| `owner_user` | Individual owner (Django User) |
| `owner_group` | Owning group (CDB `Group`) |
| `group_writeable` | Whether group members can edit |
| `created_by` / `created_on` | Creation audit trail |
| `modified_by` / `modified_on` | Modification audit trail |

**`Group`** — a named team or department (e.g. `DIAG`, `CTL`, `EPIC_TRK`).

---

## Django Admin

The admin interface at `/admin/` provides full CRUD access to all models,
mirroring the CDB portal's layout:

- **Institution** pages include an inline table of all their locations.
- **Component** pages include inline tables for sources, properties,
  inventory instances, and log entries.
- **ComponentInstance** pages include inline properties and logs, plus an
  Institution column in the list view for quick site identification.
- **Design** pages include inline design elements, properties, and logs.
- **DesignElement** pages include inline element-level properties.
- All list views support filtering and search.

---

## Python Client (`cdb_client`)

A standalone query client that wraps Django ORM calls behind a clean API.
All methods return lazy Django QuerySets (chainable) unless documented as
returning a plain dict or list.

### Setup outside the Django shell

```python
import sys, os
sys.path.insert(0, "/path/to/epiCDB")          # Django project root
sys.path.insert(0, "/path/to/epiCDB/client")   # cdb_client package
os.environ["DJANGO_SETTINGS_MODULE"] = "cdb_project.settings"
import django; django.setup()

from cdb_client import CDBClient
client = CDBClient()
```

---

### LocationClient

```python
lc = client.locations

# All institutions
lc.all_institutions()

# Single institution by abbreviation
lc.get_institution(abbreviation="BNL")

# Institutions in a country
lc.institutions_by_country("USA")

# All locations at an institution
lc.locations_at_institution("CERN")

# Buildings only, optionally filtered by institution
lc.buildings(institution_abbr="BNL")

# Rooms within a named building
lc.rooms_in_building("Building 510", institution_abbr="BNL")

# Full nested tree as a list of dicts
lc.location_tree("BNL")
# Returns: [{"id": 1, "name": "Building 510", "type": "building",
#            "children": [{"id": 2, "name": "Room 382", ...}]}]
```

---

### CatalogClient

```python
cc = client.catalog

# Full-text search (name, alternate name, model number, description)
cc.search("silicon strip")

# Filter by technical system or project
cc.by_technical_system("BTOF-Sensor")
cc.by_project("ePIC")

# Look up a single component
cc.get(name="PbWO4 Crystal")
cc.get(model_number="PWO-BEMC-01")

# Count physical instances of a component type
cc.instance_count("PbWO4 Crystal")  # → int

# Full plain-dict summary
cc.summary("PbWO4 Crystal")
# Returns: {"id": ..., "name": ..., "sources": [...], "properties": [...], ...}
```

---

### InventoryClient

```python
ic = client.inventory

# Look up a single instance by its primary key (UUID)
ic.get(pk="5a2c5c0e-479b-4e2f-a7cb-caea37435506")

# All instances of a component type
ic.instances_of("ePIC SVT Silicon Strip Sensor")

# Filter by institution or location
ic.at_institution("CERN")
ic.at_location("Room 382", institution_abbr="BNL")

# Filter by owner group
ic.by_group("EPIC_TRK")

# Full-text search
ic.search("SVT-Sensor")

# Which instances are installed in a design's slots?
ic.installed_in_design("ePIC SVT Layer 1 Module")

# Instance count grouped by institution (plain list of dicts)
ic.institution_summary()
# Returns: [{"institution": "BNL", "count": 7}, {"institution": "FNAL", "count": 1}]

# Full plain-dict detail for a single instance
ic.detail("5a2c5c0e-479b-4e2f-a7cb-caea37435506")
# Returns: {"id": ..., "tag": ..., "location": ..., "institution": ..., "properties": [...], "logs": [...]}
```

---

### DesignClient

```python
dc = client.designs

# All designs, or filtered by project
dc.all_designs()
dc.by_project("ePIC")

# Full-text search
dc.search("tracking")

# Elements of a design
dc.elements_of("ePIC SVT Layer 1 Module")  # → QuerySet[DesignElement]

# Recursive Bill of Materials (nested list of dicts)
dc.bom("ePIC Tracking System")
# Returns:
# [{"element": "TRK-SVT-L1-MOD-01", "type": "DESIGN",
#   "ref": "ePIC SVT Layer 1 Module", "qty": 1,
#   "children": [
#     {"element": "SVT-L1-SEN-A", "type": "COMPONENT",
#      "ref": "ePIC SVT Silicon Strip Sensor",
#      "model_number": "HPK-SVT-01",
#      "installed_id": "5a2c5c0e-479b-4e2f-a7cb-caea37435506", "children": []},
#     ...
#   ]}, ...]

# Flat list of leaf components only (no sub-design rows)
dc.flat_component_list("ePIC SVT Layer 1 Module")

# Which designs directly contain a given component?
dc.designs_using_component("ePIC SVT Silicon Strip Sensor")

# Full plain-dict summary including BOM
dc.summary("ePIC DAQ MicroTCA Crate")
```

---

### CDBClient (combined)

```python
client = CDBClient()

# Search components, instances, and designs in one call
client.search_all("strip")
# Returns: {"components": [...], "instances": [...], "designs": [...]}

# Where is this item right now?
client.where_is("5a2c5c0e-479b-4e2f-a7cb-caea37435506")
# Returns: {"id": ..., "component": "ePIC SVT Silicon Strip Sensor",
#           "technical_system": "Tracking",
#           "location": "BNL / Building 510 / Room 382",
#           "institution": "BNL", "city": "Upton", "country": "USA",
#           "owner_user": "tester", "owner_group": "EPIC_TRK"}

# Sub-clients
client.locations   # LocationClient
client.systems     # SystemClient
client.catalog     # CatalogClient
client.inventory   # InventoryClient
client.designs     # DesignClient
```

---

## Seed Data

`python manage.py seed_cdb` loads a realistic ePIC detector dataset:

| Object | Count | Examples |
|--------|-------|---------|
| Institutions | 4 | BNL, CUA, UIC, UH |
| Locations | 3 | Storage Room (CUA), Test Lab (UIC), Bldg.510A (BNL) |
| Groups | 2 | BEMC, BTOF |
| Components | 4 | PbWO4 Crystal, Hamamatsu S14160-3010PS, AC-LGAD Sensor, FCFDv2 Readout |
| Instances | 14 | 3 crystals, 4 SiPMs, 2 AC-LGAD sensors, 5 FCFDv2 readout ASICs |
| Designs | 1 | BEMC tower (1 crystal + 4 SiPMs) |
| Property Types | 7 | Weight, Datasheet, Timing Constant, Length, Width, Height, Image |
| Sources | 0 | — |

The seed command is idempotent — running it multiple times will not create
duplicate records.

Default superuser: **admin / admin**.

---

## Schema Diagram

```
Institution
    │
    └─► Location (building → room → cabinet → shelf, self-FK)
              │
              └─► ComponentInstance ──────────────────┐
                       │                              │
                       │  FK: component               │  FK: installed_instance
                       ▼                              ▼
Group ──────► Component                         DesignElement ◄──── Design
                  │                                  │
                  ├─► ComponentSource ──► Source     └─► child_design (FK: Design)
                  │
                  ├─► PropertyValue   (FK: component)
                  ├─► PropertyValue   (FK: component_instance)
                  ├─► PropertyValue   (FK: design)
                  └─► PropertyValue   (FK: design_element)

LogEntry  (optional FK to Component / ComponentInstance / Design)

TechnicalSystem ──► Component
PropertyType ──► PropertyValue
```

---

## Design Decisions

**`OwnedModel` abstract base** — every domain model inherits a consistent
set of ownership (`owner_user`, `owner_group`, `group_writeable`) and audit
fields (`created_by/on`, `modified_by/on`), matching the CDB User Guide's
ownership model.

**`Institution` as a first-class model** — rather than a free-text field or
a special location type, `Institution` is its own model with country, city,
and URL. Every `Location` has a mandatory FK to an `Institution`, enabling
`SELECT … WHERE location__institution__abbreviation = 'CERN'` style queries
across the whole inventory.

**Single `PropertyValue` table** — four nullable FK columns (`component`,
`component_instance`, `design`, `design_element`) let one table serve all
domains with no duplication of schema, matching the CDB's philosophy that
any object can carry any property.

**`DesignElement` dual-target pattern** — each element sets either
`component` (leaf) or `child_design` (sub-assembly), never both. This
enables unlimited BOM nesting depth and is the mechanism by which a "full
tracking system" design can reference "SVT Layer 1 Module" sub-designs,
which in turn reference individual sensor and ASIC components.

**QuerySet-returning client methods** — `CatalogClient`, `InventoryClient`,
and `DesignClient` return lazy Django QuerySets wherever possible. Callers
can chain additional filters, annotations, or `values()` calls without
re-querying the database. Methods that return dicts or lists are explicitly
documented as such.

## Note for testers/developers

To completely reset the database, use these commands:
```bash
# from your project root (where manage.py lives)
rm -f db.sqlite3
rm -rf cdb/migrations
python manage.py makemigrations cdb
python manage.py migrate
python manage.py seed_cdb
```

## Logging 

Log entries are stored in a single LogEntry table, same table regardless of what the entry is attached to.
Each row has a topic (General/Installation/Inventory/Design/Maintenance/Inspection/Repair/Decommission/Other), free-text entry,
an optional attachment file (log_attachments/ under MEDIA_ROOT), who logged it, and a timestamp — and it links
to exactly one of component, component_instance, or design via nullable foreign keys, all CASCADE, so deleting
a Component/Instance/Design deletes its logs too.

There's a /logs/ page (log_list) that browses and filters all of them, and each Component/Instance/Design detai
page shows its own logs via the reverse related_name="log_entries". One thing worth knowing: like Designs,
there's currently no "Add Log Entry" form anywhere in the web UI — log_list is read-only,
so the only way to create one right now is the Django admin or direct ORM/API access.
