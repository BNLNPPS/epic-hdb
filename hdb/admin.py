from django.contrib import admin
from django.db import transaction
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User
from .models import (
    Institution, Location, PropertyType, PropertyValue, LogEntry,
    TechnicalSystem, Source,
    Component, ComponentSource,
    ComponentInstance,
    Design, DesignElement, DesignElementInstance,
    DesignTemplate, DesignTemplateElement,
    UserProfile,
)

# ── Inlines ──────────────────────────────────────────────────────────────────

class PropertyValueComponentInline(admin.TabularInline):
    model = PropertyValue; fk_name = "component"; extra = 0
    fields = ("property_type", "tag", "value", "units", "is_dynamic")

class PropertyValueInstanceInline(admin.TabularInline):
    model = PropertyValue; fk_name = "component_instance"; extra = 0
    fields = ("property_type", "tag", "value", "units", "is_dynamic")

class PropertyValueDesignInline(admin.TabularInline):
    model = PropertyValue; fk_name = "design"; extra = 0
    fields = ("property_type", "tag", "value", "units", "is_dynamic")

class PropertyValueElementInline(admin.TabularInline):
    model = PropertyValue; fk_name = "design_element"; extra = 0
    fields = ("property_type", "tag", "value", "units", "is_dynamic")

class LogComponentInline(admin.TabularInline):
    model = LogEntry; fk_name = "component"; extra = 0
    fields = ("timestamp", "logged_by", "topic", "entry"); readonly_fields = ("timestamp",)

class LogInstanceInline(admin.TabularInline):
    model = LogEntry; fk_name = "component_instance"; extra = 0
    fields = ("timestamp", "logged_by", "topic", "entry"); readonly_fields = ("timestamp",)

class LogDesignInline(admin.TabularInline):
    model = LogEntry; fk_name = "design"; extra = 0
    fields = ("timestamp", "logged_by", "topic", "entry"); readonly_fields = ("timestamp",)

class ComponentSourceInline(admin.TabularInline):
    model = ComponentSource; extra = 0
    fields = ("source", "part_number", "cost", "role")

class ComponentInstanceInline(admin.TabularInline):
    model = ComponentInstance; extra = 0
    fields = ("pk", "tag", "serial_number", "location", "owner_group")
    readonly_fields = ("pk",)
    show_change_link = True

class LocationInline(admin.TabularInline):
    model = Location; extra = 0
    fields = ("name", "location_type", "parent", "description")
    show_change_link = True

class DesignElementInline(admin.TabularInline):
    model = DesignElement; fk_name = "design"; extra = 0
    fields = ("element_name", "component", "child_design", "quantity")
    show_change_link = True

class DesignElementInstanceInline(admin.TabularInline):
    model = DesignElementInstance; extra = 0
    fields = ("instance",)

# ── Supporting ────────────────────────────────────────────────────────────────

@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display  = ("pk", "name", "abbreviation", "city", "country", "url")
    search_fields = ("name", "abbreviation", "city", "country")
    inlines       = [LocationInline]

@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display  = ("pk", "name", "location_type", "institution", "parent")
    list_filter   = ("location_type", "institution")
    search_fields = ("name",)

@admin.register(PropertyType)
class PropertyTypeAdmin(admin.ModelAdmin):
    list_display = ("pk", "name", "category", "handler", "default_units")
    list_filter  = ("category", "handler")
    search_fields = ("name",)

@admin.register(TechnicalSystem)
class TechnicalSystemAdmin(admin.ModelAdmin):
    list_display = ("pk", "name", "group", "description")
    list_filter  = ("group",)

@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display  = ("pk", "name", "contact_email", "url")
    search_fields = ("name",)

# ── Domain 1: Catalog ─────────────────────────────────────────────────────────

class GroupChangeLogMixin:
    """Write an activity-log entry when the admin changes owner_group (from
    the change form or the changelist's editable column). `log_field` names
    the LogEntry FK ("component" / "component_instance") and `log_topic` the
    topic."""
    log_field = None
    log_topic = ""

    def save_model(self, request, obj, form, change):
        old_group = None
        if change and "owner_group" in form.changed_data:
            old_group = type(obj).objects.filter(pk=obj.pk).values_list("owner_group__name", flat=True).first()
        super().save_model(request, obj, form, change)
        if change and "owner_group" in form.changed_data:
            from .models import LogEntry
            label = getattr(obj, "name", None) or getattr(obj, "tag", None) or obj.pk
            who = request.user.get_full_name() or request.user.username
            LogEntry.objects.create(
                topic=self.log_topic,
                logged_by=request.user,
                entry=(f"Group of {label} changed from {old_group or '(none)'} to "
                       f"{obj.owner_group.name if obj.owner_group else '(none)'} by {who} (admin)."),
                **{self.log_field: obj},
            )


