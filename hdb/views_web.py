"""
HDB web views — server-rendered Django pages.
URL config: hdb/urls_web.py
"""
import io
from itertools import groupby

from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.http import HttpResponseForbidden, HttpResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User, Group
from django.db.models import Q, Count
from django.core.paginator import Paginator
import qrcode

from .models import (
    Component, ComponentInstance, Design, DesignElement,
    DesignTemplate, DesignTemplateElement, DesignElementInstance,
    Institution, Location, LogEntry, TechnicalSystem, PropertyType, PropertyValue,
)


PAGE_SIZE = 20

# Selectable page sizes offered via a "per page" dropdown next to the
# pagination controls, on any list page that opts into it (Inventory,
# Activity Log, ...). Anything else in the ?per_page= query param (missing,
# non-numeric, or not one of these) falls back to DEFAULT_PAGE_SIZE.
PAGE_SIZE_CHOICES = [10, 25, 50, 100]
DEFAULT_PAGE_SIZE = 25


# ── helpers ──────────────────────────────────────────────────────────────────

def _qs(request, *exclude):
    """Return current GET params as a query string, minus excluded keys."""
    params = request.GET.copy()
    for key in ('page',) + exclude:
        params.pop(key, None)
    return params.urlencode()


def _resolve_page_size(request):
    """Resolve a list page's page size from ?per_page=, constrained to
    PAGE_SIZE_CHOICES."""
    try:
        size = int(request.GET.get('per_page', DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return size if size in PAGE_SIZE_CHOICES else DEFAULT_PAGE_SIZE


# ── Dashboard ─────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    context = {
        'component_count':   Component.objects.count(),
        'instance_count':    ComponentInstance.objects.count(),
        'design_count':      Design.objects.count(),
        'log_count':         LogEntry.objects.count(),
        'institution_count': Institution.objects.count(),
        'recent_logs':       LogEntry.objects.select_related('logged_by').order_by('-timestamp')[:8],
        'institutions':      Institution.objects.all(),
        'active_page':       'dashboard',
    }
    return render(request, 'cdb/dashboard.html', context)


# ── Component Catalog ─────────────────────────────────────────────────────────

@login_required
def component_list(request):
    """List/search the component catalog. Also handles the "New Component"
    pop-up form: a POST here (name, alternate_name, model_number,
    technical_system -- the same fields shown in the table) creates a
    Component and redirects to its detail page. On validation failure the
    list re-renders with the modal reopened and the entered values kept."""
    form_error = None
    form_data  = {}

    if request.method == 'POST':
        name                 = request.POST.get('name', '').strip()
        alternate_name       = request.POST.get('alternate_name', '').strip()
        model_number         = request.POST.get('model_number', '').strip()
        technical_system_id  = request.POST.get('technical_system') or None
        form_data = {
            'name':             name,
            'alternate_name':   alternate_name,
            'model_number':     model_number,
            'technical_system': technical_system_id or '',
        }

        if not name:
            form_error = 'Name is required.'
        elif Component.objects.filter(name=name, project='ePIC').exists():
            form_error = f'A component named "{name}" already exists.'
        else:
            comp = Component.objects.create(
                name=name,
                alternate_name=alternate_name,
                model_number=model_number,
                technical_system_id=technical_system_id,
                owner_user=request.user,
                created_by=request.user,
            )
            return redirect('component-detail', pk=comp.pk)

    q         = request.GET.get('q', '')
    system    = request.GET.get('system', '')
    group     = request.GET.get('group', '')
    sort      = request.GET.get('sort', '')
    direction = request.GET.get('dir', 'asc')

    qs = Component.objects.select_related(
        'technical_system', 'owner_group', 'owner_user',
    ).annotate(instance_count=Count('instances')).order_by('name')

    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(alternate_name__icontains=q) |
            Q(model_number__icontains=q) | Q(description__icontains=q)
        )
    if system:
        qs = qs.filter(technical_system__name=system)
    if group:
        qs = qs.filter(owner_group__name=group)

    _sort_map = {
        'name':   'name',
        'model':  'model_number',
        'system': 'technical_system__name',
        'count':  'instance_count',
        'group':  'owner_group__name',
        'owner':  'owner_user__username',
    }
    if sort in _sort_map:
        order_field = _sort_map[sort]
        if direction == 'desc':
            order_field = '-' + order_field
        qs = qs.order_by(order_field, 'name')

    paginator = Paginator(qs, PAGE_SIZE)
    page_obj  = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj':    page_obj,
        'q':           q,
        'system':      system,
        'group':       group,
        'sort':        sort,
        'dir':         direction,
        'sort_qs':     _qs(request, 'sort', 'dir'),
        'systems':     TechnicalSystem.objects.order_by('name'),
        'groups':      Group.objects.order_by('name'),
        'query_str':   _qs(request),
        'active_page': 'components',
        'form_error':  form_error,
        'form_data':   form_data,
        'open_modal':  bool(form_error),
    }
    return render(request, 'cdb/components.html', context)


