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


# ---------------------------------------------------------------------------
# Source-file provenance helpers for load_templates_from_yaml() /
# verify_templates_from_yaml() -- see DesignTemplate.source_path/
# source_sha256/source_git_commit in hdb/models.py for what these feed.
# Deliberately independent of git: sha256 is plain file-content hashing
# (hashlib, no subprocess), so it works identically whether or not the
# file happens to be inside a git checkout, on a machine with git
# installed, or in a clean vs. dirty working tree. Only the commit-hash
# lookup below actually shells out to git, and it's best-effort -- every
# failure mode (git missing, file not in a repo, git not on PATH) just
# leaves source_git_commit blank rather than failing the load.
# ---------------------------------------------------------------------------

def _file_sha256(path) -> str:
    """SHA-256 hex digest of a file's exact raw bytes."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_commit_for(path) -> str:
    """Best-effort `git rev-parse HEAD` of the repo containing path, run
    from path's own directory so it works regardless of the caller's cwd.
    Returns "" (never raises) if git isn't installed, the file isn't
    inside a git working tree, or anything else goes wrong -- this is a
    convenience for provenance, not something a load should ever fail
    over."""
    import os
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(path)) or ".",
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _git_relative_path(path) -> str:
    """path, rewritten relative to its git repo's root when it's inside
    one (e.g. "data/btof_split/btof_stave.yaml" instead of an absolute or
    cwd-relative path that only makes sense on the machine that ran the
    load) -- falls back to path unchanged (as given to the loader) if git
    can't resolve it."""
    import os
    import subprocess
    try:
        abspath = os.path.abspath(path)
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.path.dirname(abspath) or ".",
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return str(path)
        toplevel = result.stdout.strip()
        return os.path.relpath(abspath, toplevel)
    except Exception:
        return str(path)


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

    def subsystem_fingerprint(self, name: str | None = None, pk: str | None = None) -> dict:
        """Thin wrapper over DesignTemplate.subsystem_fingerprint() -- see
        that method's docstring in hdb/models.py for the full shape and
        rationale. Raises DesignTemplate.DoesNotExist if name/pk doesn't
        match anything (same as get_template()); returns
        {"complete": False} (nothing else) if the template exists but
        isn't complete yet, or {"complete": True, **the model method's
        dict} otherwise -- "complete" is the one key both shapes always
        have, so callers can branch on it without a None check."""
        template = self.get_template(name=name, pk=pk)
        result = template.subsystem_fingerprint()
        if result is None:
            return {"complete": False}
        return {"complete": True, **result}

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
        source_path: str | None = None,
        source_sha256: str | None = None,
        source_git_commit: str | None = None,
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

        `source_path`/`source_sha256`/`source_git_commit`: internal --
        set by load_templates_from_yaml() to stamp provenance on the
        template (see DesignTemplate.source_path et al. in hdb/models.py).
        Leave unset when calling this directly with in-memory data; there
        is no file behind it to record.
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

        defaults = {
            "project": project,
            "description": description,
            "owner_group": owner_group,
            "owner_user": owner_user,
            "product_component": product,
        }
        if source_path is not None:
            defaults["source_path"] = source_path
            defaults["source_sha256"] = source_sha256 or ""
            defaults["source_git_commit"] = source_git_commit or ""
        if self.user is not None:
            defaults["created_by"] = self.user

        template, template_created = m.DesignTemplate.objects.get_or_create(
            name=name,
            defaults=defaults,
        )

        # get_or_create()'s defaults= only apply when the row is first
        # created -- re-stamp explicitly on every call, including a no-op
        # reload of an already-existing template, so source_* and
        # modified_by/modified_on always describe the most recent load
        # this template was involved in, not just whichever load happened
        # to create the row in the first place.
        if source_path is not None or self.user is not None:
            if source_path is not None:
                template.source_path = source_path
                template.source_sha256 = source_sha256 or ""
                template.source_git_commit = source_git_commit or ""
            if self.user is not None:
                template.modified_by = self.user
            template.save()

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

        Every template this call touches also gets its source_path/
        source_sha256/source_git_commit stamped (see DesignTemplate in
        hdb/models.py) -- automatically, no extra step -- recording
        exactly which file, which exact bytes, and (best-effort) which
        git commit this load came from. Re-running this on an unchanged
        file still re-stamps them, so they always reflect the most recent
        load, not just template creation. Use `hdb verify-template` /
        verify_templates_from_yaml() to check a tracked file's current
        content against what's actually live.
        """
        import yaml
        from django.db import transaction

        with open(path) as fh:
            data = yaml.safe_load(fh)

        docs = data["templates"] if isinstance(data, dict) and "templates" in data else [data]

        # Provenance: one file content hash / git commit shared by every
        # template this file defines (a file can hold several "templates:"
        # documents) -- computed once up front, not per document. See the
        # helpers above and DesignTemplate.source_path et al.
        file_source_path = _git_relative_path(path)
        file_sha256 = _file_sha256(path)
        file_git_commit = _git_commit_for(path)

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
                    source_path=file_source_path,
                    source_sha256=file_sha256,
                    source_git_commit=file_git_commit,
                ))
        resolution = results[-1]["resolution"] if results else {"resolved": [], "conflicts": []}
        return {"templates": results, "resolution": resolution}

    def verify_templates_from_yaml(self, path) -> dict:
        """
        Read-only drift check: parse a YAML file exactly as
        load_templates_from_yaml() would, but never write anything --
        instead, for every template the file defines, compare the file's
        current content against what's actually live in the database and
        report any mismatch.

        This is the enforcement half of source_path/source_sha256 (see
        DesignTemplate in hdb/models.py): removing the web UI's per-row
        quantity/delete controls closed one way the database could drift
        from the tracked YAML, but Django admin and the ORM/shell can
        still write to a template directly (deliberately -- see
        client/README.md's note on removing a single placeholder). This
        can't prevent that; it can tell you it happened, and exactly
        which field disagrees.

        Returns:
            {
              "path": path,
              "file_sha256": <current hash of the file on disk right now>,
              "templates": [
                {
                  "name": str,
                  "in_db": bool,                 # False: not loaded at all yet
                  "source_sha256_recorded": str,  # "" if never loaded from a file
                  "file_changed_since_load": bool | None,  # None if in_db is False
                                                            # or never loaded from a file
                  "field_diffs": {field: {"yaml": ..., "db": ...}, ...},
                  "elements": [
                    {
                      "element_name": str,
                      "in_db": bool,              # False: this placeholder isn't
                                                   # loaded (or was removed some
                                                   # other way -- see client/README.md)
                      "field_diffs": {field: {"yaml": ..., "db": ...}, ...},
                    }, ...
                  ],
                  "db_only_elements": [str, ...],  # live placeholders this file
                                                    # doesn't mention at all
                }, ...
              ],
              "has_drift": bool,  # True if ANY field_diffs or db_only_elements
                                  # exist anywhere above -- file_changed_since_load
                                  # is informational only and doesn't set this,
                                  # since "haven't re-loaded yet" isn't drift by
                                  # itself, just a reason to run load-template.
            }
        """
        import yaml

        m = _m()

        with open(path) as fh:
            data = yaml.safe_load(fh)
        docs = data["templates"] if isinstance(data, dict) and "templates" in data else [data]

        file_sha256 = _file_sha256(path)

        def _spec_name(spec):
            """The name a component/product_component spec declares,
            without resolving or creating anything (spec is a plain
            string, or a dict with at least "name") -- see
            _resolve_component(), which this deliberately does NOT call:
            verify must never write."""
            return spec if isinstance(spec, str) else spec.get("name")

        template_reports = []
        has_drift = False

        for doc in docs:
            tpl = doc["template"]
            name = tpl["name"]
            template = m.DesignTemplate.objects.filter(name=name).select_related(
                "owner_group", "owner_user", "product_component",
            ).prefetch_related("elements__component", "elements__child_template").first()

            report = {
                "name": name,
                "in_db": template is not None,
                "source_sha256_recorded": "",
                "file_changed_since_load": None,
                "field_diffs": {},
                "elements": [],
                "db_only_elements": [],
            }

            if template is None:
                template_reports.append(report)
                continue

            report["source_sha256_recorded"] = template.source_sha256
            if template.source_sha256:
                report["file_changed_since_load"] = (template.source_sha256 != file_sha256)

            expected = {
                "project": tpl.get("project", "ePIC"),
                "description": tpl.get("description", ""),
                "owner_group": tpl.get("owner_group"),
                "owner_user": tpl.get("owner_user"),
                "product_component": (
                    _spec_name(tpl["product_component"]) if tpl.get("product_component") else None
                ),
            }
            actual = {
                "project": template.project,
                "description": template.description,
                "owner_group": template.owner_group.name if template.owner_group else None,
                "owner_user": template.owner_user.username if template.owner_user else None,
                "product_component": template.product_component.name if template.product_component else None,
            }
            for field, yaml_val in expected.items():
                if yaml_val != actual[field]:
                    report["field_diffs"][field] = {"yaml": yaml_val, "db": actual[field]}
            if report["field_diffs"]:
                has_drift = True

            elements_by_name = {el.element_name: el for el in template.elements.all()}
            seen_names = set()

            for el in doc.get("elements", []):
                el_name = el["element_name"]
                seen_names.add(el_name)
                db_el = elements_by_name.get(el_name)
                el_report = {"element_name": el_name, "in_db": db_el is not None, "field_diffs": {}}

                if db_el is not None:
                    expected_el = {
                        "quantity": el.get("quantity", 1),
                        "description": el.get("description", ""),
                    }
                    actual_el = {
                        "quantity": db_el.quantity,
                        "description": db_el.description,
                    }
                    if "child_template" in el and el["child_template"] is not None:
                        expected_el["child_template_name"] = el["child_template"]
                        actual_el["child_template_name"] = db_el.child_template_name
                    else:
                        expected_el["component_name"] = _spec_name(el.get("component"))
                        actual_el["component_name"] = db_el.component.name if db_el.component else None

                    for field, yaml_val in expected_el.items():
                        if yaml_val != actual_el[field]:
                            el_report["field_diffs"][field] = {"yaml": yaml_val, "db": actual_el[field]}
                    if el_report["field_diffs"]:
                        has_drift = True

                report["elements"].append(el_report)

            report["db_only_elements"] = sorted(set(elements_by_name) - seen_names)
            if report["db_only_elements"]:
                has_drift = True

            template_reports.append(report)

        return {"path": str(path), "file_sha256": file_sha256, "templates": template_reports, "has_drift": has_drift}

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
