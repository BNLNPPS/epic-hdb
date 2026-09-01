"""
Hardware Database (HDB) models.
Three primary domains: Component Catalog, Component Inventory, Design.
Supporting: Institution, Location, Ownership, Properties, Logs.
Groups use Django's built-in auth.Group.
"""

import hashlib
import uuid
from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.auth.models import User, Group
from django.utils import timezone


# ---------------------------------------------------------------------------
# Supporting tables
# ---------------------------------------------------------------------------

class Institution(models.Model):
    """
    Top-level site anchor (BNL, CERN, Fermilab, …).
    Locations belong to an institution, enabling multi-site inventory tracking.
    """
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    name         = models.CharField(max_length=128, unique=True)
    abbreviation = models.CharField(max_length=16,  blank=True)
    country      = models.CharField(max_length=64,  blank=True)
    city         = models.CharField(max_length=64,  blank=True)
    url          = models.URLField(blank=True)
    description  = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return self.abbreviation if self.abbreviation else self.name

    class Meta:
        ordering = ["name"]


class Location(models.Model):
    """
    Physical location hierarchy within an institution:
    building → room → cabinet → shelf.
    Every location is anchored to exactly one Institution.
    """
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    LOCATION_TYPES = [
        ("building", "Building"),
        ("room",     "Room"),
        ("cabinet",  "Cabinet"),
        ("shelf",    "Shelf"),
        ("other",    "Other"),
    ]
    name          = models.CharField(max_length=128)
    location_type = models.CharField(max_length=16, choices=LOCATION_TYPES, default="room")
    # Mandatory -- every location is anchored to exactly one institution (see
    # the class docstring), so this can never be left blank. PROTECT rather
    # than CASCADE/SET_NULL: deleting an Institution that still has
    # Locations attached would either silently orphan them (impossible now
    # that this is required) or wipe out inventory location history, so it's
    # blocked instead until the locations are reassigned or removed first.
    institution   = models.ForeignKey(
        Institution, on_delete=models.PROTECT, related_name="locations"
    )
    parent = models.ForeignKey(
        "self", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="children"
    )
    description = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def full_path(self):
        """Return slash-separated path: Institution / Building / Room / …
        institution is a required field, so it's always there to anchor
        the path -- no None-guard needed."""
        parts = []
        node = self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        parts.append(str(self.institution))
        return " / ".join(reversed(parts))

    def __str__(self):
        return self.full_path()

    class Meta:
        ordering = ["name"]


class PropertyType(models.Model):
    """Predefined property types (extensible by admins)."""
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    HANDLER_CHOICES = [
        ("",                  "None"),
        ("pdmlink",           "PDMLink"),
        ("component_design",  "Component Design"),
        ("traveler_template", "Traveler Template"),
        ("traveler_instance", "Traveler Instance"),
        ("document",          "Document"),
        ("image",             "Image"),
        ("http_link",         "HTTP Link"),
        ("currency",          "Currency"),
        ("boolean",           "Boolean"),
        ("date",              "Date"),
    ]
    CATEGORY_CHOICES = [
        ("physical",       "Physical"),
        ("documentation",  "Documentation"),
        ("qa",             "QA"),
        ("lattice",        "Lattice"),
        ("safety",         "Safety"),
        ("maintenance",    "Maintenance"),
        ("design",         "Design"),
        ("status",         "Status"),
        ("other",          "Other"),
    ]
    name          = models.CharField(max_length=128, unique=True)
    category      = models.CharField(max_length=32, choices=CATEGORY_CHOICES, default="other")
    handler       = models.CharField(max_length=32, choices=HANDLER_CHOICES, blank=True, default="")
    description   = models.TextField(blank=True)
    default_units = models.CharField(max_length=64, blank=True)
    default_value = models.CharField(max_length=256, blank=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]


# ---------------------------------------------------------------------------
# Abstract base: ownership + timestamps
# ---------------------------------------------------------------------------