@admin.register(Component)
class ComponentAdmin(GroupChangeLogMixin, admin.ModelAdmin):
    log_field = "component"
    log_topic = "components"
    list_display    = ("pk", "name", "model_number", "technical_system", "project", "owner_group", "instance_count")
    list_filter     = ("technical_system", "project", "owner_group")
    list_editable   = ("owner_group",)
    search_fields   = ("name", "alternate_name", "model_number", "description")
    readonly_fields = ("pk", "created_on", "modified_on")
    inlines         = [ComponentSourceInline, PropertyValueComponentInline, ComponentInstanceInline, LogComponentInline]
    fieldsets = (
        ("Identity",   {"fields": ("pk", "name", "alternate_name", "model_number", "description", "project", "technical_system", "owner_group")}),
        ("Ownership",  {"fields": ("owner_user", "group_writeable", "created_by", "created_on", "modified_by", "modified_on"), "classes": ("collapse",)}),
    )

    @admin.display(description="# Instances")
    def instance_count(self, obj):
        return obj.instances.count()

# ── Domain 2: Inventory ───────────────────────────────────────────────────────

@admin.register(ComponentInstance)
class ComponentInstanceAdmin(GroupChangeLogMixin, admin.ModelAdmin):
    log_field = "component_instance"
    log_topic = "inventory"
    list_display    = ("pk", "tag", "component", "technical_system", "serial_number", "location", "institution_name", "owner_group", "owner_user")
    list_filter     = ("technical_system", "location__institution", "owner_group")
    list_editable   = ("owner_group",)
    search_fields   = ("tag", "serial_number", "component__name")
    readonly_fields = ("pk", "created_on", "modified_on")
    inlines         = [PropertyValueInstanceInline, LogInstanceInline]
    fieldsets = (
        ("Identification", {"fields": ("pk", "tag", "serial_number", "component", "owner_group")}),
        ("Location",       {"fields": ("location", "description")}),
        ("Ownership",      {"fields": ("owner_user", "group_writeable", "created_by", "created_on", "modified_by", "modified_on"), "classes": ("collapse",)}),
    )

    @admin.display(description="Institution")
    def institution_name(self, obj):
        return obj.location.institution if obj.location else "—"


# ── Domain 3: Designs ─────────────────────────────────────────────────────────

class DesignTemplateElementInline(admin.TabularInline):
    model = DesignTemplateElement; extra = 0
    # fk_name required: DesignTemplateElement now has two FKs to
    # DesignTemplate (template, child_template) -- this inline is anchored
    # to the *owning* template, not the (optional) nested one.
    fk_name = "template"
    # child_template_name alongside child_template: a template-type
    # placeholder can be pending (child_template blank, child_template_name
    # set to a not-yet-uploaded name) -- see DesignTemplateElement's
    # docstring/help_text. Leaving child_template_name out here would make
    # that state impossible to create by hand through admin, even though
    # it's a normal, valid row shape now.
    fields = ("element_name", "component", "child_template", "child_template_name", "quantity", "description")


@admin.register(DesignTemplate)
class DesignTemplateAdmin(admin.ModelAdmin):
    list_display    = ("pk", "name", "project", "placeholder_count", "owner_group")
    list_filter     = ("project", "owner_group")
    search_fields   = ("name", "description")
    # nesting_levels is a recursive property (walks the child_template FK
    # chain), not a DB column -- fine to compute once for a single object's
    # change form, deliberately kept out of list_display where it would run
    # once per row on every paginated admin list page.
    #
    # source_path/source_sha256/source_git_commit are read-only here on
    # purpose: they're an audit trail stamped automatically by
    # DesignClient.load_templates_from_yaml() on every load (see
    # hdb/models.py and client/README.md's "Provenance and drift
    # detection") -- hand-editing them in admin would just misrepresent
    # where a template actually came from. Use `hdb verify-template` to
    # check a tracked YAML file against what's live, not this form.
    readonly_fields = (
        "pk", "created_on", "modified_on", "nesting_levels",
        "source_path", "source_sha256", "source_git_commit",
    )
    inlines         = [DesignTemplateElementInline]
    fieldsets = (
        ("Identity",  {"fields": ("pk", "name", "description", "project", "nesting_levels", "product_component")}),
        ("Ownership", {"fields": ("owner_user", "owner_group", "group_writeable", "created_by", "created_on", "modified_by", "modified_on"), "classes": ("collapse",)}),
        ("Source Provenance", {"fields": ("source_path", "source_sha256", "source_git_commit"), "classes": ("collapse",)}),
    )

    @admin.display(description="# Placeholders")
    def placeholder_count(self, obj):
        return obj.elements.count()


