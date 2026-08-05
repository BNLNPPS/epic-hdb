"""
Plain-dict "brief" / "detail" shapers for the Hardware Database (HDB) client
layer -- used by the CLI (client/hdb.py) and the MCP server
(client/mcp_server.py) to turn Django model instances into JSON-safe dicts.

Deliberately NOT Django REST Framework serializers (those live in
hdb/serializers.py and back the web app's /api/ endpoints instead) -- these
functions are plain, dependency-light, and flatten related objects down to
their display string / username / name rather than nesting id+name pairs,
matching the style already used by hdb_client/client.py's where_is().

Naming convention:
  *_brief()  -- compact fields for search/list results.
  *_detail() -- full fields for a single-record summary, including nested
               properties/log_entries where the model has them.
"""
from __future__ import annotations


def _iso(dt):
    return dt.isoformat() if dt else None


def _property_brief(pv) -> dict:
    return {
        "id": str(pv.pk),
        "property_type": pv.property_type.name if pv.property_type_id else None,
        "tag": pv.tag,
        "value": pv.value,
        "units": pv.units,
        "description": pv.description,
    }


def _log_entry_brief(entry) -> dict:
    return {
        "id": str(entry.pk),
        "timestamp": _iso(entry.timestamp),
        "topic": entry.topic,
        "entry": entry.entry,
        "logged_by": entry.logged_by.username if entry.logged_by_id else None,
    }


# -- Component Catalog -------------------------------------------------------

def component_brief(c) -> dict:
    return {
        "id": str(c.pk),
        "name": c.name,
        "model_number": c.model_number,
        "project": c.project,
        "technical_system": str(c.technical_system) if c.technical_system_id else None,
        "owner_group": str(c.owner_group) if c.owner_group_id else None,
        "instance_count": c.instances.count(),
    }


def component_detail(c) -> dict:
    return {
        "id": str(c.pk),
        "name": c.name,
        "alternate_name": c.alternate_name,
        "model_number": c.model_number,
        "description": c.description,
        "project": c.project,
        "technical_system": str(c.technical_system) if c.technical_system_id else None,
        "owner_user": c.owner_user.username if c.owner_user_id else None,
        "owner_group": str(c.owner_group) if c.owner_group_id else None,
        "group_writeable": c.group_writeable,
        "created_on": _iso(c.created_on),
        "modified_on": _iso(c.modified_on),
        "instance_count": c.instances.count(),
        "sources": [
            {
                "source": cs.source.name,
                "part_number": cs.part_number,
                "cost": str(cs.cost) if cs.cost is not None else None,
                "role": cs.role,
            }
            for cs in c.componentsource_set.select_related("source").all()
        ],
        "properties": [_property_brief(pv) for pv in c.properties.select_related("property_type").all()],
        "log_entries": [_log_entry_brief(e) for e in c.log_entries.all()],
    }


# -- Component Inventory ------------------------------------------------------

def instance_brief(i) -> dict:
    return {
        "id": str(i.pk),
        "tag": i.tag,
        "serial_number": i.serial_number,
        "component": i.component.name if i.component_id else None,
        "location": str(i.location) if i.location_id else None,
        "owner_group": str(i.owner_group) if i.owner_group_id else None,
    }


def instance_detail(i) -> dict:
    return {
        "id": str(i.pk),
        "tag": i.tag,
        "serial_number": i.serial_number,
        "description": i.description,
        "component": i.component.name if i.component_id else None,
        "technical_system": str(i.technical_system) if i.technical_system_id else None,
        "location": str(i.location) if i.location_id else None,
        "owner_user": i.owner_user.username if i.owner_user_id else None,
        "owner_group": str(i.owner_group) if i.owner_group_id else None,
        "group_writeable": i.group_writeable,
        "created_on": _iso(i.created_on),
        "modified_on": _iso(i.modified_on),
        "properties": [_property_brief(pv) for pv in i.properties.select_related("property_type").all()],
        "log_entries": [_log_entry_brief(e) for e in i.log_entries.all()],
    }


# -- Designs -------------------------------------------------------------------

def design_brief(d) -> dict:
    return {
        "id": str(d.pk),
        "name": d.name,
        "description": d.description,
        "project": d.project,
        "owner_group": str(d.owner_group) if d.owner_group_id else None,
        "element_count": d.elements.count(),
    }


def design_detail(d, bom_fn) -> dict:
    return {
        "id": str(d.pk),
        "name": d.name,
        "description": d.description,
        "project": d.project,
        "owner_user": d.owner_user.username if d.owner_user_id else None,
        "owner_group": str(d.owner_group) if d.owner_group_id else None,
        "group_writeable": d.group_writeable,
        "created_on": _iso(d.created_on),
        "modified_on": _iso(d.modified_on),
        "element_count": d.elements.count(),
        "bom": bom_fn(d.name),
    }


# -- Institutions ----------------------------------------------------------------

def institution_brief(inst) -> dict:
    return {
        "id": str(inst.pk),
        "name": inst.name,
        "abbreviation": inst.abbreviation,
        "country": inst.country,
        "city": inst.city,
        "url": inst.url,
        "description": inst.description,
        "location_count": inst.locations.count(),
    }