class OwnedModel(models.Model):
    owner_user      = models.ForeignKey(User,  null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    owner_group     = models.ForeignKey(Group, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    group_writeable = models.BooleanField(default=False)
    created_by      = models.ForeignKey(User,  null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_on      = models.DateTimeField(default=timezone.now, editable=False)
    modified_by     = models.ForeignKey(User,  null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    modified_on     = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Cross-domain: PropertyValue and LogEntry
# ---------------------------------------------------------------------------

class PropertyValue(models.Model):
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    property_type = models.ForeignKey(PropertyType, on_delete=models.CASCADE)
    tag           = models.CharField(max_length=128, blank=True)
    value         = models.TextField(blank=True)
    # For handler="document"/"image" property types: an actual uploaded file.
    # value is still used as a fallback for a plain pasted URL (e.g. a link
    # to an externally-hosted datasheet) when no file is attached.
    file          = models.FileField(upload_to="property_files/", null=True, blank=True)
    units         = models.CharField(max_length=64,  blank=True)
    description   = models.TextField(blank=True)
    is_dynamic    = models.BooleanField(default=False)
    user_writable = models.BooleanField(default=True)

    # One of these FKs is set; the rest are NULL
    component          = models.ForeignKey("Component",         null=True, blank=True, on_delete=models.CASCADE, related_name="properties")
    component_instance = models.ForeignKey("ComponentInstance", null=True, blank=True, on_delete=models.CASCADE, related_name="properties")
    design             = models.ForeignKey("Design",            null=True, blank=True, on_delete=models.CASCADE, related_name="properties")
    design_element     = models.ForeignKey("DesignElement",     null=True, blank=True, on_delete=models.CASCADE, related_name="properties")

    created_on  = models.DateTimeField(default=timezone.now, editable=False)
    modified_on = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.property_type.name}: {self.value[:40]}"

    class Meta:
        ordering = ["property_type__name"]


class LogEntry(models.Model):
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    TOPIC_CHOICES = [
        ("",             "General"),
        ("installation", "Installation"),
        ("inventory",    "Inventory"),
        ("design",       "Design"),
        ("maintenance",  "Maintenance"),
        ("inspection",   "Inspection"),
        ("repair",       "Repair"),
        ("decommission", "Decommission"),
        ("other",        "Other"),
    ]
    topic      = models.CharField(max_length=32, choices=TOPIC_CHOICES, blank=True, default="")
    entry      = models.TextField()
    attachment = models.FileField(upload_to="log_attachments/", null=True, blank=True)
    logged_by  = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="hdb_log_entries")
    timestamp  = models.DateTimeField(default=timezone.now)

    component          = models.ForeignKey("Component",         null=True, blank=True, on_delete=models.CASCADE, related_name="log_entries")
    component_instance = models.ForeignKey("ComponentInstance", null=True, blank=True, on_delete=models.CASCADE, related_name="log_entries")
    design             = models.ForeignKey("Design",            null=True, blank=True, on_delete=models.CASCADE, related_name="log_entries")

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d}] {self.entry[:60]}"

    class Meta:
        ordering = ["-timestamp"]


# ---------------------------------------------------------------------------
# Domain 1 — Component Catalog
# ---------------------------------------------------------------------------

class TechnicalSystem(models.Model):
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    name        = models.CharField(max_length=64, unique=True)
    description = models.TextField(blank=True)
    group       = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="technical_systems",
        help_text="Django auth Group responsible for this technical system.",
    )

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]


class Source(models.Model):
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    name          = models.CharField(max_length=256, unique=True)
    contact_email = models.EmailField(blank=True)
    url           = models.URLField(blank=True)
    address       = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]


class Component(OwnedModel):
    id               = models.CharField(max_length=36, primary_key=True, editable=False)
    name             = models.CharField(max_length=256)
    alternate_name   = models.CharField(max_length=256, blank=True)
    model_number     = models.CharField(max_length=128, blank=True)
    description      = models.TextField(blank=True)
    project          = models.CharField(max_length=64,  blank=True, default="ePIC")
    technical_system = models.ForeignKey(TechnicalSystem, null=True, blank=True, on_delete=models.SET_NULL, related_name="components")
    sources          = models.ManyToManyField(Source, through="ComponentSource", blank=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]
        unique_together = [("name", "project")]


class ComponentSource(models.Model):
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    ROLE_CHOICES = [
        ("vendor",       "Vendor"),
        ("manufacturer", "Manufacturer"),
        ("both",         "Vendor & Manufacturer"),
    ]
    component   = models.ForeignKey(Component, on_delete=models.CASCADE)
    source      = models.ForeignKey(Source,    on_delete=models.CASCADE)
    part_number = models.CharField(max_length=128, blank=True)
    cost        = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    role        = models.CharField(max_length=16, choices=ROLE_CHOICES, default="vendor")
    description = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.source.name} → {self.component.name}"

    class Meta:
        unique_together = [("component", "source")]


# ---------------------------------------------------------------------------
# Domain 2 — Component Inventory
# ---------------------------------------------------------------------------