@login_required
def component_detail(request, pk):
    """Component detail page. Also handles the "Add Property" pop-up form:
    a POST here (property_type, tag, value, units) creates a component-level
    PropertyValue, which is then inherited by every ComponentInstance of this
    component that doesn't already override that (property_type, tag) pair.
    Members of the component's owner_group (or a superuser) may add/edit
    properties -- 403 on a POST from anyone else, same authorization pattern
    as the design-level equivalent."""
    comp = get_object_or_404(
        Component.objects.prefetch_related(
            'componentsource_set__source',
            'properties__property_type',
            'log_entries__logged_by',
            'instances__location__institution',
        ).select_related('technical_system', 'owner_group', 'owner_user'),
        pk=pk,
    )

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_edit_properties = (
        (bool(comp.owner_group_id) and comp.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    form_error = None
    form_data  = {}

    if request.method == 'POST':
        if not can_edit_properties:
            return HttpResponseForbidden("You don't have permission to add properties to this component.")
        property_type_id = request.POST.get('property_type') or None
        tag               = request.POST.get('tag', '').strip()
        value             = request.POST.get('value', '').strip()
        units             = request.POST.get('units', '').strip()
        uploaded_file     = request.FILES.get('file')
        form_data = {'property_type': property_type_id or '', 'tag': tag, 'value': value, 'units': units}

        if not property_type_id:
            form_error = 'Property Type is required.'
        else:
            # (component, property_type, tag) identifies "the same property".
            # Re-submitting the same combination (e.g. re-uploading a
            # replacement datasheet) should update that one row in place,
            # not create a second row that duplicates it in the panel.
            pv, created = PropertyValue.objects.get_or_create(
                component=comp, property_type_id=property_type_id, tag=tag,
                defaults={'value': value, 'units': units, 'file': uploaded_file},
            )
            if not created:
                pv.value = value
                pv.units = units
                if uploaded_file:
                    pv.file = uploaded_file
                pv.save()
            return redirect('component-detail', pk=comp.pk)

    # Distinct sites (institutions) among this component's instances, for the
    # site filter dropdown on the Inventory Instances panel.
    sites = sorted(
        {inst.location.institution for inst in comp.instances.all()
         if inst.location and inst.location.institution},
        key=str,
    )
    can_add_instance = bool(comp.owner_group_id) and comp.owner_group_id in user_group_ids

    # Same group-membership check gates the "Current Owner" transfer
    # control -- only members of the component's owner_group may reassign
    # ownership, and the dropdown only ever lists that group's members.
    can_transfer_owner = can_add_instance or request.user.is_superuser
    group_members = (
        comp.owner_group.user_set.order_by('username') if comp.owner_group_id else User.objects.none()
    )

    # Group the Properties panel by units of measurement (e.g. every "g"
    # property together, every "mm" property together), so related physical
    # properties read as a set instead of being scattered in whatever order
    # they were added. Properties with no units (documents, images, links,
    # unitless text) form their own trailing group. Sort key puts
    # units-bearing groups first (alphabetically by unit), the no-units
    # group last, and orders items within a group by property type name for
    # a stable, predictable layout.
    sorted_props = sorted(
        comp.properties.all(),
        key=lambda pv: (pv.units == '', pv.units, str(pv.property_type)),
    )
    prop_groups = [
        {'units': units, 'items': list(items)}
        for units, items in groupby(sorted_props, key=lambda pv: pv.units)
    ]

    context = {
        'component':        comp,
        'active_page':      'components',
        'sites':            sites,
        'property_types':   PropertyType.objects.order_by('name'),
        'prop_groups':      prop_groups,
        'can_add_instance': can_add_instance,
        'can_edit_properties': can_edit_properties,
        'can_transfer_owner': can_transfer_owner,
        'group_members':    group_members,
        'locations':        Location.objects.select_related('institution').order_by('name'),
        'form_error':      form_error,
        'form_data':       form_data,
        'open_modal':      bool(form_error),
    }
    return render(request, 'cdb/component_detail.html', context)


@login_required
def component_property_delete(request, pk, property_id):
    """Remove a property from a component's Properties panel.
    property_id is scoped to component=pk so a property can only be deleted
    through the component it actually belongs to. If the property has an
    attached file, it's removed from storage too -- Django does not delete
    the underlying file automatically when a FileField-holding row is
    deleted, so leaving this out would silently orphan files on disk.
    Members of the component's owner_group (or a superuser) only; 403
    otherwise -- same authorization check as component_detail's Add
    Property form."""
    comp = get_object_or_404(Component, pk=pk)
    pv = get_object_or_404(PropertyValue, pk=property_id, component=comp)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_edit_properties = (
        (bool(comp.owner_group_id) and comp.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )
    if request.method == 'POST':
        if not can_edit_properties:
            return HttpResponseForbidden("You don't have permission to delete properties of this component.")
        if pv.file:
            pv.file.delete(save=False)
        pv.delete()
    return redirect('component-detail', pk=comp.pk)


@login_required
def component_property_update(request, pk, property_id):
    """Inline-edit a component property's value/units from the Properties
    panel. property_id is scoped to component=pk, same protection as
    component_property_delete. Document/Image property types (and any
    property that happens to have a file attached) are excluded -- their
    content is managed via file upload in the Add Property modal, not a
    plain text field, so an edit attempt on one of those is silently
    ignored rather than honoured. Members of the component's owner_group
    (or a superuser) only; 403 otherwise, same authorization check as
    component_property_delete."""
    comp = get_object_or_404(Component, pk=pk)
    pv = get_object_or_404(PropertyValue, pk=property_id, component=comp)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_edit_properties = (
        (bool(comp.owner_group_id) and comp.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )
    if request.method == 'POST':
        if not can_edit_properties:
            return HttpResponseForbidden("You don't have permission to edit properties of this component.")
        if pv.property_type.handler not in ('document', 'image') and not pv.file:
            pv.value = request.POST.get('value', '').strip()
            pv.units = request.POST.get('units', '').strip()
            pv.save()
    return redirect('component-detail', pk=comp.pk)


@login_required
def component_instance_create(request, pk):
    """Create a new ComponentInstance for this component from the "+ Add
    Instance" button on the component detail page, and send the user
    straight to the new instance's page. Only members of the component's
    owner_group may do this. The button is hidden from everyone else, but
    this is the authoritative, server-side check -- a POST here from
    anyone else (or against a component with no owner_group at all to
    check membership against) is rejected with 403 rather than silently
    creating an instance owned by a group the requester doesn't belong
    to."""
    comp = get_object_or_404(Component, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_add = bool(comp.owner_group_id) and comp.owner_group_id in user_group_ids

    if request.method == 'POST':
        if not can_add:
            return HttpResponseForbidden("You don't have permission to add instances of this component.")
        tag           = request.POST.get('tag', '').strip()
        serial_number = request.POST.get('serial_number', '').strip()
        location_id   = request.POST.get('location') or None
        instance = ComponentInstance.objects.create(
            tag=tag,
            serial_number=serial_number,
            component=comp,
            location_id=location_id,
            owner_group=comp.owner_group,
            owner_user=request.user,
            created_by=request.user,
        )
        LogEntry.objects.create(
            component_instance=instance,
            topic='inventory',
            logged_by=request.user,
            entry=(
                f"Instance {instance.tag or instance.pk} of {comp.name} created by "
                f"{request.user.get_full_name() or request.user.username}. "
                f"Location: {instance.location or 'unassigned'}."
            ),
        )
        return redirect('inventory-detail', pk=instance.pk)

    return redirect('component-detail', pk=comp.pk)


@login_required
def component_transfer_owner(request, pk):
    """Transfer a component's ownership to another member of its own
    owner_group, from the "Current Owner" control on the component detail
    page. Only members of the component's owner_group may initiate a
    transfer -- same authorization pattern as component_instance_create,
    enforced with 403 on an unauthorized POST, not just hidden client-side.

    The new owner must themselves belong to the component's owner_group --
    the dropdown only ever offers group members, but a POST naming someone
    outside the group is a business-rule violation from an otherwise
    authorized user, not an authorization breach, so it's silently ignored
    (component redirects unchanged) rather than rejected with 403."""
    comp = get_object_or_404(Component, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_transfer = (
        (bool(comp.owner_group_id) and comp.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_transfer:
            return HttpResponseForbidden("You don't have permission to change this component's owner.")
        new_owner_id = request.POST.get('owner_user') or None
        if new_owner_id and User.objects.filter(pk=new_owner_id, groups=comp.owner_group_id).exists():
            if comp.owner_user_id != int(new_owner_id):
                old_owner = comp.owner_user
                new_owner = User.objects.get(pk=new_owner_id)
                comp.owner_user = new_owner
                comp.save()
                LogEntry.objects.create(
                    component=comp,
                    topic='other',
                    logged_by=request.user,
                    entry=(
                        f"Ownership of {comp.name} transferred from "
                        f"{old_owner.get_full_name() or old_owner.username if old_owner else 'unassigned'} to "
                        f"{new_owner.get_full_name() or new_owner.username} by "
                        f"{request.user.get_full_name() or request.user.username}."
                    ),
                )

    return redirect('component-detail', pk=comp.pk)


# ── Component Inventory ───────────────────────────────────────────────────────

@login_required
def inventory_list(request):
    """List/search the inventory. Also handles the "Add Inventory Item"
    pop-up form: a POST here (component, tag, serial number, location,
    group) creates a ComponentInstance and redirects to its detail page.
    The owner is always the logged-in user, and the group dropdown is
    restricted to groups that user actually belongs to. On validation
    failure the list re-renders with the modal reopened and the entered
    values kept."""
    form_error = None
    form_data  = {}

    if request.method == 'POST':
        tag           = request.POST.get('tag', '').strip()
        serial_number = request.POST.get('serial_number', '').strip()
        component_id  = request.POST.get('component') or None
        location_id   = request.POST.get('location') or None
        group_id      = request.POST.get('owner_group') or None
        form_data = {
            'tag':           tag,
            'serial_number': serial_number,
            'component':     component_id or '',
            'location':      location_id or '',
            'owner_group':   group_id or '',
        }

        user_group_ids = set(request.user.groups.values_list('id', flat=True))

        if not component_id:
            form_error = 'Please choose a component.'
        elif not Component.objects.filter(pk=component_id).exists():
            form_error = 'Please choose a valid component.'
        elif group_id and int(group_id) not in user_group_ids:
            form_error = 'You can only assign a group you belong to.'
        else:
            instance = ComponentInstance.objects.create(
                tag=tag,
                serial_number=serial_number,
                component_id=component_id,
                location_id=location_id,
                owner_group_id=group_id,
                owner_user=request.user,
                created_by=request.user,
            )
            LogEntry.objects.create(
                component_instance=instance,
                topic='inventory',
                logged_by=request.user,
                entry=(
                    f"Instance {instance.tag or instance.pk} of {instance.component.name} created by "
                    f"{request.user.get_full_name() or request.user.username}. "
                    f"Location: {instance.location or 'unassigned'}."
                ),
            )
            return redirect('inventory-detail', pk=instance.pk)

    q           = request.GET.get('q', '')
    location    = request.GET.get('location', '')
    system      = request.GET.get('system', '')
    group       = request.GET.get('group', '')
    owner       = request.GET.get('owner', '')
    sort        = request.GET.get('sort', 'component')
    direction   = request.GET.get('dir', 'asc')

    qs = ComponentInstance.objects.select_related(
        'component', 'component__technical_system',
        'location', 'location__institution', 'owner_group', 'owner_user',
    )
    if q:
        qs = qs.filter(
            Q(tag__icontains=q) |
            Q(serial_number__icontains=q) | Q(component__name__icontains=q)
        )
    if location:
        qs = qs.filter(location_id=location)
    if system:
        qs = qs.filter(component__technical_system__name=system)
    if group:
        qs = qs.filter(owner_group__name=group)
    if owner:
        qs = qs.filter(owner_user__username=owner)

    _sort_map = {
        'tag':       'tag',
        'component': 'component__name',
        'system':    'component__technical_system__name',
        'serial':    'serial_number',
        'location':  'location__name',
        'group':     'owner_group__name',
        'owner':     'owner_user__username',
        'created':   'created_on',
    }
    order_field = _sort_map.get(sort, 'component__name')
    if direction == 'desc':
        order_field = '-' + order_field
    qs = qs.order_by(order_field)

    _excl   = {'sort', 'dir', 'page'}
    sort_qs = '&'.join(
        f'{k}={v}' for k, v in request.GET.items() if k not in _excl
    )

    per_page  = _resolve_page_size(request)
    paginator = Paginator(qs, per_page)
    page_obj  = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj':     page_obj,
        'q':            q,
        'location':     location,
        'system':       system,
        'group':        group,
        'owner':        owner,
        'sort':         sort,
        'dir':          direction,
        'sort_qs':      sort_qs,
        'per_page':         per_page,
        'per_page_choices': PAGE_SIZE_CHOICES,
        'systems':      TechnicalSystem.objects.order_by('name'),
        'groups':       Group.objects.order_by('name'),
        'users':        User.objects.order_by('username'),
        'query_str':    _qs(request),
        'active_page':  'inventory',
        'components':      Component.objects.order_by('name'),
        'locations':       Location.objects.select_related('institution').order_by('name'),
        'user_groups':     request.user.groups.order_by('name'),
        'show_add_button': True,
        'form_error':      form_error,
        'form_data':       form_data,
        'open_modal':      bool(form_error),
    }
    return render(request, 'cdb/inventory.html', context)


@login_required
def inventory_property_update(request, pk, property_id):
    """Inline-edit a property's value/units from the instance detail page.
    property_id may refer to either an instance-owned PropertyValue or one
    inherited from the instance's Component (as returned by
    effective_properties()) -- scoped to one or the other so an unrelated
    property can't be targeted by guessing an id.

    Editing an instance-owned row updates it in place. Editing an inherited
    row does NOT mutate the shared component-level default (that would
    silently change the value for every other instance); instead it
    creates (or updates) this instance's own override for the same
    (property_type, tag) pair -- the same effect as using the "Add /
    Override" form with a matching Property Type and Tag.

    Document/Image property types (and any property that happens to have a
    file attached) are excluded, same as the component-level version of
    this feature. Members of the instance's owner_group (or a superuser)
    only; 403 otherwise -- same authorization pattern as
    inventory_transfer_owner."""
    instance = get_object_or_404(ComponentInstance, pk=pk)
    pv = get_object_or_404(
        PropertyValue,
        Q(component_instance=instance) | Q(component_id=instance.component_id),
        pk=property_id,
    )
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_edit_properties = (
        (bool(instance.owner_group_id) and instance.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )
    if request.method == 'POST':
        if not can_edit_properties:
            return HttpResponseForbidden("You don't have permission to edit properties of this instance.")
        if pv.property_type.handler not in ('document', 'image') and not pv.file:
            value = request.POST.get('value', '').strip()
            units = request.POST.get('units', '').strip()
            if pv.component_instance_id == instance.pk:
                pv.value = value
                pv.units = units
                pv.save()
            else:
                override, created = PropertyValue.objects.get_or_create(
                    component_instance=instance, property_type_id=pv.property_type_id, tag=pv.tag,
                    defaults={'value': value, 'units': units},
                )
                if not created:
                    override.value = value
                    override.units = units
                    override.save()
    return redirect('inventory-detail', pk=instance.pk)


@login_required
def inventory_detail(request, pk):
    """Instance detail page. Also handles the "Add / Override Property"
    pop-up form: a POST here (property_type, tag, value, units) creates an
    instance-level PropertyValue. If its (property_type, tag) matches one
    inherited from the component, it overrides (hides) that default; if not,
    it's simply an additional property on this instance alone. See
    ComponentInstance.effective_properties(). Members of the instance's
    owner_group (or a superuser) may add/override a property -- 403 on a
    POST from anyone else, same authorization pattern as
    inventory_transfer_owner."""
    instance = get_object_or_404(
        ComponentInstance.objects.prefetch_related(
            'properties__property_type',
            'log_entries__logged_by',
            'design_installations__element__design',
        ).select_related(
            'component', 'location', 'location__institution',
            'owner_group', 'owner_user', 'technical_system',
        ),
        pk=pk,
    )

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_edit_properties = (
        (bool(instance.owner_group_id) and instance.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    form_error = None
    form_data  = {}

    if request.method == 'POST':
        if not can_edit_properties:
            return HttpResponseForbidden("You don't have permission to add properties to this instance.")
        property_type_id = request.POST.get('property_type') or None
        tag               = request.POST.get('tag', '').strip()
        value             = request.POST.get('value', '').strip()
        units             = request.POST.get('units', '').strip()
        uploaded_file     = request.FILES.get('file')
        form_data = {'property_type': property_type_id or '', 'tag': tag, 'value': value, 'units': units}

        if not property_type_id:
            form_error = 'Property Type is required.'
        else:
            # Same reasoning as component_detail: (component_instance,
            # property_type, tag) identifies "the same property" -- update
            # it in place on resubmission instead of creating a duplicate.
            pv, created = PropertyValue.objects.get_or_create(
                component_instance=instance, property_type_id=property_type_id, tag=tag,
                defaults={'value': value, 'units': units, 'file': uploaded_file},
            )
            if not created:
                pv.value = value
                pv.units = units
                if uploaded_file:
                    pv.file = uploaded_file
                pv.save()
            return redirect('inventory-detail', pk=instance.pk)

    can_transfer_owner = (
        (bool(instance.owner_group_id) and instance.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )
    # Same group-membership-or-superuser check gates deletion. Uses the
    # already-prefetched design_installations list (not .exists(), which
    # would issue its own query) to decide whether the item is currently
    # installed in a design -- if so, the Delete control is withheld and a
    # message points the user at removing it from the design first.
    can_delete   = can_transfer_owner
    is_in_design = bool(instance.design_installations.all())
    group_members = (
        instance.owner_group.user_set.order_by('username') if instance.owner_group_id else User.objects.none()
    )
    missing_identifiers = not instance.tag or not instance.serial_number

    context = {
        'instance':            instance,
        'active_page':         'inventory',
        'property_types':      PropertyType.objects.order_by('name'),
        'can_edit_properties': can_edit_properties,
        'can_transfer_owner':  can_transfer_owner,
        'can_delete':          can_delete,
        'is_in_design':        is_in_design,
        'group_members':       group_members,
        'institutions':        Institution.objects.order_by('name'),
        'locations':            Location.objects.select_related('institution').order_by('name'),
        'missing_identifiers': missing_identifiers,
        'form_error':      form_error,
        'form_data':       form_data,
        'open_modal':      bool(form_error),
    }
    return render(request, 'cdb/inventory_detail.html', context)


@login_required
def inventory_update_location(request, pk):
    """Move a ComponentInstance to a different Location, from the
    Institution/Location controls on the instance detail page. Gated by
    the same group-membership-or-superuser check as ownership transfer.
    The Institution dropdown is only a client-side filter over the
    Location list -- Location already carries its own institution FK, so
    the server only needs to persist the submitted location; there's no
    way to end up with an institution/location pair that disagree with
    each other since every option in the list is a real, existing
    Location row with its own correct institution."""
    instance = get_object_or_404(ComponentInstance, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_manage = (
        (bool(instance.owner_group_id) and instance.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_manage:
            return HttpResponseForbidden("You don't have permission to move this instance.")
        location_id = request.POST.get('location') or None
        old_location = instance.location
        new_location = None

        if location_id:
            new_location = Location.objects.filter(pk=location_id).first()
            if new_location:
                instance.location_id = location_id
                instance.save()
        else:
            instance.location_id = None
            instance.save()

        if old_location != new_location:
            LogEntry.objects.create(
                component_instance=instance,
                topic='inventory',
                logged_by=request.user,
                entry=(
                    f"{instance.tag or instance.pk} moved from "
                    f"{old_location or 'unassigned'} to {new_location or 'unassigned'} by "
                    f"{request.user.get_full_name() or request.user.username}."
                ),
            )

    return redirect('inventory-detail', pk=instance.pk)


@login_required
def inventory_update_identifiers(request, pk):
    """Fill in a missing Tag and/or Serial Number for a ComponentInstance,
    from the "Would you like to fill this in?" prompt shown on the instance
    detail page when either field is blank. Same group-membership-or-
    superuser gate as the other instance-management controls.

    Only fields that are currently blank are ever touched -- if a value
    somehow arrives for a field that's already set (stale form, direct
    POST), it's ignored rather than overwriting an established identifier.
    This control exists purely to fill gaps."""
    instance = get_object_or_404(ComponentInstance, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_manage = (
        (bool(instance.owner_group_id) and instance.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_manage:
            return HttpResponseForbidden("You don't have permission to edit this instance.")
        changes = []
        if not instance.tag:
            new_tag = request.POST.get('tag', '').strip()
            if new_tag:
                instance.tag = new_tag
                changes.append(f"Tag set to {new_tag}")
        if not instance.serial_number:
            new_serial = request.POST.get('serial_number', '').strip()
            if new_serial:
                instance.serial_number = new_serial
                changes.append(f"Serial Number set to {new_serial}")
        if changes:
            instance.save()
            LogEntry.objects.create(
                component_instance=instance,
                topic='inventory',
                logged_by=request.user,
                entry=(
                    f"{'; '.join(changes)} for {instance.tag or instance.pk} by "
                    f"{request.user.get_full_name() or request.user.username}."
                ),
            )

    return redirect('inventory-detail', pk=instance.pk)


@login_required
def inventory_transfer_owner(request, pk):
    """Transfer a ComponentInstance's ownership to another member of its own
    owner_group, from the "Current Owner" control on the instance detail
    page. Same pattern as component_transfer_owner: group members (or any
    superuser) may initiate a transfer, enforced server-side with 403 on an
    unauthorized POST; a target user outside the owner_group is a
    business-rule violation from an otherwise authorized user, not an
    authorization breach, so it's silently ignored rather than rejected."""
    instance = get_object_or_404(ComponentInstance, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_transfer = (
        (bool(instance.owner_group_id) and instance.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_transfer:
            return HttpResponseForbidden("You don't have permission to change this instance's owner.")
        new_owner_id = request.POST.get('owner_user') or None
        if new_owner_id and User.objects.filter(pk=new_owner_id, groups=instance.owner_group_id).exists():
            if instance.owner_user_id != int(new_owner_id):
                old_owner = instance.owner_user
                new_owner = User.objects.get(pk=new_owner_id)
                instance.owner_user = new_owner
                instance.save()
                LogEntry.objects.create(
                    component_instance=instance,
                    topic='inventory',
                    logged_by=request.user,
                    entry=(
                        f"Ownership of {instance.tag or instance.pk} transferred from "
                        f"{old_owner.get_full_name() or old_owner.username if old_owner else 'unassigned'} to "
                        f"{new_owner.get_full_name() or new_owner.username} by "
                        f"{request.user.get_full_name() or request.user.username}."
                    ),
                )

    return redirect('inventory-detail', pk=instance.pk)


@login_required
def inventory_delete(request, pk):
    """Delete a ComponentInstance entirely, from the "Delete" button on its
    detail page (the button asks for confirmation client-side first). Only
    possible while the instance isn't installed into any design -- removing
    a physical item that a design still references would silently break
    that design's Bill of Materials, so it has to be unassigned there first
    (see design_element_unassign_instance). The button itself is hidden
    once it's in use (see inventory_detail's is_in_design), and this is the
    authoritative server-side check, not just a hidden control. Members of
    the instance's owner_group (or a superuser) may delete; 403
    otherwise.

    The deletion is recorded as a LogEntry against the parent Component
    (not the instance) -- LogEntry.component_instance is on_delete=CASCADE,
    so a log entry attached only to the instance being deleted would vanish
    along with it. Logging on the component instead means the record
    survives and stays visible on the component's own Log Entries panel."""
    instance = get_object_or_404(ComponentInstance, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_delete = (
        (bool(instance.owner_group_id) and instance.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_delete:
            return HttpResponseForbidden("You don't have permission to delete this instance.")
        if instance.design_installations.exists():
            # In use by a design -- business-rule state check, not an
            # authorization failure: ignore silently rather than 403, same
            # treatment as the locked-template/locked-design POSTs
            # elsewhere in this file.
            return redirect('inventory-detail', pk=instance.pk)
        LogEntry.objects.create(
            component=instance.component,
            topic='inventory',
            logged_by=request.user,
            entry=(
                f"Instance {instance.tag or instance.pk} of {instance.component.name} "
                f"(serial {instance.serial_number or 'unassigned'}) deleted from inventory by "
                f"{request.user.get_full_name() or request.user.username}. "
                f"Last location: {instance.location or 'unassigned'}."
            ),
        )
        instance.delete()
        return redirect('inventory-list')

    return redirect('inventory-detail', pk=instance.pk)


@login_required
def inventory_qr(request, pk):
    """PNG QR code encoding this instance's ID, for the "QR" pop-up on the
    instance detail page. Generated server-side with the `qrcode` package
    (pure Python + Pillow) -- no network calls, no client-side JS library,
    so it works the same whether or not the deployment has outbound
    internet access."""
    instance = get_object_or_404(ComponentInstance, pk=pk)
    img = qrcode.make(str(instance.pk), box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return HttpResponse(buf.getvalue(), content_type='image/png')


# ── Designs ───────────────────────────────────────────────────────────────────

@login_required
def design_list(request):
    """List/search designs. Also handles the "New from Template" pop-up
    form: a POST here (template, name) instantiates a DesignTemplate into a
    real Design -- one DesignElement per template placeholder, ready to have
    its placeholders replaced with actual inventory instances on the design
    detail page. Only members of the template's owner_group (or a superuser)
    may instantiate it; the dropdown only offers those templates, and the
    server enforces the same rule with 403 on a direct POST. On validation
    failure the list re-renders with the modal reopened and values kept."""
    form_error = None
    form_data  = {}

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    if request.user.is_superuser:
        usable_templates = DesignTemplate.objects.prefetch_related('elements').order_by('name')
    else:
        usable_templates = DesignTemplate.objects.filter(
            owner_group_id__in=user_group_ids
        ).prefetch_related('elements').order_by('name')

    if request.method == 'POST':
        template_id = request.POST.get('template') or None
        name        = request.POST.get('name', '').strip()
        form_data   = {'template': template_id or '', 'name': name}

        template = DesignTemplate.objects.filter(pk=template_id).first() if template_id else None
        can_instantiate = template is not None and (
            request.user.is_superuser
            or (bool(template.owner_group_id) and template.owner_group_id in user_group_ids)
        )

        if not template:
            form_error = 'Please choose a template.'
        elif not can_instantiate:
            return HttpResponseForbidden("You don't have permission to instantiate this template.")
        elif not name:
            form_error = 'Design name is required.'
        elif Design.objects.filter(name=name).exists():
            form_error = f'A design named "{name}" already exists.'
        else:
            design = Design.objects.create(
                name=name,
                description=template.description,
                project=template.project,
                template=template,
                owner_group=template.owner_group,
                owner_user=request.user,
            )
            for el in template.elements.all():
                DesignElement.objects.create(
                    design=design,
                    element_name=el.element_name,
                    component=el.component,
                    quantity=el.quantity,
                    description=el.description,
                )
            LogEntry.objects.create(
                design=design,
                topic='design',
                logged_by=request.user,
                entry=(
                    f"Design {design.name} created from template {template.name} by "
                    f"{request.user.get_full_name() or request.user.username}."
                ),
            )
            return redirect('design-detail', pk=design.pk)

    q         = request.GET.get('q', '')
    group     = request.GET.get('group', '')
    owner     = request.GET.get('owner', '')
    sort      = request.GET.get('sort', '')
    direction = request.GET.get('dir', 'asc')

    qs = Design.objects.select_related('owner_group', 'owner_user').annotate(
        element_count=Count('elements')
    )
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
    if group:
        qs = qs.filter(owner_group__name=group)
    if owner:
        qs = qs.filter(owner_user__username=owner)

    _sort_map = {
        'name':  'name',
        'count': 'element_count',
        'group': 'owner_group__name',
        'owner': 'owner_user__username',
    }
    if sort in _sort_map:
        order_field = _sort_map[sort]
        if direction == 'desc':
            order_field = '-' + order_field
        qs = qs.order_by(order_field, 'name')
    else:
        qs = qs.order_by('name')

    paginator = Paginator(qs, PAGE_SIZE)
    page_obj  = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj':    page_obj,
        'q':           q,
        'group':       group,
        'owner':       owner,
        'sort':        sort,
        'dir':         direction,
        'sort_qs':     _qs(request, 'sort', 'dir'),
        'groups':      Group.objects.order_by('name'),
        'users':       User.objects.order_by('username'),
        'query_str':   _qs(request),
        'templates':   usable_templates,
        'form_error':  form_error,
        'form_data':   form_data,
        'open_modal':  bool(form_error),
        'active_page': 'designs',
    }
    return render(request, 'cdb/designs.html', context)


@login_required
def design_detail(request, pk):
    """Design detail page. Also handles the "Add Property" pop-up form:
    a POST here (property_type, tag, value, units) creates a design-level
    PropertyValue. Members of the design's owner_group (or a superuser) may
    add a property -- the button is hidden from everyone else, and a POST
    from anyone else is rejected with 403 (same authorization pattern as
    design_delete etc.)."""
    design = get_object_or_404(
        Design.objects.prefetch_related(
            'properties__property_type',
            'log_entries__logged_by',
        ).select_related('owner_group', 'owner_user', 'template',
                         'location', 'location__institution'),
        pk=pk,
    )

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_add_property = (
        (bool(design.owner_group_id) and design.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    form_error = None
    form_data  = {}

    if request.method == 'POST':
        if not can_add_property:
            return HttpResponseForbidden("You don't have permission to add properties to this design.")
        property_type_id = request.POST.get('property_type') or None
        tag               = request.POST.get('tag', '').strip()
        value             = request.POST.get('value', '').strip()
        units             = request.POST.get('units', '').strip()
        uploaded_file     = request.FILES.get('file')
        form_data = {'property_type': property_type_id or '', 'tag': tag, 'value': value, 'units': units}

        if not property_type_id:
            form_error = 'Property Type is required.'
        else:
            pv, created = PropertyValue.objects.get_or_create(
                design=design, property_type_id=property_type_id, tag=tag,
                defaults={'value': value, 'units': units, 'file': uploaded_file},
            )
            if not created:
                pv.value = value
                pv.units = units
                if uploaded_file:
                    pv.file = uploaded_file
                pv.save()
            return redirect('design-detail', pk=design.pk)

    bom_rows = _build_bom(design)

    # Placeholder-replacement controls: members of the design's owner_group
    # (or a superuser) may swap a component placeholder row for an actual
    # inventory instance of that same component. Only rows belonging to THIS
    # design are editable -- rows recursed in from child designs must be
    # edited on their own design's page. Available instances are fetched in
    # one query and grouped by component so each editable row gets its own
    # dropdown without a per-row query.
    # can_add_property already includes the superuser bypass.
    can_edit_elements = can_add_property

    # A design is assembled in exactly one place, so all its instances must
    # come from the same location. Until the design's assembly location is
    # picked, the per-placeholder instance dropdowns are withheld and the
    # page prompts for the location instead; once picked, each dropdown
    # offers only instances of that component stored at that location.
    has_placeholder_rows = any(
        row['depth'] == 0 and row['element'].component_id is not None
        for row in bom_rows
    )
    needs_location = can_edit_elements and has_placeholder_rows and design.location_id is None

    if can_edit_elements and design.location_id is not None:
        editable_component_ids = {
            row['element'].component_id for row in bom_rows
            if row['depth'] == 0 and row['element'].component_id is not None
        }
        # A ComponentInstance is a physical inventory item -- it can only be
        # installed in one design at a time, not just one slot of THIS
        # design. So exclude any instance already installed anywhere, in any
        # design, not merely this one.
        used_instance_ids = set(
            DesignElementInstance.objects.values_list('instance_id', flat=True)
        )
        instances_by_component = {}
        for inst in ComponentInstance.objects.filter(
            component_id__in=editable_component_ids,
            location_id=design.location_id,
        ).exclude(pk__in=used_instance_ids).select_related(
            'location', 'location__institution'
        ).order_by('tag', 'serial_number'):
            instances_by_component.setdefault(inst.component_id, []).append(inst)
        for row in bom_rows:
            if row['depth'] == 0 and row['element'].component_id is not None:
                row['editable'] = True
                row['available_instances'] = instances_by_component.get(row['element'].component_id, [])
            else:
                row['editable'] = False

    context  = {
        'design':            design,
        'bom_rows':          bom_rows,
        'active_page':       'designs',
        'can_add_property':  can_add_property,
        'can_edit_elements': can_edit_elements,
        'needs_location':    needs_location,
        'institutions':      Institution.objects.order_by('name'),
        'locations':         Location.objects.select_related('institution').order_by('name'),
        'property_types':    PropertyType.objects.order_by('name'),
        'form_error':        form_error,
        'form_data':         form_data,
        'open_modal':        bool(form_error),
    }
    return render(request, 'cdb/design_detail.html', context)


@login_required
def design_property_update(request, pk, property_id):
    """Inline-edit a design property's value/units from the Properties
    panel. property_id is scoped to design=pk. Members of the design's
    owner_group (or a superuser) may edit -- same authorization check as
    adding a property; a POST from anyone else is rejected with 403.
    Document/Image property types (and any property that happens to have a
    file attached) are excluded from editing regardless of group
    membership, same as the component-level version of this feature."""
    design = get_object_or_404(Design, pk=pk)
    pv = get_object_or_404(PropertyValue, pk=property_id, design=design)

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_edit = (
        (bool(design.owner_group_id) and design.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_edit:
            return HttpResponseForbidden("You don't have permission to edit properties of this design.")
        if pv.property_type.handler not in ('document', 'image') and not pv.file:
            pv.value = request.POST.get('value', '').strip()
            pv.units = request.POST.get('units', '').strip()
            pv.save()
    return redirect('design-detail', pk=design.pk)


@login_required
def design_delete(request, pk):
    """Delete a design entirely, from the "Delete Design" button on its
    detail page (the button asks for confirmation client-side first).
    Members of the design's owner_group (or a superuser) may delete; 403
    otherwise. Elements, slot assignments, properties, and log entries go
    with it (CASCADE). If this was the only design instantiated from its
    template, the template automatically becomes editable again -- the lock
    is simply "does any design based on it exist", so no extra bookkeeping
    is needed here."""
    design = get_object_or_404(Design, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_delete = (
        (bool(design.owner_group_id) and design.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_delete:
            return HttpResponseForbidden("You don't have permission to delete this design.")
        design.delete()
        return redirect('design-list')

    return redirect('design-detail', pk=design.pk)


@login_required
def design_update_location(request, pk):
    """Set (or change) a design's assembly location, from the
    Institution/Location picker on the design detail page. A design belongs
    to exactly one location, and this choice constrains which inventory
    instances the placeholder-replacement dropdowns offer. Members of the
    design's owner_group (or a superuser) may set it -- 403 otherwise, same
    pattern as the rest of the design-editing controls."""
    design = get_object_or_404(Design, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_edit = (
        (bool(design.owner_group_id) and design.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_edit:
            return HttpResponseForbidden("You don't have permission to set this design's location.")
        location_id = request.POST.get('location') or None
        old_location = design.location
        new_location = Location.objects.filter(pk=location_id).first() if location_id else None

        if location_id and not new_location:
            return redirect('design-detail', pk=design.pk)

        if old_location != new_location:
            design.location = new_location
            design.save()
            LogEntry.objects.create(
                design=design,
                topic='design',
                logged_by=request.user,
                entry=(
                    f"Assembly location of {design.name} set to "
                    f"{new_location or 'unassigned'}"
                    f"{f' (was {old_location})' if old_location else ''} by "
                    f"{request.user.get_full_name() or request.user.username}."
                ),
            )

    return redirect('design-detail', pk=design.pk)


@login_required
def design_element_assign_instance(request, pk, element_id):
    """Replace a component placeholder in a design's Bill of Materials with
    an actual ComponentInstance from the inventory (or clear the assignment
    by submitting an empty value). This is how a template-derived design
    goes from "needs 4 SiPMs" to "contains these 4 specific SiPMs".

    Members of the design's owner_group (or a superuser) may do this,
    enforced with 403 on an unauthorized POST. The chosen instance must be
    an instance of the placeholder's own component -- the dropdown only
    offers those, so a POST naming any other instance is a business-rule
    violation from an otherwise authorized user and is silently ignored."""
    design = get_object_or_404(Design, pk=pk)
    element = get_object_or_404(DesignElement, pk=element_id, design=design)

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_edit = (
        (bool(design.owner_group_id) and design.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_edit:
            return HttpResponseForbidden("You don't have permission to edit this design's elements.")
        instance_id = request.POST.get('instance') or None
        if instance_id:
            # Adding one instance to one of the element's slots. All of these
            # are business rules the dropdown already respects, so a POST
            # violating any of them is silently ignored rather than 403'd:
            #   - the design must have an assembly location picked,
            #   - the instance must be of the element's own component,
            #   - it must be stored at the design's assembly location,
            #   - it must not already be installed anywhere, in ANY design --
            #     a physical inventory item can only be physically present
            #     in one design at a time, not just one slot of this one,
            #   - the element must have a free slot (fewer than `quantity`
            #     instances installed).
            instance = ComponentInstance.objects.filter(
                pk=instance_id, component_id=element.component_id,
                location_id=design.location_id,
            ).first() if design.location_id else None
            already_used = instance and DesignElementInstance.objects.filter(
                instance=instance
            ).exists()
            slots_full = element.installed_instances.count() >= element.quantity
            if instance and not already_used and not slots_full:
                DesignElementInstance.objects.create(element=element, instance=instance)
                LogEntry.objects.create(
                    design=design,
                    topic='design',
                    logged_by=request.user,
                    entry=(
                        f"Element {element.element_name}: inventory instance "
                        f"{instance.tag or instance.pk} installed "
                        f"({element.installed_instances.count()} of {element.quantity}) by "
                        f"{request.user.get_full_name() or request.user.username}."
                    ),
                )

    return redirect('design-detail', pk=design.pk)


@login_required
def design_element_unassign_instance(request, pk, element_id):
    """Remove one installed instance from a design element's slots (the x
    next to its tag in the BOM), returning that slot to a placeholder. Same
    authorization as assignment: design owner_group members or superusers,
    403 otherwise. Naming an instance that isn't installed in this element
    is silently ignored."""
    design = get_object_or_404(Design, pk=pk)
    element = get_object_or_404(DesignElement, pk=element_id, design=design)

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_edit = (
        (bool(design.owner_group_id) and design.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_edit:
            return HttpResponseForbidden("You don't have permission to edit this design's elements.")
        instance_id = request.POST.get('instance') or None
        dei = DesignElementInstance.objects.filter(
            element=element, instance_id=instance_id
        ).select_related('instance').first() if instance_id else None
        if dei:
            removed = dei.instance
            dei.delete()
            LogEntry.objects.create(
                design=design,
                topic='design',
                logged_by=request.user,
                entry=(
                    f"Element {element.element_name}: inventory instance "
                    f"{removed.tag or removed.pk} removed (slot back to placeholder) by "
                    f"{request.user.get_full_name() or request.user.username}."
                ),
            )

    return redirect('design-detail', pk=design.pk)


@login_required
def template_list(request):
    """Design Templates page: browse every template and create new ones via
    the "New Template" pop-up (name, description, owner group). The owner
    group is required because templates are group-owned throughout: only
    members of that group may edit the template or instantiate designs from
    it. A user may only create a template for a group they belong to
    (superusers may pick any group); anything else is rejected with 403."""
    form_error = None
    form_data  = {}

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    if request.user.is_superuser:
        creatable_groups = Group.objects.order_by('name')
    else:
        creatable_groups = Group.objects.filter(id__in=user_group_ids).order_by('name')

    if request.method == 'POST':
        name        = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        group_id    = request.POST.get('owner_group') or None
        form_data   = {'name': name, 'description': description, 'owner_group': group_id or ''}

        allowed_group = group_id and (
            request.user.is_superuser or int(group_id) in user_group_ids
        ) and Group.objects.filter(pk=group_id).exists()

        if not name:
            form_error = 'Name is required.'
        elif DesignTemplate.objects.filter(name=name).exists():
            form_error = f'A template named "{name}" already exists.'
        elif not group_id:
            form_error = 'Owner group is required.'
        elif not allowed_group:
            return HttpResponseForbidden("You can only create templates for a group you belong to.")
        else:
            template = DesignTemplate.objects.create(
                name=name,
                description=description,
                owner_group_id=group_id,
                owner_user=request.user,
                created_by=request.user,
            )
            return redirect('template-detail', pk=template.pk)

    q  = request.GET.get('q', '')
    qs = DesignTemplate.objects.select_related('owner_group', 'owner_user').annotate(
        placeholder_count=Count('elements', distinct=True),
        design_count=Count('designs', distinct=True),
    ).order_by('name')
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))

    paginator = Paginator(qs, PAGE_SIZE)
    page_obj  = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj':     page_obj,
        'q':            q,
        'query_str':    _qs(request),
        'groups':       creatable_groups,
        'form_error':   form_error,
        'form_data':    form_data,
        'open_modal':   bool(form_error),
        'active_page':  'design-templates',
    }
    return render(request, 'cdb/templates_list.html', context)


@login_required
def template_detail(request, pk):
    """Design Template detail page: the placeholder table plus editing
    tools. A POST here adds a placeholder (element_name, component,
    quantity). Members of the template's owner_group (or a superuser) may
    edit; everyone else sees a read-only view and gets 403 on a direct
    POST."""
    template = get_object_or_404(
        DesignTemplate.objects.select_related('owner_group', 'owner_user')
                              .prefetch_related('elements__component'),
        pk=pk,
    )

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    is_member = (
        (bool(template.owner_group_id) and template.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )
    # Once at least one Design has been instantiated from a template, the
    # template becomes immutable: those designs were created from a specific
    # bill of placeholders, and letting the template drift afterwards would
    # make "instantiated from BEMC tower" mean different things at different
    # times. Editing tools disappear for everyone; a fresh template with no
    # designs yet stays fully editable.
    is_locked = template.designs.exists()
    can_edit  = is_member and not is_locked

    form_error = None
    form_data  = {}

    if request.method == 'POST':
        if not is_member:
            return HttpResponseForbidden("You don't have permission to edit this template.")
        if is_locked:
            # Authorized user, but the template is frozen -- state-based
            # business rule, not an authorization failure: ignore silently.
            return redirect('template-detail', pk=template.pk)
        element_name = request.POST.get('element_name', '').strip()
        component_id = request.POST.get('component') or None
        try:
            quantity = max(int(request.POST.get('quantity', 1)), 1)
        except (TypeError, ValueError):
            quantity = 1
        form_data = {'element_name': element_name, 'component': component_id or '', 'quantity': quantity}

        if not element_name:
            form_error = 'Placeholder name is required.'
        elif template.elements.filter(element_name=element_name).exists():
            form_error = f'This template already has a placeholder named "{element_name}".'
        elif not component_id or not Component.objects.filter(pk=component_id).exists():
            form_error = 'Please choose a component.'
        else:
            DesignTemplateElement.objects.create(
                template=template,
                element_name=element_name,
                component_id=component_id,
                quantity=quantity,
            )
            return redirect('template-detail', pk=template.pk)

    context = {
        'template':     template,
        'can_edit':     can_edit,
        'is_locked':    is_locked,
        'components':   Component.objects.order_by('name'),
        'designs_from': template.designs.select_related('owner_user').order_by('name'),
        'form_error':   form_error,
        'form_data':    form_data,
        'open_modal':   bool(form_error),
        'active_page':  'design-templates',
    }
    return render(request, 'cdb/template_detail.html', context)


@login_required
def template_delete(request, pk):
    """Delete a Design Template entirely, from the "Delete Template" button
    on its detail page (confirmed client-side first) -- modeled on
    design_delete. Only possible while the template is unlocked, i.e. no
    Design has ever been instantiated from it; the button itself is hidden
    once it's locked (see template_detail's is_locked), and this is the
    authoritative server-side check, not just a hidden control. Members of
    the template's owner_group (or a superuser) may delete; 403 otherwise."""
    template = get_object_or_404(DesignTemplate, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_delete = (
        (bool(template.owner_group_id) and template.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_delete:
            return HttpResponseForbidden("You don't have permission to delete this template.")
        if template.designs.exists():
            # Locked -- a design exists based on this template. Business-rule
            # state check, not an authorization failure: ignore silently
            # rather than 403, same treatment as the other locked-template
            # POSTs above.
            return redirect('template-detail', pk=template.pk)
        template.delete()
        return redirect('template-list')

    return redirect('template-detail', pk=template.pk)


@login_required
def template_element_update(request, pk, element_id):
    """Inline-edit a template placeholder's quantity. Owner-group members
    (or superusers) only; 403 otherwise. Invalid quantities fall back to 1;
    quantity is the only editable field -- changing what component a
    placeholder points at is delete-and-recreate, deliberately, so it can't
    drift silently under designs already instantiated from the template."""
    template = get_object_or_404(DesignTemplate, pk=pk)
    element = get_object_or_404(DesignTemplateElement, pk=element_id, template=template)

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    is_member = (
        (bool(template.owner_group_id) and template.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not is_member:
            return HttpResponseForbidden("You don't have permission to edit this template.")
        if template.designs.exists():
            # Template frozen once designs exist -- see template_detail.
            return redirect('template-detail', pk=template.pk)
        try:
            element.quantity = max(int(request.POST.get('quantity', element.quantity)), 1)
            element.save()
        except (TypeError, ValueError):
            pass

    return redirect('template-detail', pk=template.pk)


@login_required
def template_element_delete(request, pk, element_id):
    """Remove a placeholder from a template. Owner-group members (or
    superusers) only; 403 otherwise. Existing designs instantiated earlier
    are unaffected -- they own real DesignElement copies, not references to
    the template's rows."""
    template = get_object_or_404(DesignTemplate, pk=pk)
    element = get_object_or_404(DesignTemplateElement, pk=element_id, template=template)

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    is_member = (
        (bool(template.owner_group_id) and template.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not is_member:
            return HttpResponseForbidden("You don't have permission to edit this template.")
        if template.designs.exists():
            # Template frozen once designs exist -- see template_detail.
            return redirect('template-detail', pk=template.pk)
        element.delete()

    return redirect('template-detail', pk=template.pk)


def _build_bom(design, depth=0, max_depth=10):
    """Flat list of BOM rows with depth info for template indentation.

    Each row carries a 'row_type' of 'child_design' (the element points at
    another Design), 'instance' (a component element with at least one
    inventory instance installed into its slots), or 'component' (a plain
    catalog placeholder with nothing installed yet). An element with
    quantity N holds up to N installed instances (DesignElementInstance
    rows); 'installed' carries them for the Tag column and
    'installed_count' drives the k/N progress badge."""
    rows = []
    if depth > max_depth:
        return rows
    for el in DesignElement.objects.filter(design=design).select_related(
        'component', 'child_design'
    ).prefetch_related('installed_instances__instance__location__institution'):
        installed = list(el.installed_instances.all())
        if el.child_design_id is not None:
            row_type = 'child_design'
        elif installed:
            row_type = 'instance'
        else:
            row_type = 'component'
        rows.append({
            'element':         el,
            'depth':           depth,
            'indent':          list(range(depth)),   # iterate in template
            'is_design':       el.child_design_id is not None,
            'row_type':        row_type,
            'installed':       installed,
            'installed_count': len(installed),
            'slots_left':      max(el.quantity - len(installed), 0),
        })
        if el.child_design:
            rows.extend(_build_bom(el.child_design, depth + 1, max_depth))
    return rows


# ── Technical Systems ─────────────────────────────────────────────────────────

@login_required
def system_list(request):
    """List all technical systems with component and instance counts."""
    systems = TechnicalSystem.objects.select_related('group').annotate(
        component_count=Count('components', distinct=True),
        instance_count=Count('components__instances', distinct=True),
    ).order_by('name')
    context = {'systems': systems, 'active_page': 'systems'}
    return render(request, 'cdb/systems.html', context)


@login_required
def system_detail(request, pk):
    """Show a single technical system with its inventory items, filterable."""
    system = get_object_or_404(TechnicalSystem, pk=pk)

    # The "System" dropdown in inventory.html posts back to this same URL
    # via GET. Since the system itself is chosen via the URL's <pk>, not a
    # query param, switching the dropdown has to redirect to the newly
    # selected system's own detail page (preserving the other filters)
    # rather than silently being ignored.
    selected_name = request.GET.get('system', '')
    if selected_name and selected_name != system.name:
        other = TechnicalSystem.objects.filter(name=selected_name).first()
        if other:
            params = request.GET.copy()
            params.pop('system', None)
            params.pop('page', None)
            query = params.urlencode()
            url = reverse('system-detail', args=[other.pk])
            if query:
                url = f'{url}?{query}'
            return redirect(url)

    q           = request.GET.get('q', '')
    location    = request.GET.get('location', '')
    group       = request.GET.get('group', '')
    owner       = request.GET.get('owner', '')

    qs = ComponentInstance.objects.filter(
        component__technical_system=system,
    ).select_related(
        'component',
        'location', 'location__institution', 'owner_group',
    )

    if q:
        qs = qs.filter(
            Q(tag__icontains=q) |
            Q(serial_number__icontains=q) | Q(component__name__icontains=q)
        )
    if location:
        qs = qs.filter(location_id=location)
    if group:
        qs = qs.filter(owner_group__name=group)
    if owner:
        qs = qs.filter(owner_user__username=owner)
    per_page  = _resolve_page_size(request)
    paginator = Paginator(qs, per_page)
    page_obj  = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj':     page_obj,
        'q':            q,
        'location':     location,
        'system':       system.name,
        'group':        group,
        'owner':        owner,
        'per_page':         per_page,
        'per_page_choices': PAGE_SIZE_CHOICES,
        'page_title':   'Inventory — ' + system.name,
        'locations':    Location.objects.select_related('institution').order_by('name'),
        'systems':      TechnicalSystem.objects.order_by('name'),
        'groups':       Group.objects.order_by('name'),
        'users':        User.objects.order_by('username'),
        'query_str':    _qs(request),
        'active_page':  'inventory',
    }
    return render(request, 'cdb/inventory.html', context)


# ── Users ─────────────────────────────────────────────────────────────────────

@login_required
def user_list(request):
    """List all site users (excluding the built-in "admin" account) with
    their name, home institution, and email. Optionally filtered down to
    one group via ?group=<name> and/or one institution via
    ?institution=<abbreviation>, and sortable by Last Name or Institution
    via ?sort=last_name|institution&dir=asc|desc."""
    group        = request.GET.get('group', '')
    institution  = request.GET.get('institution', '')
    sort         = request.GET.get('sort', '')
    direction    = request.GET.get('dir', 'asc')

    users = User.objects.exclude(username='admin').select_related(
        'profile__institution',
    ).prefetch_related('groups')
    if group:
        users = users.filter(groups__name=group)
    if institution:
        users = users.filter(profile__institution__abbreviation=institution)

    _sort_map = {
        'last_name':   'last_name',
        'institution': 'profile__institution__name',
    }
    if sort in _sort_map:
        order_field = _sort_map[sort]
        if direction == 'desc':
            order_field = '-' + order_field
        users = users.order_by(order_field, 'first_name', 'username')
    else:
        users = users.order_by('first_name', 'last_name', 'username')

    context = {
        'users':        users,
        'groups':       Group.objects.order_by('name'),
        'group':        group,
        'institutions': Institution.objects.order_by('name'),
        'institution':  institution,
        'sort':         sort,
        'dir':          direction,
        'sort_qs':      _qs(request, 'sort', 'dir'),
        'active_page':  'users',
    }
    return render(request, 'cdb/users.html', context)


# ── Institutions & Locations ──────────────────────────────────────────────────

@login_required
def institution_list(request):
    institutions = Institution.objects.prefetch_related(
        'locations',
        'users__user',
    ).all()
    context = {'institutions': institutions, 'active_page': 'institutions'}
    return render(request, 'cdb/institutions.html', context)


@login_required
def user_inventory(request, username):
    user = get_object_or_404(User, username=username)
    instances = ComponentInstance.objects.filter(
        owner_user=user,
    ).select_related(
        'component', 'technical_system',
        'location', 'location__institution',
        'owner_group',
    ).order_by('component__name', 'tag')

    context = {
        'owner':     user,
        'instances': instances,
        'active_page': 'inventory',
    }
    return render(request, 'cdb/user_inventory.html', context)


@login_required
def location_inventory(request, pk):
    location = get_object_or_404(
        Location.objects.select_related('institution', 'parent'),
        pk=pk,
    )

    system    = request.GET.get('system', '')
    group     = request.GET.get('group', '')
    sort      = request.GET.get('sort', '')
    direction = request.GET.get('dir', 'asc')

    instances = ComponentInstance.objects.filter(
        location=location,
    ).select_related(
        'component', 'technical_system', 'owner_group', 'owner_user',
    )
    if system:
        instances = instances.filter(technical_system__name=system)
    if group:
        instances = instances.filter(owner_group__name=group)

    # Every column but ID is sortable, same convention as inventory_list.
    _sort_map = {
        'tag':       'tag',
        'component': 'component__name',
        'system':    'technical_system__name',
        'serial':    'serial_number',
        'owner':     'owner_user__username',
        'group':     'owner_group__name',
    }
    if sort in _sort_map:
        order_field = _sort_map[sort]
        if direction == 'desc':
            order_field = '-' + order_field
        instances = instances.order_by(order_field, 'component__name', 'tag')
    else:
        instances = instances.order_by('component__name', 'tag')

    context = {
        'location':    location,
        'instances':   instances,
        'system':      system,
        'group':       group,
        'sort':        sort,
        'dir':         direction,
        'sort_qs':     _qs(request, 'sort', 'dir'),
        'systems':     TechnicalSystem.objects.order_by('name'),
        'groups':      Group.objects.order_by('name'),
        'active_page': 'institutions',
    }
    return render(request, 'cdb/location_inventory.html', context)


# ── Activity Log ──────────────────────────────────────────────────────────────

@login_required
def log_list(request):
    q     = request.GET.get('q', '')
    topic = request.GET.get('topic', '')

    qs = LogEntry.objects.select_related(
        'logged_by', 'component', 'component_instance', 'design',
    ).order_by('-timestamp')

    if q:
        qs = qs.filter(entry__icontains=q)
    if topic:
        qs = qs.filter(topic=topic)

    per_page  = _resolve_page_size(request)
    paginator = Paginator(qs, per_page)
    page_obj  = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj':         page_obj,
        'q':                q,
        'topic':            topic,
        'topics':           LogEntry.TOPIC_CHOICES,
        'per_page':         per_page,
        'per_page_choices': PAGE_SIZE_CHOICES,
        'query_str':        _qs(request),
        'active_page':      'logs',
    }
    return render(request, 'cdb/logs.html', context)
