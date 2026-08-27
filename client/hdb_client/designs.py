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

    # -- Design templates (nested blueprints) ----------------------------
    #
    # DesignTemplateElement has a real child_template FK, mirroring how
    # DesignElement.child_design lets a real Design nest another Design.
    # A multi-level assembly (e.g. Stave > Half-Stave > Stavelet) is a
    # DesignTemplate per level, with the higher level's element pointing
    # at the level below's *template* directly (not, as in an earlier
    # version of this schema, a same-named Component -- that name-matching
    # convention is gone; see client/README.md). A template can never
    # nest itself, at any depth -- DesignTemplateElement.save() enforces
    # this unconditionally (ValidationError on an attempted cycle), so
    # template_bom()'s recursion below is guaranteed to terminate on any
    # template tree that could exist at all; the _max depth guard is
    # read-time defense-in-depth on top of that, not the primary defense.

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
        ).select_related("component", "child_template", "template")

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
        product_component=None,
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

        `product_component` (optional): str | dict, same shape as an
        element's "component" (see _resolve_component()) -- the catalog
        Component representing one built instance of this template's
        assembly, e.g. so a physically completed "BTOF Stavelet" can be
        tracked as inventory. Independent of nesting: set this whether or
        not this template is ALSO used as another template's
        child_template placeholder.

        `elements` is an iterable of dicts, each specifying exactly one of
        "component" (a leaf part) or "child_template" (a nested
        sub-assembly -- must already exist, so for a multi-level hierarchy
        create/load leaf templates first):
            {
              "element_name": str,               # required
              "quantity": int,                    # default 1
              "description": str,                 # default ""
              "component": str | dict,            # see _resolve_component()
            }
        or:
            {
              "element_name": str,
              "quantity": int,
              "description": str,
              "child_template": str,               # name of an existing DesignTemplate
            }

        Returns a summary dict: {"template", "template_created", "elements": [...]}.
        Raises ValueError if an element gives both or neither of
        "component"/"child_template".

        A "child_template" element does NOT have to already exist:
        uploads are asynchronous and order-independent, so a parent
        template's YAML can be loaded before the sub-template it names
        has been uploaded at all. If the named template already exists,
        the link is validated and made immediately -- the same as
        before, and it still fails loudly here (ValidationError) if it
        would make a template nest itself, or if the referenced template
        doesn't share this one's project and owner_group (see
        DesignTemplate.can_nest). If it doesn't exist yet, the element is
        created as *pending*: this call still succeeds, and the link is
        completed automatically -- with the same validation -- whenever
        a template with that name is eventually loaded (see
        resolve_pending_template_references(), called at the end of this
        method and of load_templates_from_yaml() below). Use
        DesignTemplate.is_complete()/pending_placeholders() to check
        what, if anything, is still outstanding.
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

        product = None
        if product_component is not None:
            product, _ = self._resolve_component(product_component, default_project=project)

        template, template_created = m.DesignTemplate.objects.get_or_create(
            name=name,
            defaults={
                "project": project,
                "description": description,
                "owner_group": owner_group,
                "owner_user": owner_user,
                "product_component": product,
            },
        )

        element_results = []
        for el in elements:
            has_component = "component" in el and el["component"] is not None
            has_child     = "child_template" in el and el["child_template"] is not None
            if has_component == has_child:
                raise ValueError(
                    f"Element {el.get('element_name')!r} of template {name!r} must give "
                    f"exactly one of 'component' or 'child_template', not both or neither."
                )
            if has_child:
                child_name = el["child_template"]
                # May legitimately not exist yet -- see docstring. Only an
                # already-existing target is linked (and validated) here;
                # a not-yet-uploaded one is recorded as pending and picked
                # up later by resolve_pending_template_references().
                child_template = m.DesignTemplate.objects.filter(name=child_name).first()
                tpl_element, element_created = m.DesignTemplateElement.objects.get_or_create(
                    template=template, element_name=el["element_name"],
                    defaults={
                        "child_template": child_template,
                        "child_template_name": child_name,
                        "quantity": el.get("quantity", 1),
                        "description": el.get("description", ""),
                    },
                )
                element_results.append({
                    "element_name": el["element_name"],
                    "child_template": child_name,
                    "resolved": child_template is not None,
                    "element_created": element_created,
                })
            else:
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

        resolution = m.resolve_pending_template_references()

        return {
            "template": name,
            "template_created": template_created,
            "elements": element_results,
            "resolution": resolution,
        }

    def resolve_pending_references(self) -> dict:
        """Re-attempt every outstanding pending child_template reference
        anywhere in the database, not just ones touched by a call this
        session made -- useful after uploading templates through some
        other path (admin, a script, another user's `hdb load-template`
        run) to pick up anything that can now link. create_template() and
        load_templates_from_yaml() already call this themselves, so this
        is only needed to check on or re-trigger resolution on demand.
        Returns {"resolved": [...], "conflicts": [...]} -- see
        hdb.models.resolve_pending_template_references()."""
        return _m().resolve_pending_template_references()

    def load_templates_from_yaml(self, path) -> dict:
        """
        Load one or more DesignTemplates from a single YAML file. The
        file is either a single template document, or {"templates": [...]}
        for several.

        Uploads are asynchronous and order-independent -- see
        create_template()'s docstring -- so documents within this file,
        AND across separate calls/files/uploaders, no longer need to be
        sequenced leaf-first: a document naming a "child_template" that
        doesn't exist yet (in this file or already in the database) is
        loaded as pending and linked automatically whenever a template
        with that name eventually appears, from this file or any other.
        The whole file is loaded in one transaction, so a mid-file error
        (e.g. a malformed document) leaves nothing from this file
        committed rather than a partial load.

        Document shape (one entry of "templates:", or the whole file for a
        single template):
            template:
              name: str
              project: str                 # default "ePIC"
              description: str
              owner_group: str             # optional Group name
              owner_user: str              # optional Django username
              product_component: str|dict  # optional -- see create_template()
            elements:
              - element_name: str
                quantity: int               # default 1
                description: str
                component: str | dict       # see _resolve_component()
              # or, for a nested sub-assembly (need not exist yet -- see
              # above):
              - element_name: str
                quantity: int
                description: str
                child_template: str         # name of another template, this file or any other

        Returns {"templates": [create_template()'s summary dicts, one per
        document], "resolution": {"resolved": [...], "conflicts": [...]}}
        -- the resolution entry is from the LAST resolver pass this call
        made (each document's create_template() already re-resolves after
        itself, so this reflects the fully-settled state after the whole
        file, not just the last document alone). A non-empty "conflicts"
        list means at least one pending reference, somewhere in the
        database, names a template that exists but can't legally be
        nested where it's referenced (cycle, or project/owner_group
        mismatch) -- that needs a human to look at it; it will not
        resolve itself no matter what else is uploaded.
        """
        import yaml
        from django.db import transaction

        with open(path) as fh:
            data = yaml.safe_load(fh)

        docs = data["templates"] if isinstance(data, dict) and "templates" in data else [data]

        results = []
        with transaction.atomic():
            for doc in docs:
                tpl = doc["template"]
                results.append(self.create_template(
                    name=tpl["name"],
                    project=tpl.get("project", "ePIC"),
                    description=tpl.get("description", ""),
                    owner_group_name=tpl.get("owner_group"),
                    owner_username=tpl.get("owner_user"),
                    product_component=tpl.get("product_component"),
                    elements=doc.get("elements", []),
                ))
        resolution = results[-1]["resolution"] if results else {"resolved": [], "conflicts": []}
        return {"templates": results, "resolution": resolution}

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
        been instantiated from it) or is referenced as another template's
        child_template placeholder (child_template is PROTECTed at the DB
        level for exactly this reason -- checked explicitly here so this
        raises a clear RuntimeError instead of an unhandled
        django.db.models.deletion.ProtectedError). Returns {"template":
        name, "deleted": True} on success.
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
        if template.parent_elements.exists():
            referencing = ", ".join(
                sorted({el.template.name for el in template.parent_elements.select_related("template")})
            )
            raise RuntimeError(
                f"Design template {template.name!r} is nested as a sub-template inside: "
                f"{referencing}. Remove that placeholder (or the referencing template) first."
            )
        template_name = template.name
        template.delete()
        return {"template": template_name, "deleted": True}

    def template_bom(self, template_name: str, _depth: int = 0, _max: int = MAX_BOM_DEPTH) -> list[dict]:
        """
        Recursive parts explosion for a DesignTemplate, same shape idea as
        DesignClient.bom(): each row has "type" ("COMPONENT", "TEMPLATE",
        or "PENDING") and "ref" (the component's or nested template's
        name). A TEMPLATE row recurses into that sub-template's own
        elements; a COMPONENT row is a leaf and also carries
        "model_number". A PENDING row is a template-type placeholder
        whose named child_template hasn't been uploaded yet (see
        DesignTemplateElement.child_template_name) -- "ref" is still the
        name it's waiting on, but there's nothing to recurse into and no
        model_number, since there's no real object behind it yet. Callers
        that need to know whether a BOM is fully known should check
        DesignTemplate.is_complete() rather than scan for PENDING rows
        themselves -- that already accounts for pending references
        arbitrarily deep in the tree, not just at this level.
        """
        if _depth > _max:
            return [{"error": "max depth exceeded"}]
        rows = []
        for tel in self.template_elements(template_name):
            entry = {
                "element": tel.element_name,
                "qty": tel.quantity,
                "description": tel.description,
            }
            if tel.child_template_id:
                entry["type"] = "TEMPLATE"
                entry["ref"] = tel.child_template.name
                entry["children"] = self.template_bom(tel.child_template.name, _depth + 1, _max)
            elif tel.child_template_name:
                entry["type"] = "PENDING"
                entry["ref"] = tel.child_template_name
                entry["children"] = []
            else:
                entry["type"] = "COMPONENT"
                entry["ref"] = tel.component.name
                entry["model_number"] = tel.component.model_number
                entry["children"] = []
            rows.append(entry)
        return rows

    def designs_using_component(self, component_name: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(elements__component__name=component_name).distinct()[: _clamp(limit)])

    # -- serialized outputs --------------------------------------------

    def search_brief(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        return [ser.design_brief(d) for d in self.search(query, limit)]

    def summary(self, design_name: str) -> dict:
        return ser.design_detail(self.get(name=design_name), self.bom)
