"""
DRF serializers for the Hardware Database (HDB).
"""

from rest_framework import serializers
from django.contrib.auth.models import Group
from .models import (
    Institution, Location, PropertyType, PropertyValue, LogEntry,
    TechnicalSystem, Source,
    Component, ComponentSource,
    ComponentInstance,
    Design, DesignElement,
)


# ── Supporting ──────────────────────────────────────────────────────────────

class GroupSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Group
        fields = ["id", "name"]


class InstitutionSerializer(serializers.ModelSerializer):
    location_count = serializers.SerializerMethodField()

    class Meta:
        model  = Institution
        fields = ["id", "name", "abbreviation", "country", "city",
                  "url", "description", "location_count"]

    def get_location_count(self, obj):
        return obj.locations.count()


class LocationSerializer(serializers.ModelSerializer):
    full_path        = serializers.SerializerMethodField()
    institution_name = serializers.SerializerMethodField()
    children_count   = serializers.SerializerMethodField()

    class Meta:
        model  = Location
        fields = ["id", "name", "location_type", "institution", "description",
                  "parent", "full_path", "institution_name", "children_count"]

    def get_full_path(self, obj):        return obj.full_path()
    def get_institution_name(self, obj): return str(obj.institution) if obj.institution else None
    def get_children_count(self, obj):   return obj.children.count()


class LocationListSerializer(serializers.ModelSerializer):
    full_path        = serializers.SerializerMethodField()
    institution_name = serializers.SerializerMethodField()

    class Meta:
        model  = Location
        fields = ["id", "name", "location_type", "institution",
                  "parent", "full_path", "institution_name"]

    def get_full_path(self, obj):        return obj.full_path()
    def get_institution_name(self, obj): return str(obj.institution) if obj.institution else None


class PropertyTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PropertyType
        fields = ["id", "name", "category", "handler",
                  "description", "default_units", "default_value"]


class PropertyValueSerializer(serializers.ModelSerializer):
    property_type_name    = serializers.CharField(source="property_type.name",    read_only=True)
    property_type_handler = serializers.CharField(source="property_type.handler", read_only=True)

    class Meta:
        model  = PropertyValue
        fields = ["id", "property_type", "property_type_name", "property_type_handler",
                  "tag", "value", "units", "description",
                  "is_dynamic", "user_writable", "created_on", "modified_on"]


class LogEntrySerializer(serializers.ModelSerializer):
    logged_by_username = serializers.CharField(source="logged_by.username", read_only=True)

    class Meta:
        model  = LogEntry
        fields = ["id", "timestamp", "topic", "entry", "attachment",
                  "logged_by", "logged_by_username",
                  "component", "component_instance", "design"]


# ── Domain 1: Component Catalog ─────────────────────────────────────────────

class TechnicalSystemSerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source="group.name", read_only=True)

    class Meta:
        model  = TechnicalSystem
        fields = ["id", "name", "description", "group", "group_name"]


class SourceSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Source
        fields = ["id", "name", "contact_email", "url", "address"]


class ComponentSourceSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source.name", read_only=True)

    class Meta:
        model  = ComponentSource
        fields = ["id", "source", "source_name", "part_number", "cost", "role", "description"]


class ComponentListSerializer(serializers.ModelSerializer):
    technical_system_name = serializers.CharField(source="technical_system.name", read_only=True)
    owner_group_name      = serializers.CharField(source="owner_group.name",      read_only=True)
    instance_count        = serializers.SerializerMethodField()

    class Meta:
        model  = Component
        fields = ["id", "name", "model_number", "project",
                  "technical_system", "technical_system_name",
                  "owner_group", "owner_group_name",
                  "instance_count"]

    def get_instance_count(self, obj):
        return obj.instances.count()


class ComponentSerializer(serializers.ModelSerializer):
    technical_system_name = serializers.CharField(source="technical_system.name", read_only=True)
    owner_group_name      = serializers.CharField(source="owner_group.name",      read_only=True)
    owner_username        = serializers.CharField(source="owner_user.username",   read_only=True)
    sources               = ComponentSourceSerializer(source="componentsource_set", many=True, read_only=True)
    properties            = PropertyValueSerializer(many=True, read_only=True)
    log_entries           = LogEntrySerializer(many=True, read_only=True)
    instance_count        = serializers.SerializerMethodField()

    class Meta:
        model  = Component
        fields = ["id", "name", "alternate_name", "model_number",
                  "description", "project",
                  "technical_system", "technical_system_name",
                  "owner_user", "owner_username",
                  "owner_group", "owner_group_name",
                  "group_writeable", "created_on", "modified_on",
                  "sources", "properties", "log_entries", "instance_count"]

    def get_instance_count(self, obj):
        return obj.instances.count()


