"""
Hardware Database (HDB) models.
Three primary domains: Component Catalog, Component Inventory, Design.
Supporting: Institution, Location, Ownership, Properties, Logs.
Groups use Django's built-in auth.Group.
"""

import uuid
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
    location         = models.ForeignKey(Location,        null=True, blank=True, on_delete=models.SET_NULL, related_name="instances")
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

class DesignTemplate(OwnedModel):
    """
    Reusable blueprint for a Design. Template elements reference catalog
    Components as *placeholders* -- they say "this assembly needs 4 SiPMs",
    not "these four specific SiPMs". When a user instantiates a template,
    a real Design is created with one DesignElement per placeholder; the
    editing tools on the design detail page then let the owning group
    replace each placeholder with an actual ComponentInstance from the
    inventory as the physical assembly is built.
    """
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    name        = models.CharField(max_length=256, unique=True)
    description = models.TextField(blank=True)
    project     = models.CharField(max_length=64, blank=True, default="ePIC")

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]


class DesignTemplateElement(models.Model):
    """One placeholder line in a DesignTemplate: a catalog Component and a
    quantity. Deliberately no ComponentInstance reference here -- templates
    describe what kind of parts an assembly needs, never specific serialized
    items; those are chosen later on the instantiated Design."""
    id = models.CharField(max_length=36, primary_key=True, editable=False)
    template     = models.ForeignKey(DesignTemplate, on_delete=models.CASCADE, related_name="elements")
    element_name = models.CharField(max_length=128)
    component    = models.ForeignKey(Component, on_delete=models.CASCADE, related_name="template_memberships")
    quantity     = models.PositiveIntegerField(default=1)
    description  = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = str(uuid.uuid4())
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.template.name} / {self.element_name}"

    class Meta:
        ordering = ["element_name"]
        unique_together = [("template", "element_name")]


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
    location    = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL,
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
