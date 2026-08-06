"""
DesignClient — query the Design Library.

User-scoped: pass the authenticated Django User in via the constructor.
`user=None` (used by the CLI / trusted shell) applies no scoping.
"""
from ._bootstrap import _m
from . import access
from . import serializers as ser

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_BOM_DEPTH = 10


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


class DesignClient:
    def __init__(self, user=None):
        self.user = user

    def _qs(self):
        qs = _m().Design.objects.select_related("owner_group", "owner_user")
        return access.visible_to(qs, self.user)

    def all_designs(self):
        """All visible designs (used by the CLI's `find design` command)."""
        return self._qs()

    def get(self, name: str | None = None, pk: str | None = None):
        qs = access.visible_to(_m().Design.objects, self.user)
        if pk:
            return qs.get(pk=pk)
        return qs.get(name=name)

    def by_project(self, project: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(project__iexact=project)[: _clamp(limit)])

    def search(self, query: str, limit: int = DEFAULT_LIMIT):
        from django.db.models import Q

        qs = self._qs().filter(Q(name__icontains=query) | Q(description__icontains=query))
        return list(qs[: _clamp(limit)])

    def elements_of(self, design_name: str):
        return _m().DesignElement.objects.filter(design__name=design_name).select_related(
            "component", "child_design"
        ).prefetch_related("installed_instances__instance")

    def bom(self, design_name: str, _depth: int = 0, _max: int = MAX_BOM_DEPTH) -> list[dict]:
        if _depth > _max:
            return [{"error": "max depth exceeded"}]
        rows = []
        for el in self.elements_of(design_name):
            entry = {
                "element": el.element_name,
                "type": el.element_type(),
                "qty": el.quantity,
                "description": el.description,
            }
            if el.child_design:
                entry["ref"] = el.child_design.name
                entry["children"] = self.bom(el.child_design.name, _depth + 1, _max)
            else:
                entry["ref"] = el.component.name if el.component else None
                entry["model_number"] = el.component.model_number if el.component else None
                # A DesignElement with quantity > 1 can have more than one
                # installed instance (one per slot) -- installed_instances is
                # the plural reverse relation, there is no singular FK here.
                entry["installed_ids"] = [
                    str(dei.instance_id) for dei in el.installed_instances.all()
                ]
                entry["children"] = []
            rows.append(entry)
        return rows

    def flat_component_list(self, design_name: str) -> list[dict]:
        def _flat(rows):
            out = []
            for r in rows:
                if r["type"] == "COMPONENT":
                    out.append(r)
                out.extend(_flat(r.get("children", [])))
            return out

        return _flat(self.bom(design_name))

    # -- Design templates (flat blueprints) -----------------------------
    #
    # DesignTemplateElement.component is a required FK to a real catalog
    # Component -- there is no "sub-template" reference on the model (see
    # client/README.md for why). To represent a multi-level assembly
    # (e.g. Stave > Half-Stave > Stavelet) with flat templates, each
    # intermediate level is modeled as its own catalog Component *and* its
    # own DesignTemplate of the SAME NAME -- template_bom() below follows
    # that name match to recurse, the same way a real BOM follows
    # child_design. This is a naming convention, not a schema constraint,
    # so keep template names and their corresponding assembly Component
    # names identical when authoring a multi-level template set.

    def all_templates(self):
        return _m().DesignTemplate.objects.select_related("owner_group", "owner_user")

    def get_template(self, name: str | None = None, pk: str | None = None):
        qs = _m().DesignTemplate.objects
        if pk:
            return qs.get(pk=pk)
        return qs.get(name=name)

    def template_elements(self, template_name: str):
        return _m().DesignTemplateElement.objects.filter(
            template__name=template_name
        ).select_related("component", "template")

    def _resolve_component(self, spec, default_project: str = "ePIC"):
        """
        Resolve one template element's `component` spec into a Component.

        `spec` is either:
          - a plain string: an EXISTING component, looked up by
            (name, project) -- raises Component.DoesNotExist if it isn't
            already in the catalog.
          - a dict with at least "name": get_or_create'd (never clobbers
            an existing row), optionally auto-creating its
            `technical_system` (and that system's `group`) too if given
            and not already present.

        A newly-created Component's `owner_group` is set to its
        `technical_system`'s `group` (read from the TechnicalSystem row
        itself, not just this spec's own `technical_system_group` key) --
        so every component sharing a `technical_system` inherits the same
        owner_group even if only the first spec that creates that
        TechnicalSystem actually specifies `technical_system_group` (the
        common case in a multi-element YAML file: repeating the group on
        every element referencing the same technical_system would be
        redundant). A component with no `technical_system` gets no
        owner_group either -- there's nothing to inherit from.

        Returns (component, created).
        """
        m = _m()
        if isinstance(spec, str):
            return m.Component.objects.get(name=spec, project=default_project), False

        name = spec["name"]
        project = spec.get("project", default_project)

        technical_system = None
        ts_name = spec.get("technical_system")
        if ts_name:
            ts_group = None
            ts_group_name = spec.get("technical_system_group")
            if ts_group_name:
                from django.contrib.auth.models import Group
                ts_group, _ = Group.objects.get_or_create(name=ts_group_name)
            technical_system, _ = m.TechnicalSystem.objects.get_or_create(
                name=ts_name, defaults={"group": ts_group},
            )

        component, created = m.Component.objects.get_or_create(
            name=name, project=project,
            defaults={
                "model_number": spec.get("model_number", ""),
                "description": spec.get("description", ""),
                "technical_system": technical_system,
                "owner_group": technical_system.group if technical_system else None,
            },
        )
        return component, created

    def create_template(
        self,
        name: str,
        project: str = "ePIC",
        description: str = "",
        owner_group_name: str | None = None,
        owner_username: str | None = None,
        elements=(),
    ) -> dict:
        """
        Idempotent create of a DesignTemplate and its DesignTemplateElements
        from plain data -- safe to re-run (existing rows are never
        clobbered; only missing ones are created). This is an
        administrative/curator operation akin to `seed_hdb` (it loads
        reference/catalog data), not a per-request scoped write like
        `InventoryClient.create()` -- `owner_group_name`/`owner_username`
        are recorded on the template if given, but there is no requirement
        that `self.user` belong to that group.

        `elements` is an iterable of dicts:
            {
              "element_name": str,               # required
              "quantity": int,                    # default 1
              "description": str,                 # default ""
              "component": str | dict,            # see _resolve_component()
            }

        Returns a summary dict: {"template", "template_created", "elements": [...]}.
        """
        m = _m()

        owner_group = None
        if owner_group_name:
            from django.contrib.auth.models import Group
            owner_group, _ = Group.objects.get_or_create(name=owner_group_name)

        owner_user = None
        if owner_username:
            from django.contrib.auth.models import User
            owner_user = User.objects.get(username=owner_username)

        template, template_created = m.DesignTemplate.objects.get_or_create(
            name=name,
            defaults={
                "project": project,
                "description": description,
                "owner_group": owner_group,
                "owner_user": owner_user,
            },
        )

        element_results = []
        for el in elements:
            component, component_created = self._resolve_component(
                el["component"], default_project=project,
            )
            tpl_element, element_created = m.DesignTemplateElement.objects.get_or_create(
                template=template, element_name=el["element_name"],
                defaults={
                    "component": component,
                    "quantity": el.get("quantity", 1),
                    "description": el.get("description", ""),
                },
            )
            element_results.append({
                "element_name": el["element_name"],
                "component": component.name,
                "component_created": component_created,
                "element_created": element_created,
            })

        return {
            "template": name,
            "template_created": template_created,
            "elements": element_results,
        }

    def load_templates_from_yaml(self, path) -> list[dict]:
        """
        Load one or more DesignTemplates from a YAML file. The file is
        either a single template document, or {"templates": [...]} for
        several -- loaded top to bottom, so for a multi-level hierarchy
        list leaf templates first (each level's assembly Component must
        already exist, or be defined earlier in the same file, before a
        higher level can reference it).

        Document shape (one entry of "templates:", or the whole file for a
        single template):
            template:
              name: str
              project: str            # default "ePIC"
              description: str
              owner_group: str        # optional Group name
              owner_user: str         # optional Django username
            elements:
              - element_name: str
                quantity: int          # default 1
                description: str
                component: str | dict  # see _resolve_component()

        Returns a list of create_template()'s summary dicts, one per
        template document.
        """
        import yaml

        with open(path) as fh:
            data = yaml.safe_load(fh)

        docs = data["templates"] if isinstance(data, dict) and "templates" in data else [data]

        results = []
        for doc in docs:
            tpl = doc["template"]
            results.append(self.create_template(
                name=tpl["name"],
                project=tpl.get("project", "ePIC"),
                description=tpl.get("description", ""),
                owner_group_name=tpl.get("owner_group"),
                owner_username=tpl.get("owner_user"),
                elements=doc.get("elements", []),
            ))
        return results

    def delete_template(self, name: str | None = None, pk: str | None = None) -> dict:
        """
        Delete a DesignTemplate outright -- superuser-only, and only while
        the template is unlocked (no Design has ever been instantiated
        from it). Mirrors the web UI's template_delete view/policy exactly:
        deletion is stricter than editing (any owner_group member may add
        or remove placeholders on an unlocked template) because removing
        the template removes it for every group member at once, not just
        the caller's own view of it.

        Raises PermissionError if self.user isn't a superuser, or
        RuntimeError if the template is locked (at least one Design has
        been instantiated from it). Returns {"template": name, "deleted":
        True} on success.
        """
        if self.user is None or not self.user.is_superuser:
            who = self.user.username if self.user else "<anonymous>"
            raise PermissionError(
                f"User {who!r} is not a superuser and cannot delete design templates."
            )
        template = self.get_template(name=name, pk=pk)
        if template.designs.exists():
            raise RuntimeError(
                f"Design template {template.name!r} is locked -- "
                f"{template.designs.count()} design(s) have been instantiated from it."
            )
        template_name = template.name
        template.delete()
        return {"template": template_name, "deleted": True}

    def template_bom(self, template_name: str, _depth: int = 0, _max: int = MAX_BOM_DEPTH) -> list[dict]:
        """
        Recursive parts explosion for a flat DesignTemplate: for each
        element, if a DesignTemplate exists with the SAME NAME as the
        element's Component, recurse into it (see the naming-convention
        note above the "Design templates" section); otherwise it's a leaf
        part.
        """
        if _depth > _max:
            return [{"error": "max depth exceeded"}]
        m = _m()
        rows = []
        for tel in self.template_elements(template_name):
            entry = {
                "element": tel.element_name,
                "component": tel.component.name,
                "model_number": tel.component.model_number,
                "qty": tel.quantity,
                "description": tel.description,
            }
            sub_template = m.DesignTemplate.objects.filter(name=tel.component.name).first()
            entry["children"] = (
                self.template_bom(sub_template.name, _depth + 1, _max) if sub_template else []
            )
            rows.append(entry)
        return rows

    def designs_using_component(self, component_name: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(elements__component__name=component_name).distinct()[: _clamp(limit)])

    # -- serialized outputs --------------------------------------------

    def search_brief(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        return [ser.design_brief(d) for d in self.search(query, limit)]

    def summary(self, design_name: str) -> dict:
        return ser.design_detail(self.get(name=design_name), self.bom)
