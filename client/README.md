# The HDB Client

Three ways to talk to the Hardware Database from outside the web UI, all built
on the same permission-checked query/write layer:

| Layer | File(s) | Use it when... |
|---|---|---|
| **Python library** | `hdb_client/` | You're writing a script or another Django-adjacent tool and want direct ORM-speed access, in-process. |
| **Command-line tool** | `hdb.py` | You want to query or update HDB from a terminal or shell script. |
| **MCP server** | `mcp_server.py` | You want an AI assistant (Claude, etc.) or any other MCP client to query/update HDB over HTTP. |

All three are thin wrappers around `hdb_client`: they don't talk to the web
app's `/api/` endpoints over HTTP, they import Django directly and query the
ORM in-process. Run them from a machine that has this repo (and the same
`db.sqlite3` / database) available, not from a remote box.

Everything below was tested against this repo's own seed data (`python
manage.py seed_hdb`), so every example output you see here is real.

---

## Installation

From the project root (next to `manage.py`):

```bash
pip install -r client/requirements.txt
```

`client/requirements.txt` covers the CLI and the MCP server (`django`,
`mcp[cli]`, `starlette`, `uvicorn`, `asgiref`, `PyYAML`). If you also want to
run `smoke_test.py`, additionally install `httpx`:

```bash
pip install httpx
```

Nothing here needs `djangorestframework` — that's only used by the web app's
`/api/` endpoints (`hdb/views.py`), which is a separate, unrelated code path.

---

## 1. The `hdb_client` Python library

Inside `manage.py shell` or any Django management command, Django is already
configured — just `from hdb_client import HDBClient` and go. Nothing below
this paragraph is needed in that case.

From a *plain* Python script (not launched through `manage.py`), Django has
no idea where your settings module is until you tell it, via the
`DJANGO_SETTINGS_MODULE` environment variable — `django.setup()` alone raises
`ImproperlyConfigured` without it. You also need two separate directories on
`sys.path`: the project root (for `hdb_project.settings`, and for `hdb_client`
to reach the Django app's `hdb.models`) and `client/` (for `hdb_client`
itself). Order matters: the project root must come *before* `client/`, or
Python resolves the bare name `hdb` to `client/hdb.py` (the CLI script)
instead of the real `hdb/` Django app package, and everything breaks with a
confusing `ImportError: cannot import name 'models' from 'hdb'`.

```python
import os, sys, django

ROOT = "/path/to/epic-hdb"          # repo root, next to manage.py
sys.path.insert(0, ROOT + "/client")  # so `import hdb_client` resolves
sys.path.insert(0, ROOT)              # must win over client/hdb.py -- insert last
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hdb_project.settings")
django.setup()

from hdb_client import HDBClient
from django.contrib.auth.models import User

# Unscoped / trusted-shell use (no ownership checks on reads):
client = HDBClient()

# User-scoped use (required for anything that writes):
crafts = User.objects.get(username="crafts")
client = HDBClient(user=crafts)
```

This is exactly what `client/hdb_client/_bootstrap.py`'s `_bootstrap()`
helper does (it's what every domain module calls internally via `_m()` to
reach `hdb.models` lazily) — but note it is *not* currently called by
anything for this exact purpose, and its `project_root` argument only adds
one path (the root, for settings), not `client/` itself. So it works
unmodified for code living *inside* `hdb_client`, but an external script
importing `hdb_client` from outside the package still needs the two-path
setup above; `hdb.py` and `mcp_server.py` each additionally rely on Python's
own "script directory goes on sys.path[0] automatically" behavior to get
`client/` for free, since they *are* files inside `client/`, which is why
their bootstrap code only inserts the project root explicitly.

`HDBClient` aggregates five sub-clients, each covering one domain:

| Sub-client | Domain |
|---|---|
| `client.locations` | Institutions, physical location hierarchy |
| `client.systems` | Technical systems (BEMC-CRYSTAL, BTOF-Sensor, ...) |
| `client.catalog` | Component Catalog (the "what" — PbWO4 Crystal, etc.) |
| `client.inventory` | Component Inventory (the "which physical one, where") |
| `client.designs` | Design Library (assemblies + Bill of Materials) |

Plus two cross-domain helpers directly on `HDBClient`:

```python
client.search_all("Crystal")     # -> {"components": [...], "instances": [...], "designs": [...]}
client.where_is(instance_pk)     # -> flat dict: location, institution, owner, ...
```

### Locations & Institutions — `client.locations`

```python
client.locations.all_institutions()                     # queryset of Institution
client.locations.get_institution(abbreviation="CUA")
client.locations.institutions_by_country("USA")
client.locations.users_at_institution("BNL")             # UserProfile queryset