@admin.register(Design)
class DesignAdmin(admin.ModelAdmin):
    list_display    = ("pk", "name", "project", "element_count", "owner_group")
    list_filter     = ("project", "owner_group")
    search_fields   = ("name", "description")
    readonly_fields = ("pk", "created_on", "modified_on")
    inlines         = [DesignElementInline, PropertyValueDesignInline, LogDesignInline]
    fieldsets = (
        ("Identity",  {"fields": ("pk", "name", "description", "project")}),
        ("Ownership", {"fields": ("owner_user", "owner_group", "group_writeable", "created_by", "created_on", "modified_by", "modified_on"), "classes": ("collapse",)}),
    )

    @admin.display(description="# Elements")
    def element_count(self, obj):
        return obj.elements.count()

@admin.register(DesignElement)
class DesignElementAdmin(admin.ModelAdmin):
    list_display  = ("pk", "element_name", "design", "element_type_display", "component", "child_design", "quantity")
    list_filter   = ("design",)
    search_fields = ("element_name", "design__name", "component__name")
    inlines       = [PropertyValueElementInline, DesignElementInstanceInline]

    @admin.display(description="Type")
    def element_type_display(self, obj):
        return obj.element_type()

# ── Cross-domain browsing ─────────────────────────────────────────────────────

@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    list_display    = ("pk", "timestamp", "logged_by", "topic", "short_entry", "component", "component_instance", "design")
    list_filter     = ("topic",)
    search_fields   = ("entry",)
    readonly_fields = ("timestamp",)

    @admin.display(description="Entry")
    def short_entry(self, obj):
        return obj.entry[:80]

@admin.register(PropertyValue)
class PropertyValueAdmin(admin.ModelAdmin):
    list_display = ("pk", "property_type", "tag", "value", "units", "component", "component_instance", "design")
    list_filter  = ("property_type__category", "is_dynamic")
    search_fields = ("tag", "value", "property_type__name")

# ── User admin ────────────────────────────────────────────────────────────────

class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'Profile'
    fields = ('institution',)
    extra = 1

class CustomUserAdmin(UserAdmin):
    list_display = ('pk', 'username', 'email', 'first_name', 'last_name',
                    'is_staff', 'institution_name', 'group_names')
    inlines = [UserProfileInline]

    # Specify which columns act as clickable links to the change view
    list_display_links = ('pk', 'username')

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            who = request.user.get_full_name() or request.user.username
            LogEntry.objects.create(
                topic="user",
                logged_by=request.user,
                entry=f"User {obj.username} created by {who} (admin).",
            )

    def get_deleted_objects(self, objs, request):
        """Block deletion of users who still own components, instances,
        design templates or designs: those show up in `protected`, which makes
        Django's confirmation page (single delete and the bulk action) explain
        why and offer no confirm button."""
        to_delete, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        from .models import user_owned_objects
        for user in objs:
            for label, qs in user_owned_objects(user):
                names = [getattr(o, "name", None) or getattr(o, "tag", None) or str(o.pk) for o in qs[:5]]
                more = qs.count() - len(names)
                protected.append(
                    f"User {user.username} owns {qs.count()} {label.lower()}: "
                    + ", ".join(names) + (f" and {more} more" if more > 0 else "")
                    + ". Reassign them to another user first."
                )
        return to_delete, model_count, perms_needed, protected

    def _log_deletion(self, request, user):
        who = request.user.get_full_name() or request.user.username
        LogEntry.objects.create(
            topic="user",
            logged_by=request.user,
            entry=f"User {user.username} deleted by {who} (admin).",
        )

    def delete_model(self, request, obj):
        with transaction.atomic():
            self._log_deletion(request, obj)
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # "Delete selected users" action: one entry per user.
        with transaction.atomic():
            for user in queryset:
                self._log_deletion(request, user)
            super().delete_queryset(request, queryset)


    @admin.display(description='Institution')
    def institution_name(self, obj):
        try:
            return obj.profile.institution or '—'
        except UserProfile.DoesNotExist:
            return '—'

    @admin.display(description='Groups')
    def group_names(self, obj):
        return ', '.join(obj.groups.values_list('name', flat=True)) or '—'

admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)
