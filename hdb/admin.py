from django.contrib import admin
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

@admin.register(Component)
class ComponentAdmin(admin.ModelAdmin):
    list_display    = ("pk", "name", "model_number", "technical_system", "project", "owner_group", "instance_count")
    list_filter     = ("technical_system", "project", "owner_group")
    search_fields   = ("name", "alternate_name", "model_number", "description")
    readonly_fields = ("pk", "created_on", "modified_on")
    inlines         = [ComponentSourceInline, PropertyValueComponentInline, ComponentInstanceInline, LogComponentInline]
    fieldsets = (
        ("Identity",   {"fields": ("pk", "name", "alternate_name", "model_number", "description", "project", "technical_system")}),
        ("Ownership",  {"fields": ("owner_user", "owner_group", "group_writeable", "created_by", "created_on", "modified_by", "modified_on"), "classes": ("collapse",)}),
    )

    @admin.display(description="# Instances")
    def instance_count(self, obj):
        return obj.instances.count()

# ── Domain 2: Inventory ───────────────────────────────────────────────────────

@admin.register(ComponentInstance)
class ComponentInstanceAdmin(admin.ModelAdmin):
    list_display    = ("pk", "tag", "component", "technical_system", "serial_number", "location", "institution_name", "owner_group", "owner_user")
    list_filter     = ("technical_system", "location__institution", "owner_group")
    search_fields   = ("tag", "serial_number", "component__name")
    readonly_fields = ("pk", "created_on", "modified_on")
    inlines         = [PropertyValueInstanceInline, LogInstanceInline]
    fieldsets = (
        ("Identification", {"fields": ("pk", "tag", "serial_number", "component")}),
        ("Location",       {"fields": ("location", "description")}),
        ("Ownership",      {"fields": ("owner_user", "owner_group", "group_writeable", "created_by", "created_on", "modified_by", "modified_on"), "classes": ("collapse",)}),
    )

    @admin.display(description="Institution")
    def institution_name(self, obj):
        return obj.location.institution if obj.location else "—"


# ── Domain 3: Designs ─────────────────────────────────────────────────────────

class DesignTemplateElementInline(admin.TabularInline):
    model = DesignTemplateElement; extra = 0
    fields = ("element_name", "component", "quantity", "description")


@admin.register(DesignTemplate)
class DesignTemplateAdmin(admin.ModelAdmin):
    list_display    = ("pk", "name", "project", "placeholder_count", "owner_group")
    list_filter     = ("project", "owner_group")
    search_fields   = ("name", "description")
    readonly_fields = ("pk", "created_on", "modified_on")
    inlines         = [DesignTemplateElementInline]
    fieldsets = (
        ("Identity",  {"fields": ("pk", "name", "description", "project")}),
        ("Ownership", {"fields": ("owner_user", "owner_group", "group_writeable", "created_by", "created_on", "modified_by", "modified_on"), "classes": ("collapse",)}),
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