client.locations.all_locations()
client.locations.locations_at_institution("CUA")
client.locations.buildings(institution_abbr="BNL")
client.locations.rooms_in_building("Bldg,510A")

client.locations.location_tree("CUA")
# -> [{"id": ..., "name": "Storage Room", "type": "room", "children": []}]
```

### Technical Systems — `client.systems`

```python
client.systems.all()                          # queryset of TechnicalSystem
client.systems.get(name="BEMC-CRYSTAL")
client.systems.components("BEMC-CRYSTAL")     # Components assigned to this system
client.systems.instances("BEMC-CRYSTAL")      # Instances tagged with this system

client.systems.instance_counts()
# -> [{"id": ..., "name": "BEMC-CRYSTAL", "components": 1, "instances": 3}, ...]

client.systems.summary("BEMC-CRYSTAL")
# -> {"id": ..., "name": ..., "description": ..., "component_count": 1, "instance_count": 3}
```

### Component Catalog — `client.catalog`

```python
client.catalog.search("Crystal")                        # list[Component]
client.catalog.by_technical_system("BEMC-CRYSTAL")
client.catalog.by_project("ePIC")
client.catalog.get(name="PbWO4 Crystal")                 # or model_number=, pk=
client.catalog.instance_count("PbWO4 Crystal")           # -> int
client.catalog.all_components()                          # queryset, all visible components

# JSON-safe dict output (what the CLI/MCP tools actually return):
client.catalog.search_brief("Crystal")
# -> [{"id": ..., "name": "PbWO4 Crystal", "model_number": "PWO-BEMC-01",
#      "project": "ePIC", "technical_system": "BEMC-CRYSTAL",
#      "owner_group": "BEMC", "instance_count": 3}]

client.catalog.summary("PbWO4 Crystal")
# -> full dict: id, name, alternate_name, model_number, description, project,
#    technical_system, owner_user, owner_group, group_writeable,
#    created_on, modified_on, instance_count, sources[], properties[], log_entries[]
```

### Component Inventory — `client.inventory`

```python
client.inventory.get(pk)                                 # single ComponentInstance
client.inventory.instances_of("PbWO4 Crystal")
client.inventory.at_institution("CUA")
client.inventory.at_location("Storage Room", institution_abbr="CUA")
client.inventory.by_group("BEMC")
client.inventory.search("BEMC-CRYSTAL")
client.inventory.installed_in_design("BEMC tower")
client.inventory.institution_summary()
# -> [{"institution": "CUA", "count": 17}, {"institution": "UIC", "count": 9}, ...]
client.inventory.all_instances()                          # queryset, all visible instances

client.inventory.search_brief("BEMC-CRYSTAL")
# -> [{"id": ..., "tag": "BEMC-CRYSTAL-001", "serial_number": "PWO-0001",
#      "component": "PbWO4 Crystal", "location": "CUA / Storage Room",
#      "owner_group": "BEMC"}, ...]