class ComponentInstance(OwnedModel):
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    tag              = models.CharField(max_length=128, blank=True)
    serial_number    = models.CharField(max_length=128, blank=True)
    component        = models.ForeignKey(Component,       on_delete=models.PROTECT,  related_name="instances")
    technical_system = models.ForeignKey(TechnicalSystem, null=True, blank=True, on_delete=models.SET_NULL, related_name="component_instances")
    # on_delete=PROTECT (not SET_NULL): deleting a Location that still has
    # instances stored there would otherwise silently blank out their
    # location for everyone who owns one, with the person deleting the
    # Location having no obvious reason to think about inventory at all.
    # null=True/blank=True stay -- that's a separate question (whether a
    # user should ever be able to explicitly clear an instance's own
    # location) from what happens to instances when the Location itself
    # is deleted out from under them.
    location         = models.ForeignKey(Location,        null=True, blank=True, on_delete=models.PROTECT, related_name="instances")
    description      = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        """Inherit technical_system from component if not explicitly set."""
        if not self.id:
            self.id = str(uuid.uuid4())
        if self.technical_system_id is None and self.component_id:
            self.technical_system = self.component.technical_system
        super().save(*args, **kwargs)

    def effective_properties(self):
        """Properties as they should be displayed/used for this instance:
        the instance's own PropertyValue rows, plus its Component's rows for
        any (property_type, tag) pair the instance hasn't overridden.

        Matching on (property_type, tag) -- not property_type alone -- because
        a single object can legitimately hold more than one PropertyValue of
        the same type (e.g. two "Document" properties tagged "Datasheet" and
        "Photo"); overriding one shouldn't hide the other. Rows with
        component_instance_id == None in the result are inherited defaults;
        rows with it set are the instance's own (added or overriding).
        """
        own = list(self.properties.select_related('property_type').all())
        overridden = {(pv.property_type_id, pv.tag) for pv in own}
        inherited = [
            pv for pv in self.component.properties.select_related('property_type').all()
            if (pv.property_type_id, pv.tag) not in overridden
        ]
        return sorted(inherited + own, key=lambda pv: (pv.property_type.name, pv.tag))

    def __str__(self):
        label = self.tag or str(self.pk)[:8]
        return f"{label} ({self.component.name})"

    class Meta:
        ordering = ["component", "-created_on"]


# ---------------------------------------------------------------------------
# Domain 3 — Designs
# ---------------------------------------------------------------------------

# DesignTemplateElement.child_template lets one template nest another as a
# placeholder, mirroring how DesignElement.child_design does the same for
# real Designs (see that model). A template can never contain itself, at any
# depth -- it's a real physical constraint, not just a data-hygiene rule: an
# assembly cannot be one of its own sub-assemblies. DesignTemplateElement.save()
# enforces this unconditionally (every write path -- web view, admin,
# hdb_client, a raw script -- goes through it), and DesignTemplate.nesting_levels
# below is bounded by the same TEMPLATE_NESTING_MAX_DEPTH as read-time
# defense-in-depth, in case a row is ever created by something that bypasses
# .save() entirely (bulk_create, raw SQL).
TEMPLATE_NESTING_MAX_DEPTH = 10


