#!/usr/bin/env python3
"""
hdb — command-line interface to the Hardware Database.

Run from the project root (next to manage.py):

    bin/hdb <command> [args]

Add bin/ to PATH to invoke as just "hdb" from anywhere.

Commands
--------
  institutions                  List all institutions
  location-tree  <ABBR>         Location hierarchy for an institution
  systems                       List technical systems with counts
  search         <QUERY>        Cross-domain keyword search
  component      <NAME>         Component summary (YAML)
  inventory      <PK>            Instance detail (YAML)
  where          <PK>            Where is this item right now?
  bom            <DESIGN_NAME>  Bill of Materials (YAML)
  find           <TYPE> <PAT>   List items matching a wildcard pattern
  create-instance               Create a new ComponentInstance
  load-template  <FILE>         Load DesignTemplate(s) from a YAML file
  bom-template   <NAME>         Recursive parts explosion for a template
  delete-template <NAME>        Delete an unlocked DesignTemplate (superuser-only)

Options
-------
  --settings  Django settings module  [default: hdb_project.settings]
  --root      Project root directory  [default: parent of bin/]
  --yaml      Force YAML output for all commands
  --user      Django username to act as for write operations
              (required for create-instance; the created record is always
              owned by this user -- there is no way to set ownership to
              someone else, by design, see hdb_client/access.py. Also
              required for delete-template, where the user must be a
              Django superuser.)
"""