client.inventory.detail(pk)
# -> full dict: id, tag, serial_number, description, component, technical_system,
#    location, owner_user, owner_group, group_writeable, created_on, modified_on,
#    properties[], log_entries[]
```

**Writing** — `client.inventory.create(...)` — requires a user-scoped client
(`HDBClient(user=...)`); anonymous callers are rejected outright. Ownership is
*never* taken from caller input: the new record's `owner_user` is always the
client's bound user, and `owner_group` must be a group that user actually
belongs to (or `None`, or staff/superuser bypasses the group check). This is
deliberate — see `hdb_client/access.py` — so there is no parameter to assign a
record to somebody else.

```python
result = client.inventory.create(
    component_name="PbWO4 Crystal",     # or component_pk=<uuid>
    tag="BEMC-CRYSTAL-099",
    serial_number="PWO-0099",
    description="New delivery, batch 12",
    location_name="Storage Room",       # must already exist
    owner_group_name="BEMC",            # must exist AND crafts must belong to it
)
# -> same shape as client.inventory.detail()
```

### Design Library — `client.designs`

```python
client.designs.get(name="BEMC tower")                     # or pk=
client.designs.by_project("ePIC")
client.designs.search("tower")
client.designs.elements_of("BEMC tower")                  # DesignElement queryset
client.designs.designs_using_component("PbWO4 Crystal")
client.designs.all_designs()                               # queryset, all visible designs
client.designs.flat_component_list("BEMC tower")            # BOM flattened, leaves only

client.designs.bom("BEMC tower")
# -> [{"element": "Crystal", "type": "COMPONENT", "qty": 1, "description": "",
#      "ref": "PbWO4 Crystal", "model_number": "PWO-BEMC-01",
#      "installed_ids": [...], "children": []},
#     {"element": "SiPM", "type": "COMPONENT", "qty": 4, "ref": "Hamamatsu S14160-3010PS",
#      ...}]
# A COMPONENT element's "installed_ids" lists every physical instance
# currently filling one of its `qty` slots (0 to qty of them). A DESIGN
# element instead has "ref" = the child design's name and a "children" BOM
# (designs can nest other designs).

client.designs.search_brief("tower")
# -> [{"id": ..., "name": "BEMC tower", "description": "...", "project": "ePIC",
#      "owner_group": "BEMC", "element_count": 2}]

client.designs.summary("BEMC tower")
# -> full dict: id, name, description, project, owner_user, owner_group,
#    group_writeable, created_on, modified_on, element_count, bom (full BOM tree)
```

### Design Templates — loading from YAML

`DesignTemplateElement.component` is a *required* FK to a real catalog
`Component` — unlike `Design`/`DesignElement`, a `DesignTemplate` has no
`child_template` field, so one template cannot directly reference another as
a sub-assembly. To represent a multi-level hierarchy (e.g. a detector built
from staves, made of half-staves, made of smaller modules) with the schema
as it stands, model each intermediate assembly level as **both** its own
catalog `Component` **and** its own `DesignTemplate` of the identical name.
`template_bom()` follows that name match to recurse — the same way a real
design's BOM follows `child_design` — so a multi-level template set behaves
like nested templates without needing a schema change. This also means every
intermediate assembly (a stavelet, a half-stave, ...) is independently
serial-trackable as its own `ComponentInstance` once built, which a "pure
template nesting" design wouldn't give you for free.

```python
client.designs.load_templates_from_yaml("data/btof_stave_templates.yaml")
# -> list of per-template summary dicts:
#    [{"template": "BTOF Stavelet", "template_created": True,
#      "elements": [{"element_name": "AC-LGAD Sensors", "component": "AC-LGAD Sensor",
#                    "component_created": False, "element_created": True}, ...]}, ...]

client.designs.template_bom("BTOF Stave")
# -> recursive parts explosion, same shape idea as designs.bom():
#    [{"element": "Half-Stave Assemblies", "component": "BTOF Half-Stave",
#      "model_number": "BTOF-HALFSTAVE-R1", "qty": 144, "description": "...",
#      "children": [ ... recurses into the BTOF Half-Stave template ... ]}, ...]