class DesignTemplate(OwnedModel):
    """
    Reusable blueprint for a Design. Template elements reference either a
    catalog Component or another DesignTemplate as *placeholders* -- they
    say "this assembly needs 4 SiPMs" or "this assembly needs 144
    Half-Staves", not specific serialized items. When a user instantiates a
    template, a real Design is created with one DesignElement per
    placeholder (recursively instantiating a child Design for every nested
    sub-template placeholder too -- see design_list in views_web.py); the
    editing tools on the design detail page then let the owning group
    replace each leaf placeholder with an actual ComponentInstance from the
    inventory as the physical assembly is built.
    """
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    name        = models.CharField(max_length=256, unique=True)
    description = models.TextField(blank=True)
    project     = models.CharField(max_length=64, blank=True, default="ePIC")
    product_component = models.ForeignKey(
        Component, null=True, blank=True, on_delete=models.SET_NULL, related_name="product_of_templates",
        help_text="Catalog Component representing one built/assembled instance of this "
                   "template -- e.g. the 'BTOF Stavelet' template's product_component is the "
                   "'BTOF Stavelet' Component, so a physically completed stavelet can be "
                   "tracked as a ComponentInstance like any other part. Independent of "
                   "nesting: set this whether or not this template is ALSO used as another "
                   "template's child_template placeholder -- the two are unrelated concerns "
                   "(one is 'what do I contain', the other is 'what am I, once built').",
    )

    # -- Source-file provenance -----------------------------------------
    # Populated automatically by DesignClient.load_templates_from_yaml()
    # on every load (whether or not the load actually changed anything --
    # a no-op re-load still re-stamps these, so they always describe the
    # most recent load attempt, not just the load that created the row).
    # Blank for templates created any other way (create_template() called
    # directly with no file behind it, e.g. from the MCP server or a
    # script). None of this is enforced -- a template can still be edited
    # or created without going through a file at all -- it's a record of
    # "if this came from a file, here's exactly which one", used by
    # `hdb verify-template` to detect drift between a tracked YAML file
    # and what's actually live. See client/README.md.
    source_path = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Path to the YAML file this template was last loaded from, as given to "
                   "load-template (relative to the repo root when the file is inside a git "
                   "checkout, otherwise as passed).",
    )
    source_sha256 = models.CharField(
        max_length=64, blank=True, default="",
        help_text="SHA-256 of source_path's exact file content at the time of the most "
                   "recent load. Compared against the file's current hash by "
                   "`hdb verify-template` to tell 'file changed since last load' apart from "
                   "'database was edited some other way'.",
    )
    source_git_commit = models.CharField(
        max_length=40, blank=True, default="",
        help_text="`git rev-parse HEAD` of the repository containing source_path, captured "
                   "at load time on a best-effort basis (blank if the file isn't inside a "
                   "git checkout, or git isn't available). Describes the repo state the load "
                   "was run from -- NOT a guarantee source_path itself was committed at that "
                   "commit; a dirty working tree at load time still records HEAD, so treat "
                   "this as 'roughly when/where', not as a cryptographic proof by itself -- "
                   "source_sha256 is the one that's exact.",
    )

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    def descendant_template_ids(self, _seen=None):
        """Set of every DesignTemplate id reachable from this template by
        following child_template edges, at any depth (this template's own
        id is never included). Used by would_create_cycle() to check
        whether a proposed child_template link would let this template
        reach itself again.

        `_seen` guards recursion: bounded by TEMPLATE_NESTING_MAX_DEPTH and
        by never re-descending into an id already visited on this path, so
        this can't loop forever even if a cycle somehow already exists
        (see the module comment above -- should be impossible via .save(),
        this is the read-time backstop).
        """
        seen = _seen if _seen is not None else {self.pk}
        if len(seen) > TEMPLATE_NESTING_MAX_DEPTH:
            return set()
        result = set()
        for element in self.elements.select_related("child_template"):
            child_id = element.child_template_id
            if child_id and child_id not in seen:
                result.add(child_id)
                result |= element.child_template.descendant_template_ids(seen | {child_id})
        return result

    def would_create_cycle(self, candidate_child):
        """True if adding `candidate_child` as a (direct, or via further
        nesting, indirect) sub-template of this template would let this
        template reach itself again -- i.e. candidate_child IS this
        template, or this template already appears somewhere in
        candidate_child's own descendant tree. A physical assembly can
        never contain itself at any depth; callers (DesignTemplateElement
        .save(), the "Add Placeholder" view) must check this BEFORE saving
        a child_template link, not just guard against it at read time."""
        if candidate_child.pk == self.pk:
            return True
        return self.pk in candidate_child.descendant_template_ids()

    def can_nest(self, candidate_child):
        """True if `candidate_child` is a legal sub-template placeholder for
        this template: not a cycle (see would_create_cycle), same project
        (nesting a sub-assembly from an unrelated project has no physical
        meaning -- `project` is a real, load-bearing field elsewhere too,
        e.g. Component's name+project uniqueness), and same owner_group
        (the group that owns a template is responsible for everything
        physically built into it, at every level -- a BTOF assembly can't
        be built out of a BEMC group's sub-assembly). All three are
        independent reasons to reject a candidate; this is the single
        place that combines them, used by both the "Add Placeholder"
        dropdown (UI convenience) and DesignTemplateElement.clean() (the
        actual enforcement -- a dropdown is trivially bypassable)."""
        if self.would_create_cycle(candidate_child):
            return False
        if candidate_child.project != self.project:
            return False
        if candidate_child.owner_group_id != self.owner_group_id:
            return False
        return True

    @property
    def nesting_levels(self):
        """Depth of the nested sub-template structure this template
        describes, via DesignTemplateElement.child_template. A template
        made up entirely of leaf components (no placeholder that nests
        another template) returns 1; each additional level of sub-template
        nesting beneath it adds 1.

        Bounded by TEMPLATE_NESTING_MAX_DEPTH, plus a `seen` set (by
        template id) as the same read-time defense-in-depth described in
        the module comment above -- write-time validation should already
        make a cycle impossible.
        """
        def _depth(template, seen):
            if template.pk in seen or len(seen) >= TEMPLATE_NESTING_MAX_DEPTH:
                return 0
            seen = seen | {template.pk}
            deepest_child = 0
            for element in template.elements.select_related("child_template"):
                if element.child_template_id:
                    deepest_child = max(deepest_child, _depth(element.child_template, seen))
            return 1 + deepest_child

        return _depth(self, set())

    def is_complete(self, _memo=None):
        """True if every placeholder anywhere in this template's subtree
        resolves to either a catalog Component or a nested DesignTemplate
        that is itself complete -- i.e. nothing beneath this template is
        still waiting on a YAML file that hasn't been uploaded yet (see
        DesignTemplateElement.child_template_name and
        resolve_pending_template_references() below).

        Computed on demand rather than stored: cheap (bounded by
        TEMPLATE_NESTING_MAX_DEPTH, same as nesting_levels/
        breadcrumb_ancestors above) and can never go stale the way a
        cached column would need explicit invalidation to avoid. `_memo`
        caches by template id within one call so a template reused in
        several places (a "diamond") is only walked once. Resolved
        child_template edges are already guaranteed acyclic by
        would_create_cycle at write time, so straightforward recursion
        with memoization is safe -- no separate cycle guard needed here.
        """
        memo = _memo if _memo is not None else {}
        if self.pk in memo:
            return memo[self.pk]
        complete = True
        for element in self.elements.select_related('child_template'):
            if element.component_id is not None:
                continue
            if element.child_template_id is None:
                complete = False
                break
            if not element.child_template.is_complete(memo):
                complete = False
                break
        memo[self.pk] = complete
        return complete

    def pending_placeholders(self):
        """This template's OWN placeholders (not recursing into nested
        sub-templates) that are still waiting on a template name that
        hasn't been uploaded yet. Used on the template detail page to
        tell a viewer exactly which name(s) are missing, rather than just
        that something, somewhere, isn't complete -- see is_complete()."""
        return [
            el for el in self.elements.all()
            if el.child_template_id is None and el.child_template_name
        ]

    def subsystem_fingerprint(self):
        """A single hash summarizing this template AND every template
        beneath it (via descendant_template_ids()), derived from each
        one's own source_sha256 -- see client/README.md's "Provenance and
        drift detection". Answers "is this whole subtree, as a unit, the
        same as some other known state" without losing the per-template
        precision source_sha256 already provides -- this is a summary
        computed FROM that vector, not a replacement for it (see the
        `hdb subsystem-hash` CLI command).

        Returns None if this template isn't complete (see is_complete())
        -- a fingerprint of a subtree that's still waiting on an
        un-uploaded sub-template would describe a provisional, half-built
        state, not a real one; there's nothing meaningful to compare it
        against. Like is_complete(), computed fresh on every call rather
        than stored, so it can never go stale the way a cached column
        would -- if a descendant's source_sha256 changes (a re-load with
        new content) or the tree structure itself changes, the very next
        call already reflects that.

        Otherwise returns:
            {
              "fingerprint": <sha256 hex digest>,
              "templates": [{"name": str, "source_sha256": str}, ...],
                  # this template + every descendant, name-sorted -- the
                  # exact, ordered input the fingerprint was computed from
              "missing_provenance": [str, ...],
                  # names, if any, of templates in this subtree that have
                  # never been loaded via load_templates_from_yaml() (an
                  # empty source_sha256 -- e.g. created via seed_hdb or
                  # directly in admin/shell). Their "" hash still
                  # contributes deterministically to the fingerprint, but
                  # a fingerprint that includes one of these isn't backed
                  # by a tracked file for that piece -- it can't tell you
                  # "matches version X of the YAML", only "matches what
                  # was here last time you fingerprinted it".
            }

        Ordering (name-sorted) and format ("name:source_sha256" pairs,
        joined with "|") are fixed and internal -- don't parse the digest
        input back out of this; compare "fingerprint" values directly, or
        diff "templates" lists element-by-element for exactly which piece
        changed.
        """
        if not self.is_complete():
            return None
        ids = {self.pk} | self.descendant_template_ids()
        templates = list(
            DesignTemplate.objects.filter(pk__in=ids).order_by("name")
        )
        digest_input = "|".join(f"{t.name}:{t.source_sha256}" for t in templates)
        return {
            "fingerprint": hashlib.sha256(digest_input.encode()).hexdigest(),
            "templates": [{"name": t.name, "source_sha256": t.source_sha256} for t in templates],
            "missing_provenance": [t.name for t in templates if not t.source_sha256],
        }

    def parent_templates(self):
        """Distinct DesignTemplates that reference this one as a
        child_template placeholder in one (or more) of their elements,
        ordered by name for deterministic display. Zero parents means
        this template is a nesting root; more than one means this
        template is shared -- nested as a sub-assembly inside more than
        one different parent template (the "diamond" shape explicitly
        allowed by would_create_cycle, e.g. a common bracket template
        used by two otherwise-unrelated assemblies)."""
        parent_ids = self.parent_elements.values_list("template_id", flat=True).distinct()
        return list(DesignTemplate.objects.filter(pk__in=parent_ids).order_by("name"))

    def breadcrumb_ancestors(self, _seen=None):
        """This template's ancestor chain, root-first, as a list of
        {'template': DesignTemplate, 'alternatives': [DesignTemplate, ...]}
        dicts -- used to render the template detail page's breadcrumb.

        Each entry's 'template' is one ancestor on the path from a
        nesting root down to (but not including) this template; its
        'alternatives' are every immediate parent of the NEXT template
        down that same path (that next template being either the
        following entry, or this template itself for the last entry) --
        i.e. every other template that could legally occupy that same
        breadcrumb slot. Nesting is a DAG, not strictly a tree (see
        parent_templates), so there is no single canonical path in
        general; where a template has more than one immediate parent,
        one is picked deterministically (alphabetically first) to
        continue the walk, and the full set is carried along as
        'alternatives' so the UI can offer the others as one-click
        detours rather than silently hiding them.

        Bounded by TEMPLATE_NESTING_MAX_DEPTH, plus a `seen` set, as the
        same read-time defense-in-depth used elsewhere in this class --
        write-time validation (would_create_cycle) should already make a
        cycle impossible, so this is a backstop, not the enforcement.
        """
        seen = _seen if _seen is not None else {self.pk}
        if len(seen) > TEMPLATE_NESTING_MAX_DEPTH:
            return []
        parents = self.parent_templates()
        if not parents:
            return []
        chosen = parents[0]
        if chosen.pk in seen:
            return [{"template": chosen, "alternatives": parents}]
        return chosen.breadcrumb_ancestors(seen | {chosen.pk}) + [
            {"template": chosen, "alternatives": parents}
        ]

    class Meta:
        ordering = ["name"]