import argparse
import yaml
import os
import sys


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def _setup(args):
    # Default root: parent of the bin/ directory this script lives in
    root = args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)

    import django
    import django.conf
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", args.settings)
    if not django.conf.settings.configured:
        django.setup()

    from hdb_client import HDBClient

    django_user = None
    if getattr(args, "user", None):
        from django.contrib.auth.models import User
        try:
            django_user = User.objects.get(username=args.user)
        except User.DoesNotExist:
            print(f"Error: no such Django user {args.user!r}.", file=sys.stderr)
            sys.exit(1)

    return HDBClient(user=django_user)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _yaml(obj):
    """Serialize obj to YAML, converting non-basic types to strings."""
    def _fix(o):
        if isinstance(o, dict):
            return {k: _fix(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_fix(i) for i in o]
        if isinstance(o, (str, int, float, bool, type(None))):
            return o
        return str(o)
    print(yaml.dump(_fix(obj), default_flow_style=False,
                    allow_unicode=True, sort_keys=False))


def _table(rows, headers):
    """Print an aligned plain-text table."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_institutions(client, args):
    insts = client.locations.all_institutions()
    if args.yaml:
        _yaml([{"id": i.pk, "name": i.name, "abbreviation": i.abbreviation,
                "city": i.city, "country": i.country, "url": i.url}
               for i in insts])
    else:
        _table(
            [(i.abbreviation, i.name, i.city, i.country) for i in insts],
            ["Abbr", "Name", "City", "Country"],
        )


def cmd_location_tree(client, args):
    tree = client.locations.location_tree(args.abbr)
    if args.yaml:
        _yaml(tree)
    else:
        def _print(nodes, indent=0):
            for node in nodes:
                print("  " * indent + f"[{node['type']}] {node['name']}")
                _print(node["children"], indent + 1)
        _print(tree)


def cmd_systems(client, args):
    rows = client.systems.instance_counts()
    if args.yaml:
        _yaml(rows)
    else:
        _table(
            [(r["id"], r["name"], r["components"], r["instances"]) for r in rows],
            ["ID", "Technical System", "Components", "Instances"],
        )


def cmd_search(client, args):
    query = " ".join(args.query)
    results = client.search_all(query)
    if args.yaml:
        _yaml(results)
    else:
        for domain, items in results.items():
            print(f"\n-- {domain} --")
            if not items:
                print("  (none)")
            for item in items:
                parts = "  " + "  |  ".join(f"{k}: {v}" for k, v in item.items() if v)
                print(parts)


def cmd_component(client, args):
    name = " ".join(args.name)
    try:
        data = client.catalog.summary(name)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    _yaml(data)


def cmd_inventory(client, args):
    try:
        data = client.inventory.detail(args.pk)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    _yaml(data)


def cmd_where(client, args):
    try:
        data = client.where_is(args.pk)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.yaml:
        _yaml(data)
    else:
        print(f"ID       : {data['id']}")
        print(f"Component: {data['component']}")
        print(f"System   : {data['technical_system'] or '--'}")
        print(f"Location : {data['location'] or '--'}")
        site = data.get("institution") or "--"
        geo  = f"  ({data['city']}, {data['country']})" if data.get("city") else ""
        print(f"Site     : {site}{geo}")
        print(f"Owner    : {data.get('owner_user') or '--'}")
        print(f"Group    : {data.get('owner_group') or '--'}")


def cmd_bom(client, args):
    name = " ".join(args.name)
    try:
        data = client.designs.bom(name)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.yaml:
        _yaml(data)
    else:
        def _print(rows, indent=0):
            for row in rows:
                prefix = "  " * indent
                tag = f"[{row['type']}]"
                ref = row.get("ref") or ""
                qty = row.get("qty", 1)
                print(f"{prefix}{tag} {row['element']}  x{qty}  -> {ref}")
                _print(row.get("children", []), indent + 1)
        _print(data)


def _data_path(args, filename: str) -> str:
    """Resolve a filename against the project's data/ folder if it isn't
    already a valid path as given (relative to cwd or absolute)."""
    if os.path.exists(filename):
        return filename
    root = args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(root, "data", filename)
    return candidate if os.path.exists(candidate) else filename


def cmd_load_template(client, args):
    """Load one or more DesignTemplates from a YAML file.

    Looks for FILE as given first (relative to cwd, or absolute); if that
    doesn't exist, falls back to <project root>/data/FILE.

    Idempotent -- safe to re-run. Existing templates, elements, and
    components are never modified or duplicated; only what's missing gets
    created. See client/README.md for the YAML format and the flat
    multi-level-template naming convention.

    Examples
    --------
      bin/hdb load-template btof_stave_templates.yaml
      bin/hdb load-template data/btof_stave_templates.yaml
      bin/hdb --user crafts load-template btof_stave_templates.yaml
    """
    path = _data_path(args, args.file)
    try:
        results = client.designs.load_templates_from_yaml(path)
    except FileNotFoundError:
        print(f"Error: file not found: {args.file!r} (looked in cwd and data/)", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error loading template(s): {exc}", file=sys.stderr)
        sys.exit(1)

    if args.yaml:
        _yaml(results)
        return

    for r in results:
        status = "created" if r["template_created"] else "already existed"
        print(f"Template {r['template']!r}: {status}")
        for el in r["elements"]:
            el_status = "created" if el["element_created"] else "already existed"
            comp_status = " (component created)" if el["component_created"] else ""
            print(f"  - {el['element_name']}: {el['component']}{comp_status} [{el_status}]")


def cmd_bom_template(client, args):
    name = " ".join(args.name)
    try:
        data = client.designs.template_bom(name)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.yaml:
        _yaml(data)
    else:
        def _print(rows, indent=0):
            for row in rows:
                prefix = "  " * indent
                print(f"{prefix}{row['element']}  x{row['qty']}  -> {row['component']} ({row['model_number'] or '--'})")
                _print(row.get("children", []), indent + 1)
        _print(data)



def cmd_delete_template(client, args):
    """Delete a DesignTemplate outright -- superuser-only, and only while
    it's unlocked (no Design has ever been instantiated from it). Mirrors
    the web UI's "Delete Template" button/policy exactly.

    Requires --user to be a Django superuser; prompts for confirmation
    unless --yes is given.

    Examples
    --------
      bin/hdb --user maxim delete-template "BTOF Stavelet"
      bin/hdb --user maxim delete-template "BTOF Stavelet" --yes
    """
    name = " ".join(args.name)
    if not args.yes:
        reply = input(f"Delete design template {name!r}? This cannot be undone. [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return
    try:
        result = client.designs.delete_template(name=name)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Deleted template {result['template']!r}.")


def cmd_find(client, args):
    """List items whose name (or tag) matches a glob wildcard pattern.

    Examples
    --------
      bin/hdb find component "PbWO4*"
      bin/hdb find design    "ePIC*"
      bin/hdb find instance  "*strip*"
      bin/hdb find system    "SVT*"
    """
    import fnmatch

    pat = args.pattern

    def _match(s):
        return fnmatch.fnmatch((s or "").lower(), pat.lower())

    kind = args.type

    if kind == "component":
        items = [c for c in client.catalog.all_components() if _match(c.name)]
        if not items:
            print("(no matches)")
            return
        if args.yaml:
            _yaml([{"id":               str(c.pk),
                    "name":             c.name,
                    "model_number":     c.model_number,
                    "technical_system": str(c.technical_system) if c.technical_system else None,
                    "owner_user":       c.owner_user.username if c.owner_user else None,
                    "owner_group":      str(c.owner_group) if c.owner_group else None}
                   for c in items])
        else:
            _table(
                [(str(c.pk), c.name, c.model_number or "--",
                  str(c.technical_system) if c.technical_system else "--",
                  c.owner_user.username if c.owner_user else "--",
                  str(c.owner_group) if c.owner_group else "--")
                 for c in items],
                ["ID", "Name", "Model No.", "System", "Owner", "Group"],
            )

    elif kind == "design":
        items = [d for d in client.designs.all_designs() if _match(d.name)]
        if not items:
            print("(no matches)")
            return
        if args.yaml:
            _yaml([{"id":          str(d.pk),
                    "name":        d.name,
                    "owner_user":  d.owner_user.username if d.owner_user else None,
                    "owner_group": str(d.owner_group) if d.owner_group else None,
                    "description": d.description}
                   for d in items])
        else:
            _table(
                [(str(d.pk), d.name,
                  d.owner_user.username if d.owner_user else "--",
                  str(d.owner_group) if d.owner_group else "--",
                  d.description[:50] if d.description else "")
                 for d in items],
                ["ID", "Name", "Owner", "Group", "Description"],
            )

    elif kind == "instance":
        # Match on tag first; fall back to component name
        items = [i for i in client.inventory.all_instances()
                 if _match(i.tag) or _match(i.component.name)]
        if not items:
            print("(no matches)")
            return
        if args.yaml:
            _yaml([{"id":           str(i.pk),
                    "tag":          i.tag,
                    "component":    i.component.name,
                    "serial_number":i.serial_number,
                    "location":     str(i.location) if i.location else None,
                    "owner_user":   i.owner_user.username if i.owner_user else None,
                    "owner_group":  str(i.owner_group) if i.owner_group else None}
                   for i in items])
        else:
            _table(
                [(str(i.pk), i.tag or "--", i.component.name,
                  str(i.location) if i.location else "--",
                  i.owner_user.username if i.owner_user else "--",
                  str(i.owner_group) if i.owner_group else "--")
                 for i in items],
                ["ID", "Tag", "Component", "Location", "Owner", "Group"],
            )

    elif kind == "system":
        items = [s for s in client.systems.all() if _match(s.name)]
        if not items:
            print("(no matches)")
            return
        if args.yaml:
            _yaml([{"id": str(s.pk), "name": s.name,
                    "description": s.description} for s in items])
        else:
            _table(
                [(str(s.pk), s.name,
                  s.description[:70] if s.description else "") for s in items],
                ["ID", "Name", "Description"],
            )

    else:
        print(f"Unknown type {kind!r}. Choose: component, instance, design, system",
              file=sys.stderr)
        sys.exit(1)


def cmd_create_instance(client, args):
    """Create a new ComponentInstance linked to a given Component.

    Requires --user (the created record's owner_user is always that Django
    user -- there is no flag to set ownership to anyone else, by design).

    Individual flags
    ----------------
      bin/hdb --user crafts create-instance --by-name "ePIC SVT Strip Sensor" --tag SN-001
      bin/hdb --user crafts create-instance --by-pk   <UUID> --serial 20240101 --group BEMC

    YAML file input
    ---------------
      bin/hdb --user crafts create-instance --from-yaml instance.yaml
      bin/hdb --user crafts create-instance --from-yaml -          # read from stdin

    YAML file format::

      by_name: "ePIC SVT Silicon Strip Sensor"   # or: by_pk: <UUID>
      tag: SN-042
      serial: "20240315-001"
      location: "Room 112B"
      group: BEMC
      description: "Batch 7, received 2024-03-15"
    """
    # ── Load inputs: YAML file or individual flags ───────────────────────────
    if args.from_yaml:
        if args.from_yaml == "-":
            raw = sys.stdin.read()
        else:
            try:
                with open(args.from_yaml) as fh:
                    raw = fh.read()
            except OSError as exc:
                print(f"Cannot read file: {exc}", file=sys.stderr)
                sys.exit(1)
        try:
            data_in = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            print(f"Invalid YAML: {exc}", file=sys.stderr)
            sys.exit(1)
        by_name     = data_in.get("by_name")
        by_pk       = data_in.get("by_pk")
        tag         = str(data_in.get("tag", ""))
        serial      = str(data_in.get("serial", ""))
        location    = data_in.get("location")
        group       = data_in.get("group")
        description = str(data_in.get("description", ""))
    else:
        by_name     = args.by_name
        by_pk       = args.by_pk
        tag         = args.tag
        serial      = args.serial
        location    = args.location
        group       = args.group
        description = args.description

    if not by_name and not by_pk:
        print("Error: supply --by-name or --by-pk (or set by_name/by_pk in the YAML file)",
              file=sys.stderr)
        sys.exit(1)

    # ── Create instance ──────────────────────────────────────────────────────
    try:
        result = client.inventory.create(
            component_pk=by_pk,
            component_name=by_name if not by_pk else None,
            tag=tag,
            serial_number=serial,
            description=description,
            location_name=location,
            owner_group_name=group,
        )
    except Exception as exc:
        print(f"Error creating instance: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Created: {result['id']}")
    _yaml(result)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="hdb",
        description="Command-line interface to the Hardware Database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--settings", default="hdb_project.settings",
                   metavar="MODULE",
                   help="Django settings module (default: hdb_project.settings)")
    p.add_argument("--root", default=None,
                   metavar="DIR",
                   help="Project root directory (default: parent of bin/)")
    p.add_argument("--yaml", action="store_true",
                   help="Force YAML output instead of plain text tables")
    p.add_argument("--user", default=None, metavar="USERNAME",
                   help="Django username to act as for write operations "
                        "(required for create-instance)")

    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sub.add_parser("institutions",
                   help="List all institutions")

    sp = sub.add_parser("location-tree",
                        help="Location hierarchy for an institution")
    sp.add_argument("abbr", metavar="ABBR",
                    help="Institution abbreviation, e.g. BNL")

    sub.add_parser("systems",
                   help="List technical systems with component/instance counts")

    sp = sub.add_parser("search",
                        help="Cross-domain keyword search")
    sp.add_argument("query", nargs="+", metavar="WORD")

    sp = sub.add_parser("component",
                        help="Show component summary (YAML)")
    sp.add_argument("name", nargs="+", metavar="NAME")

    sp = sub.add_parser("inventory",
                        help="Show instance detail (YAML)")
    sp.add_argument("pk", metavar="PK")

    sp = sub.add_parser("where",
                        help="Where is this item right now?")
    sp.add_argument("pk", metavar="PK")

    sp = sub.add_parser("bom",
                        help="Print Bill of Materials for a design")
    sp.add_argument("name", nargs="+", metavar="DESIGN_NAME")

    sp = sub.add_parser("find",
                        help="List items matching a wildcard name pattern")
    sp.add_argument("type",
                    choices=["component", "instance", "design", "system"],
                    metavar="TYPE",
                    help="Item type: component, instance, design, or system")
    sp.add_argument("pattern", metavar="PATTERN",
                    help="Glob pattern, e.g. \"PbWO4*\" or \"*sensor*\"")

    sp = sub.add_parser("create-instance",
                        help="Create a new ComponentInstance")
    sp.add_argument("--from-yaml",   metavar="FILE",   default=None,
                    dest="from_yaml",
                    help="Read all fields from a YAML file (use - for stdin)")
    comp_grp = sp.add_mutually_exclusive_group(required=False)
    comp_grp.add_argument("--by-name", metavar="NAME", dest="by_name", default=None,
                          help="Identify the component by its exact name")
    comp_grp.add_argument("--by-pk",   metavar="PK",   dest="by_pk",   default=None,
                          help="Identify the component by its UUID primary key")
    sp.add_argument("--tag",         metavar="TAG",    default="",
                    help="Human-readable tag / label for this instance")
    sp.add_argument("--serial",      metavar="NUM",    default="",
                    help="Manufacturer serial number")
    sp.add_argument("--location",    metavar="NAME",   default=None,
                    help="Location name (must exist in the database)")
    sp.add_argument("--group",       metavar="GROUP",  default=None,
                    help="Owner group name -- must exist, and the acting "
                         "--user must belong to it (or be staff/superuser)")
    sp.add_argument("--description", metavar="TEXT",   default="",
                    help="Free-text description")

    sp = sub.add_parser("load-template",
                        help="Load DesignTemplate(s) from a YAML file (data/ by default)")
    sp.add_argument("file", metavar="FILE",
                    help="YAML file, resolved against cwd then <project root>/data/")

    sp = sub.add_parser("bom-template",
                        help="Recursive parts explosion for a DesignTemplate")
    sp.add_argument("name", nargs="+", metavar="TEMPLATE_NAME")

    sp = sub.add_parser("delete-template",
                        help="Delete an unlocked DesignTemplate (superuser-only)")
    sp.add_argument("name", nargs="+", metavar="TEMPLATE_NAME")
    sp.add_argument("--yes", action="store_true",
                    help="Skip the confirmation prompt")

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    "institutions":  cmd_institutions,
    "location-tree": cmd_location_tree,
    "systems":       cmd_systems,
    "search":        cmd_search,
    "component":     cmd_component,
    "inventory":     cmd_inventory,
    "where":         cmd_where,
    "bom":           cmd_bom,
    "find":          cmd_find,
    "create-instance": cmd_create_instance,
    "load-template": cmd_load_template,
    "bom-template":  cmd_bom_template,
    "delete-template": cmd_delete_template,
}

if __name__ == "__main__":
    parser = build_parser()
    args   = parser.parse_args()
    client = _setup(args)
    COMMANDS[args.command](client, args)