client.designs.all_templates()                 # queryset of DesignTemplate
client.designs.get_template(name="BTOF Stave")
client.designs.template_elements("BTOF Stave")  # DesignTemplateElement queryset
client.designs.create_template(name=..., project="ePIC", description=..., elements=[...])
```

**Idempotent** — safe to re-run: existing templates, elements, and
components are never modified or duplicated, only what's missing gets
created (`get_or_create` throughout, matching `seed_hdb.py`'s convention).
This is a curator/administrative operation, like `seed_hdb`, not a
per-request scoped write — `owner_group_name`/`owner_username` (or the
YAML's `owner_group`/`owner_user`) are recorded on the template if given, but
there's no requirement that the acting user belong to that group (unlike
`inventory.create()`).

YAML file format (a single template document, or `templates: [...]` for
several — loaded top to bottom, so for a multi-level hierarchy list leaf
templates first, since a higher level's element needs the level below's
Component to already exist or be creatable):

```yaml
templates:
  - template:
      name: "BTOF Stavelet"
      project: "ePIC"           # default "ePIC"
      owner_group: BTOF          # optional Group name
      owner_user: crafts         # optional Django username
      description: "..."
    elements:
      - element_name: "AC-LGAD Sensors"
        quantity: 4               # default 1
        description: "..."
        component: "AC-LGAD Sensor"   # plain string = must already exist
      - element_name: "Interposer Boards"
        quantity: 4
        component:                     # dict = get_or_create'd if missing
          name: "BTOF Interposer Board"
          model_number: "BTOF-INT-01"  # every component gets its own UUID id
          description: "..."           # (Component.pk) plus this human part
          technical_system: "BTOF-Mechanical"   # number -- both are how a
          technical_system_group: BTOF          # physical instance traces
                                                 # back to its catalog entry.
```

See `data/btof_stave_templates.yaml` for the full, real worked example: the
ePIC Barrel Time-of-Flight (BTOF) detector's Stave → Half-Stave → Stavelet
hierarchy, with dimensions, sensor/ASIC counts, and timing/spatial
resolution sourced from public ePIC BTOF status talks (cited in the file's
header comment).

### `hdb.py` commands

```
$ python client/hdb.py --user crafts load-template btof_stave_templates.yaml
Template 'BTOF Stavelet': created
  - AC-LGAD Sensors: AC-LGAD Sensor [created]
  - FCFD Readout ASICs: FCFDv2 Readout [created]
  - Interposer Boards: BTOF Interposer Board (component created) [created]
  - Flex Cable (FPC): BTOF Flex Cable (FPC) (component created) [created]
Template 'BTOF Half-Stave': created
  - Stavelet Modules: BTOF Stavelet (component created) [created]
  ...
Template 'BTOF Stave': created
  - Half-Stave Assemblies: BTOF Half-Stave (component created) [created]
  - Carbon Honeycomb Support: BTOF Carbon Honeycomb Support (component created) [created]

$ python client/hdb.py bom-template "BTOF Stave"
Carbon Honeycomb Support  x1  -> BTOF Carbon Honeycomb Support (BTOF-SUPPORT-01)
Half-Stave Assemblies  x144  -> BTOF Half-Stave (BTOF-HALFSTAVE-R1)
  Cooling Pipe Segment  x1  -> BTOF Cooling Pipe Segment (BTOF-COOL-01)
  Peripheral Board  x1  -> BTOF Peripheral Board (BTOF-PERIPH-01)
  Stavelet Modules  x8  -> BTOF Stavelet (BTOF-STAVELET-R1)
    AC-LGAD Sensors  x4  -> AC-LGAD Sensor (AC-LGAD-v1)
    FCFD Readout ASICs  x4  -> FCFDv2 Readout (FCFDv2)
    Flex Cable (FPC)  x1  -> BTOF Flex Cable (FPC) (BTOF-FPC-01)
    Interposer Boards  x4  -> BTOF Interposer Board (BTOF-INT-01)