class DesignTemplateElement(models.Model):
    """One placeholder line in a DesignTemplate: either a catalog Component
    (a leaf part) or another DesignTemplate (a nested sub-assembly), plus a
    quantity -- exactly one of `component`/`child_template` must be set.
    Validated in three overlapping layers so nothing can slip through:
    clean() (so Django admin's form validation shows a friendly inline
    error before ever attempting to save), save() (which always calls
    clean() first, so the direct-ORM paths used by the web views and
    hdb_client are covered too, not just forms), and a database
    CheckConstraint for the both/neither shape as a backstop against
    anything that bypasses save() entirely (bulk_create, raw SQL). The
    self-nesting-cycle check can only be expressed in Python (it needs to
    walk other rows), so clean()/save() are the sole enforcement for that
    one -- there is no DB-level equivalent. Same for the two scope checks
    a child_template must also pass -- same project, same owner_group as
    the parent template (see DesignTemplate.can_nest) -- since those also
    compare across rows/tables, which a CheckConstraint can't do.
    Deliberately no ComponentInstance reference here -- templates describe
    what kind of parts/sub-assemblies an assembly needs, never specific
    serialized items; those are chosen later on the instantiated Design."""
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    template       = models.ForeignKey(DesignTemplate, on_delete=models.CASCADE, related_name="elements")
    element_name   = models.CharField(max_length=128)
    component      = models.ForeignKey(Component, null=True, blank=True, on_delete=models.CASCADE, related_name="template_memberships")
    child_template = models.ForeignKey(
        DesignTemplate, null=True, blank=True, on_delete=models.PROTECT, related_name="parent_elements",
        help_text="Nested sub-template placeholder, e.g. a Stave's element pointing at the Half-Stave template. "
                   "Left NULL while child_template_name hasn't resolved yet -- see that field and "
                   "resolve_pending_template_references(). PROTECTed rather than SET_NULL once "
                   "resolved: deleting it out from under a placeholder that's set to "
                   "exactly-one-of-component/child_template would leave neither set. Delete the placeholder "
                   "(or repoint it) first, then the template.",
    )
    child_template_name = models.CharField(
        max_length=256, blank=True, default="",
        help_text="For a template-type placeholder, the child_template's name as given in the "
                   "uploaded YAML -- always populated for that placeholder shape, whether or not "
                   "child_template has resolved yet. YAML uploads are asynchronous and order-"
                   "independent: a parent template can be uploaded before the sub-template it "
                   "names exists. When it names a template that isn't in the database yet, "
                   "child_template stays NULL and this field records the intent; "
                   "resolve_pending_template_references() re-attempts the link (with the same "
                   "cycle/project/owner_group validation as an eagerly-resolved reference) after "
                   "every YAML upload, so upload order never matters. See "
                   "DesignTemplate.is_complete()/pending_placeholders() for the reader-facing view "
                   "of what's still outstanding. Left blank for a component-type placeholder.",
    )
    quantity     = models.PositiveIntegerField(default=1)
    description  = models.TextField(blank=True)

    def clean(self):
        has_component     = self.component_id is not None
        is_template_slot  = bool(self.child_template_name)
        if has_component == is_template_slot:  # both set, or neither -- exactly one required
            raise ValidationError(
                "A template placeholder must reference exactly one of component or "
                "child_template (name), not both or neither."
            )
        if self.child_template_id is not None and self.child_template.name != self.child_template_name:
            # Belt-and-suspenders: the two should always be set together by
            # create_template()/resolve_pending_template_references() --
            # this catches a bug in either, rather than silently letting a
            # resolved FK and its recorded name drift apart.
            raise ValidationError(
                f"child_template ({self.child_template.name!r}) doesn't match "
                f"child_template_name ({self.child_template_name!r}) -- these must agree."
            )
        # The three cross-row checks below only apply once child_template has
        # actually resolved -- a still-pending reference (child_template_name
        # set, child_template NULL) can't be validated yet because there's no
        # real template row to check against. resolve_pending_template_
        # references() applies these same checks the moment resolution
        # happens, so nothing here is skipped forever, only deferred.
        if self.child_template_id is None:
            return
        if self.template.would_create_cycle(self.child_template):
            raise ValidationError(
                f"{self.child_template.name!r} can't be added as a sub-template of "
                f"{self.template.name!r} -- a template can never contain itself, "
                f"directly or through further nesting."
            )
        if self.child_template.project != self.template.project:
            raise ValidationError(
                f"{self.child_template.name!r} (project {self.child_template.project!r}) can't be "
                f"nested inside {self.template.name!r} (project {self.template.project!r}) -- "
                f"a sub-assembly must belong to the same project as the template it's nested in."
            )
        if self.child_template.owner_group_id != self.template.owner_group_id:
            child_group  = self.child_template.owner_group.name if self.child_template.owner_group_id else "no group"
            parent_group = self.template.owner_group.name if self.template.owner_group_id else "no group"
            raise ValidationError(
                f"{self.child_template.name!r} (owned by {child_group}) can't be nested inside "
                f"{self.template.name!r} (owned by {parent_group}) -- a sub-assembly must be owned "
                f"by the same group as the template it's nested in."
            )

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        # Auto-derive child_template_name from an already-resolved
        # child_template for any caller that sets the FK directly without
        # also setting the name (direct ORM use, admin, scripts, existing
        # tests) -- one less thing to remember, and keeps the two fields
        # from silently drifting apart for that common case. Callers
        # creating a still-*pending* reference (child_template NULL) must
        # set child_template_name themselves -- there's no FK to derive it
        # from.
        if self.child_template_id and not self.child_template_name:
            self.child_template_name = self.child_template.name
        self.clean()
        super().save(*args, **kwargs)

    def is_pending(self):
        """True if this is a template-type placeholder whose child_template
        hasn't resolved yet -- see child_template_name's help text."""
        return self.child_template_id is None and bool(self.child_template_name)

    def element_type(self):
        return "TEMPLATE" if self.child_template_name else "COMPONENT"

    def __str__(self):
        return f"{self.template.name} / {self.element_name}"

    class Meta:
        ordering = ["element_name"]
        unique_together = [("template", "element_name")]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(component__isnull=False, child_template__isnull=True, child_template_name="") |
                    (models.Q(component__isnull=True) & ~models.Q(child_template_name=""))
                ),
                name="designtemplateelement_exactly_one_of_component_or_child_template",
            ),
        ]