# ── Domain 2: Component Inventory ───────────────────────────────────────────

class ComponentInstanceListSerializer(serializers.ModelSerializer):
    component_name   = serializers.CharField(source="component.name", read_only=True)
    location_path    = serializers.SerializerMethodField()
    owner_group_name = serializers.CharField(source="owner_group.name", read_only=True)

    class Meta:
        model  = ComponentInstance
        fields = ["id", "tag", "serial_number",
                  "component", "component_name",
                  "location", "location_path",
                  "owner_group", "owner_group_name"]

    def get_location_path(self, obj):
        return obj.location.full_path() if obj.location else None


class ComponentInstanceSerializer(serializers.ModelSerializer):
    component_name   = serializers.CharField(source="component.name", read_only=True)
    location_path    = serializers.SerializerMethodField()
    institution_name = serializers.SerializerMethodField()
    owner_group_name = serializers.CharField(source="owner_group.name",    read_only=True)
    owner_username   = serializers.CharField(source="owner_user.username", read_only=True)
    properties       = PropertyValueSerializer(many=True, read_only=True)
    log_entries      = LogEntrySerializer(many=True, read_only=True)

    class Meta:
        model  = ComponentInstance
        fields = ["id", "tag", "serial_number", "description",
                  "component", "component_name",
                  "location", "location_path", "institution_name",
                  "owner_user", "owner_username",
                  "owner_group", "owner_group_name",
                  "group_writeable", "created_on", "modified_on",
                  "properties", "log_entries"]

    def get_location_path(self, obj):
        return obj.location.full_path() if obj.location else None

    def get_institution_name(self, obj):
        if obj.location and obj.location.institution:
            return obj.location.institution.name
        return None


# ── Domain 3: Designs ────────────────────────────────────────────────────────

class DesignElementSerializer(serializers.ModelSerializer):
    element_type            = serializers.SerializerMethodField()
    component_name          = serializers.CharField(source="component.name",              read_only=True)
    child_design_name       = serializers.CharField(source="child_design.name",           read_only=True)
    # A quantity>1 element can hold several installed instances (one per
    # slot, via DesignElementInstance) -- there's no single "the" installed
    # instance any more, so this reports every instance id currently
    # occupying one of this element's slots. (There used to be a single
    # DesignElement.installed_instance FK; it was removed by migration 0004
    # when multi-instance slots were introduced, but this serializer wasn't
    # updated to match at the time -- it referenced a field that no longer
    # existed and broke GET /api/designs/.)
    installed_instance_ids = serializers.SerializerMethodField()
    properties              = PropertyValueSerializer(many=True, read_only=True)

    class Meta:
        model  = DesignElement
        fields = ["id", "element_name", "element_type", "quantity", "description",
                  "component", "component_name",
                  "child_design", "child_design_name",
                  "installed_instance_ids",
                  "properties"]

    def get_element_type(self, obj):
        return obj.element_type()

    def get_installed_instance_ids(self, obj):
        return list(obj.installed_instances.values_list("instance_id", flat=True))


class DesignListSerializer(serializers.ModelSerializer):
    owner_group_name = serializers.CharField(source="owner_group.name", read_only=True)
    element_count    = serializers.SerializerMethodField()

    class Meta:
        model  = Design
        fields = ["id", "name", "description", "project",
                  "owner_group", "owner_group_name", "element_count"]

    def get_element_count(self, obj):
        return obj.elements.count()


class DesignSerializer(serializers.ModelSerializer):
    owner_group_name = serializers.CharField(source="owner_group.name",    read_only=True)
    owner_username   = serializers.CharField(source="owner_user.username", read_only=True)
    elements         = DesignElementSerializer(many=True, read_only=True)
    properties       = PropertyValueSerializer(many=True, read_only=True)
    log_entries      = LogEntrySerializer(many=True, read_only=True)

    class Meta:
        model  = Design
        fields = ["id", "name", "description", "project",
                  "owner_user", "owner_username",
                  "owner_group", "owner_group_name",
                  "group_writeable", "created_on", "modified_on",
                  "elements", "properties", "log_entries"]