```

`load-template FILE` resolves `FILE` against the current directory first,
then falls back to `<project root>/data/FILE` — so both a bare filename and
an explicit path work, run from anywhere. `--user` is optional here (unlike
`create-instance`) since this is a curator operation, not a per-request
write.

#### Deleting a template

A `DesignTemplate` can be deleted outright — in the web UI (the "Delete
Template" button on its detail page) or via `client.designs.delete_template()`
/ `hdb.py delete-template` — but only while it's **unlocked**, i.e. no
`Design` has ever been instantiated from it (`template.designs.exists()`
is `False`). This is the same immutability rule that freezes a template's
placeholders once it's been used (see above): a template that already
backs a real, instantiated design can't be edited *or* deleted, so
"instantiated from BEMC tower" always means the same bill of placeholders.

Unlike editing (open to any member of the template's `owner_group`, or a
superuser), **deletion is superuser-only** — a deliberately stricter
policy, since removing the template removes it for every group member at
once rather than just changing the deleter's own records. `--user` is
required and must be a Django superuser; the web UI hides the button
entirely for non-superusers and 403s a direct POST.

```
$ python client/hdb.py --user crafts delete-template "BTOF Stavelet"
Error: User 'crafts' is not a superuser and cannot delete design templates.

$ python client/hdb.py --user maxim delete-template "BEMC tower"
Delete design template 'BEMC tower'? This cannot be undone. [y/N] y
Error: Design template 'BEMC tower' is locked -- 1 design(s) have been instantiated from it.

$ python client/hdb.py --user maxim delete-template "BTOF Stavelet" --yes
Deleted template 'BTOF Stavelet'.
```

`--yes` skips the interactive confirmation prompt (useful for scripting).
Deleting a template that's referenced as a "sub-template" by a
higher-level template (via the name-matching convention above) is fine —
`template_bom()` simply treats that element as a leaf on its next run,
the same way it already does for any element whose component name doesn't
match an existing template.

```python
client.designs.delete_template(name="BTOF Stavelet")
# -> {"template": "BTOF Stavelet", "deleted": True}
# Raises PermissionError if self.user isn't a superuser, or RuntimeError
# if the template is locked.
```

### Access control — `hdb_client/access.py`

- **Read**: unrestricted for any authenticated (or unauthenticated/`user=None`)
  client — HDB is a shared collaboration database with no per-row read
  privacy. `visible_to()` is a hook for tightening this later; currently a
  no-op.
- **Write**: a user may create/modify/delete a row if they're its
  `owner_user`, they're Django staff/superuser, or they're a member of
  `owner_group` *and* `group_writeable` is `True`. A user creating a new row
  may only ever set `owner_user` to themselves and `owner_group` to a group
  they belong to — never to someone/something else.

---

## 2. Command-line interface — `hdb.py`

```bash
python client/hdb.py [--settings MODULE] [--root DIR] [--user USERNAME] [--yaml] COMMAND [args...]
```

| Flag | Meaning |
|---|---|
| `--settings` | Django settings module (default `hdb_project.settings`) |
| `--root` | Project root directory (default: parent of `client/`) |
| `--user` | Django username to act as. Only needed for `create-instance` — every other command works fine unauthenticated. |
| `--yaml` | Force YAML output for commands that default to a plain table |

Set up a `bin/hdb` wrapper (not included in the repo) if you want to just
type `hdb ...`:

```bash
mkdir -p bin
printf '#!/usr/bin/env bash\nexec python3 "$(dirname "$0")/../client/hdb.py" "$@"\n' > bin/hdb
chmod +x bin/hdb
export PATH="$PWD/bin:$PATH"
```

### `institutions`

```
$ python client/hdb.py institutions
Abbr  Name                            City            Country
----  ------------------------------  --------------  -------
BNL   Brookhaven National Laboratory  Upton, NY       USA
CUA   Catholic University of America  Washington, DC  USA
UH    University of Hawaii            Honolulu, HI    USA
UIC   University of Illinois Chicago  Chicago         USA
```

### `location-tree <ABBR>`

```
$ python client/hdb.py location-tree CUA
[room] Storage Room
```

### `systems`

```
$ python client/hdb.py systems
ID                                    Technical System  Components  Instances
------------------------------------  ----------------  ----------  ---------
0a7c506c-a8e4-4c1a-8b66-7a3167bf6303  BEMC-CRYSTAL      1           3
df03ef1a-251e-4e4f-8864-64bb07486bcc  BEMC-PM           1           16
1bf3e391-1cf3-4eee-beb2-0f73a1d8d442  BTOF-Readout      1           5
7159a9ce-226c-4239-9734-c99d6686fa9c  BTOF-Sensor       1           2
```

### `search <QUERY...>`

Cross-domain keyword search over components, inventory instances, and
designs (not institutions or technical systems — use `find`/`institutions`/
`systems` for those).

```
$ python client/hdb.py search Crystal