def resolve_pending_template_references():
    """Re-attempt to link every DesignTemplateElement that names a
    child_template by string (child_template_name) but hasn't resolved the
    FK yet. YAML uploads are asynchronous and order-independent -- a
    parent template's YAML can be uploaded before the sub-template it
    names exists -- so this is what actually makes that true: called
    after every upload (see hdb_client.designs.DesignClient), it matches
    every pending element against the templates that now exist, purely by
    name. Safe to call repeatedly (a no-op once nothing new resolves) and
    doesn't need to run in any particular order relative to uploads.

    For each pending element, one of three things happens:
      - no template named `child_template_name` exists yet: left
        untouched, still pending.
      - a template with that name exists but fails the same validation an
        eagerly-resolved reference would get (would create a cycle, or a
        project/owner_group mismatch, see DesignTemplate.can_nest): left
        pending, but reported back as a *conflict* rather than an
        ordinary "still waiting" case. This is a meaningfully different
        situation for a human to see -- unlike a name nobody's uploaded
        yet, no future upload will ever make this particular link valid;
        it needs a rename or a scope fix.
      - a template with that name exists and passes validation: the FK is
        linked (via save(), so clean() re-validates the same way an
        eagerly-resolved reference always has) and reported as resolved.

    One pass over the pending set is always sufficient: resolving element
    A only requires a matching template *row* to exist, not for that
    template to itself be complete, and resolving A can't change what any
    other pending element's name matches against. Callers should wrap
    this in transaction.atomic() alongside whatever upload triggered it.

    Returns {"resolved": [...], "conflicts": [...]}, each entry a dict
    identifying the template/element/name involved (and, for a conflict,
    why it was rejected).
    """
    resolved, conflicts = [], []
    pending = DesignTemplateElement.objects.filter(
        child_template__isnull=True,
    ).exclude(child_template_name="").select_related("template")
    for element in pending:
        match = DesignTemplate.objects.filter(name=element.child_template_name).first()
        if match is None:
            continue
        if not element.template.can_nest(match):
            conflicts.append({
                "template": element.template.name,
                "element_name": element.element_name,
                "child_template_name": element.child_template_name,
                "reason": (
                    f"a template named {match.name!r} exists, but can't be nested inside "
                    f"{element.template.name!r} -- it would create a cycle, or its project/"
                    f"owner_group doesn't match. Rename one of them, or fix the scope; "
                    f"re-uploading won't resolve this on its own."
                ),
            })
            continue
        element.child_template = match
        element.save()
        resolved.append({
            "template": element.template.name,
            "element_name": element.element_name,
            "child_template": match.name,
        })
    return {"resolved": resolved, "conflicts": conflicts}