-- components --
  id: cdc27f3f-...  |  name: PbWO4 Crystal  |  model_number: PWO-BEMC-01  |  ...

-- instances --
  id: e7a1c023-...  |  tag: BEMC-CRYSTAL-003  |  serial_number: PWO-0003  |  ...
  ...

-- designs --
  id: c0db7fa1-...  |  name: BEMC tower  |  description: One BEMC tower...  |  ...
```

### `component <NAME...>` — full Component summary, YAML

```
$ python client/hdb.py component "PbWO4 Crystal"
id: cdc27f3f-ba78-4561-b7d2-8ef56b5e5026
name: PbWO4 Crystal
model_number: PWO-BEMC-01
description: Lead tungstate scintillating crystal for the Backward EMCal.
technical_system: BEMC-CRYSTAL
owner_user: crafts
owner_group: BEMC
instance_count: 3
properties:
- property_type: Weight
  value: '0.45'
  units: kg
...
```

### `inventory <PK>` — full instance detail, YAML

```
$ python client/hdb.py inventory e80ed9a1-3ac7-4afa-b540-19c575d89fe7
tag: BEMC-CRYSTAL-001
serial_number: PWO-0001
component: PbWO4 Crystal
location: CUA / Storage Room
owner_user: crafts
owner_group: BEMC
...
```

### `where <PK>` — quick "where is this thing right now"

```
$ python client/hdb.py where e80ed9a1-3ac7-4afa-b540-19c575d89fe7
ID       : e80ed9a1-3ac7-4afa-b540-19c575d89fe7
Component: PbWO4 Crystal
System   : BEMC-CRYSTAL
Location : CUA / Storage Room
Site     : CUA  (Washington, DC, USA)
Owner    : crafts
Group    : BEMC
```

### `bom <DESIGN_NAME...>` — recursive Bill of Materials

```
$ python client/hdb.py bom "BEMC tower"
[COMPONENT] Crystal  x1  -> PbWO4 Crystal
[COMPONENT] SiPM  x4  -> Hamamatsu S14160-3010PS
```

Use `--yaml` for the full nested tree (including `installed_ids`).

### `find <TYPE> <PATTERN>` — glob-match by name/tag

`TYPE` is one of `component`, `instance`, `design`, `system`. `PATTERN` is a
case-insensitive glob (`fnmatch`), e.g. `"PbWO4*"`, `"*sensor*"`.

```
$ python client/hdb.py find component "PbWO4*"
ID                                    Name           Model No.    System        Owner   Group
------------------------------------  -------------  -----------  ------------  ------  -----
cdc27f3f-...                          PbWO4 Crystal  PWO-BEMC-01  BEMC-CRYSTAL  crafts  BEMC

$ python client/hdb.py find instance "BEMC-CRYSTAL*"
ID             Tag               Component      Location            Owner   Group
-------------  ----------------  -------------  ------------------  ------  -----
e7a1c023-...   BEMC-CRYSTAL-003  PbWO4 Crystal  CUA / Storage Room  crafts  BEMC
...

$ python client/hdb.py find design "*"
ID             Name        Owner   Group  Description
-------------  ----------  ------  -----  --------------------------------------------
c0db7fa1-...   BEMC tower  crafts  BEMC   One BEMC tower: a PbWO4 crystal read out...

$ python client/hdb.py find system "*"
ID             Name          Description
-------------  ------------  -----------
0a7c506c-...   BEMC-CRYSTAL
...
```

### `create-instance` — write a new inventory item

**Requires `--user`** (global flag, comes before the subcommand). The created
record is always owned by that user — there is no flag to assign it to
someone else (see Access control, above).

```bash
# Individual flags
python client/hdb.py --user crafts create-instance \
    --by-name "PbWO4 Crystal" --tag BEMC-CRYSTAL-099 --serial PWO-0099 \
    --group BEMC --description "New delivery, batch 12"

# Identify the component by UUID instead of name
python client/hdb.py --user crafts create-instance \
    --by-pk cdc27f3f-ba78-4561-b7d2-8ef56b5e5026 --tag BEMC-CRYSTAL-100

# From a YAML file
python client/hdb.py --user crafts create-instance --from-yaml instance.yaml
python client/hdb.py --user crafts create-instance --from-yaml -   # stdin
```

`instance.yaml`:
```yaml
by_name: "PbWO4 Crystal"        # or: by_pk: <uuid>
tag: BEMC-CRYSTAL-099
serial: "PWO-0099"
location: "Storage Room"
group: BEMC
description: "New delivery, batch 12"
```

Real output:
```
$ python client/hdb.py --user crafts create-instance --by-name "PbWO4 Crystal" --tag BEMC-CRYSTAL-099 --group BEMC
Created: 22f0b691-61db-4d35-876b-eb88113670bf
id: 22f0b691-61db-4d35-876b-eb88113670bf
tag: BEMC-CRYSTAL-099
owner_user: crafts
owner_group: BEMC
...
```

`--group` must be a group `--user` actually belongs to (or `--user` must be
staff/superuser) — otherwise:
```
Error creating instance: User 'crafts' is not a member of group 'BTOF' and cannot create records owned by it.
```
And omitting `--user` entirely fails with:
```
Error creating instance: Authentication required to create inventory records.
```

---

## 3. MCP server — `mcp_server.py`

Exposes the same `hdb_client` layer over MCP (Streamable HTTP transport
only — no stdio), so an MCP-aware AI assistant can query and update HDB.

### Authentication

HTTP Basic Auth, checked against Django's own `auth.User` table (whatever
accounts already exist — `/admin/`, `createsuperuser`, etc.). There's no
separate credential store, and passwords are never logged or stored by this
file. Every authenticated request is bound to that Django user for its
duration, and every tool builds a `HDBClient(user=...)` scoped to them — so
writes go through exactly the same ownership checks described above. A basic
in-memory lockout (5 failed attempts per `client_ip:username` within 60s →
30s lockout) adds brute-force friction; it's per-process and resets on
restart, not a substitute for a real solution in a multi-worker deployment.

### Setup

```bash
pip install "mcp[cli]" django starlette uvicorn asgiref