class Design(OwnedModel):
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    name        = models.CharField(max_length=256, unique=True)
    description = models.TextField(blank=True)
    project     = models.CharField(max_length=64, blank=True, default="ePIC")
    template    = models.ForeignKey(
        DesignTemplate, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="designs",
        help_text="Template this design was instantiated from, if any.",
    )
    # on_delete=PROTECT, same reasoning as ComponentInstance.location:
    # deleting a Location a Design is currently assembled at shouldn't
    # silently clear that design's assembly location. null=True/blank=True
    # stay, unlike ComponentInstance -- an unset Design.location is a
    # normal, actively-used state (see design_detail's needs_location
    # banner), not something creation ever required in the first place.
    location    = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.PROTECT,
        related_name="designs",
        help_text=(
            "Where this design is being assembled. A design lives in exactly "
            "one place, so placeholder replacement offers only inventory "
            "instances stored at this location."
        ),
    )

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]


class DesignElement(models.Model):
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    design             = models.ForeignKey(Design,             on_delete=models.CASCADE,  related_name="elements")
    element_name       = models.CharField(max_length=128)
    component          = models.ForeignKey(Component,         null=True, blank=True, on_delete=models.SET_NULL, related_name="design_memberships")
    child_design       = models.ForeignKey(Design,            null=True, blank=True, on_delete=models.SET_NULL, related_name="parent_elements")
    quantity           = models.PositiveIntegerField(default=1)
    description        = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def element_type(self):
        return "DESIGN" if self.child_design_id else "COMPONENT"

    def __str__(self):
        return f"{self.design.name} / {self.element_name}"

    class Meta:
        ordering = ["element_name"]
        unique_together = [("design", "element_name")]


class DesignElementInstance(models.Model):
    """
    One physical inventory item installed into one slot of a design element.
    A DesignElement with quantity N (e.g. "SiPM x 4") accepts up to N of
    these rows, each pointing at a distinct ComponentInstance -- this is what
    lets a multiple-quantity placeholder be filled with N separate serialized
    items instead of a single FK.

    `instance` is unique across the whole table, not just per-element: a
    ComponentInstance is a physical inventory item, and it can only be
    physically present in one design (in one slot of one element) at a
    time -- never in two slots, two elements, or two designs at once. This
    is enforced here at the database level, in addition to the view-level
    checks that keep it out of the placeholder dropdowns of every OTHER
    design once it's installed anywhere. Removing it from its element (row
    deleted) makes it available again everywhere.
    """
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    element  = models.ForeignKey(DesignElement,     on_delete=models.CASCADE, related_name="installed_instances")
    instance = models.ForeignKey(ComponentInstance, on_delete=models.CASCADE, related_name="design_installations", unique=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.element} ← {self.instance}"

    class Meta:
        ordering = ["instance__tag"]

# ---------------------------------------------------------------------------
# User profile extension
# ---------------------------------------------------------------------------

class UserProfile(models.Model):
    """Extends Django's built-in User with HDB-specific attributes."""
    id          = models.CharField(max_length=36, primary_key=True, editable=False)
    user        = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    # Mandatory, same reasoning as Location.institution -- a profile's home
    # institution can't be left blank, and PROTECT blocks deleting an
    # Institution that users still call home instead of silently orphaning
    # them.
    institution = models.ForeignKey(Institution, on_delete=models.PROTECT, related_name='users')

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        inst = str(self.institution) if self.institution else '—'
        return f"{self.user.username} @ {inst}"