# from the project root, next to manage.py:
DJANGO_SETTINGS_MODULE=hdb_project.settings \
HDB_PROJECT_ROOT=/path/to/epic-hdb \
HDB_MCP_PUBLIC_HOST=your-tunnel-hostname.trycloudflare.com \
python client/mcp_server.py
```

Runs on `0.0.0.0:8001` (edit the bottom of `mcp_server.py` to change).

| Env var | Purpose |
|---|---|
| `DJANGO_SETTINGS_MODULE` | Which settings module to load (defaults to `hdb_project.settings` if unset) |
| `HDB_PROJECT_ROOT` | Project root to put on `sys.path` (defaults to the parent of `client/`) |
| `HDB_MCP_PUBLIC_HOST` | Your current public tunnel hostname (no scheme), e.g. from `cloudflared`. Required to let the MCP SDK's DNS-rebinding protection accept requests arriving through the tunnel — without it, tunneled requests get rejected with "Invalid Host header" before your auth check even runs. Re-set this every time the tunnel hostname changes. |

**Library version note**: the exact `mcp` SDK API this file uses
(`mcp.server.fastmcp.FastMCP`, `mcp.server.transport_security`,
`streamable_http_app()`) has moved around across releases. This repo was last
verified against `mcp[cli]==1.12.4`; the brand-new `mcp==2.0.0` is a breaking
rewrite with a different module layout and will not work as-is. If something
doesn't import, check `python -c "from mcp.server.fastmcp import FastMCP;
print(dir(FastMCP))"` against your installed version.

### Available tools

| Tool | Equivalent to |
|---|---|
| `hdb_whoami` | Authenticated user's username, email, groups |
| `hdb_search(query, limit=15)` | `client.search_all()` |
| `hdb_where_is(instance_id)` | `client.where_is()` |
| `hdb_component_search(query, limit=25)` | `client.catalog.search_brief()` |
| `hdb_component_summary(component_name)` | `client.catalog.summary()` |
| `hdb_instance_search(query, limit=25)` | `client.inventory.search_brief()` |
| `hdb_instance_detail(instance_id)` | `client.inventory.detail()` |
| `hdb_instances_at_institution(institution_abbreviation, limit=25)` | `client.inventory.at_institution()` |
| `hdb_design_search(query, limit=25)` | `client.designs.search_brief()` |
| `hdb_design_summary(design_name)` | `client.designs.summary()` |
| `hdb_design_bom(design_name)` | `client.designs.bom()` |
| `hdb_list_institutions()` | `client.locations.all_institutions()` |
| `hdb_location_tree(institution_abbreviation)` | `client.locations.location_tree()` |
| `hdb_systems_overview()` | `client.systems.instance_counts()` |
| `hdb_create_instance(component_name, tag="", serial_number="", description="", location_name=None, owner_group_name=None)` | `client.inventory.create()`, ownership always the authenticated caller |

### Talking to it from an MCP client

Point any MCP client at `http://<host>:8001/mcp/` with HTTP Basic Auth using
Django credentials. Example using the official Python SDK directly:

```python
import anyio, httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    creds = httpx.BasicAuth("crafts", "crafts")
    async with streamablehttp_client("http://127.0.0.1:8001/mcp/", auth=creds) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("hdb_search", {"query": "Crystal"})
            print(result.content[0].text)

anyio.run(main)
```

### Testing it — `smoke_test.py`

End-to-end check covering both the auth boundary (raw HTTP, no MCP protocol)
and every tool (authenticated as the seed user `crafts`):

```bash
# Terminal 1
python manage.py seed_hdb          # idempotent, safe to re-run
HDB_PROJECT_ROOT=$(pwd) python client/mcp_server.py

# Terminal 2
pip install httpx "mcp[cli]"
python client/smoke_test.py
python client/smoke_test.py --base-url http://127.0.0.1:8001 --username gnigmat --password gnigmat
```

Last verified run: **24/24 checks passed**, including the write-path
permission boundary (`crafts` can create an instance owned by BEMC, their own
group, but is rejected trying to own one by BTOF).

---

## Notes

- All three layers are read-unrestricted, write-restricted-by-ownership — see
  *Access control* above. There is currently no way, in any of the three
  layers, for a caller to assign a new record's ownership to anyone other
  than themselves.
- `client/requirements.txt` intentionally does not pin `djangorestframework`
  — the client layer's own serializers (`hdb_client/serializers.py`) are
  plain functions, independent of the web app's DRF-based `/api/` endpoints.
