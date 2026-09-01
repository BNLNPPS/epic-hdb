"""
HDB web views — server-rendered Django pages.
URL config: hdb/urls_web.py
"""
import io
import uuid
from itertools import groupby
from urllib.parse import urlencode

from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, HttpResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.db.models import Q, Count
from django.core.paginator import Paginator
from django.utils import timezone
from django.utils.dateparse import parse_date
import qrcode

from .models import (
    Component, ComponentInstance, Design, DesignElement,
    DesignTemplate, DesignTemplateElement, DesignElementInstance,
    Institution, Location, LogEntry, TechnicalSystem, PropertyType, PropertyValue,
    TEMPLATE_NESTING_MAX_DEPTH,
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


def _to_pk_int(value):
    """Parse a POST value as an integer PK (User/Group use Django's default
    integer AutoField). Returns None if missing or not a valid integer, so
    a malformed/tampered POST is treated as "no such target" rather than
    raising an unhandled ValueError from the ORM."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _filtered_inventory_queryset(params):
    """Build the ComponentInstance queryset for a set of inventory list
    filters (q, location, system, group, owner, design), given a plain
    dict or QueryDict of those keys. Factored out of inventory_list so the
    batch-update views (inventory_batch_update_location,
    inventory_batch_update_owner) can recompute exactly "everything
    inventory_list is currently showing" -- for the "select all N matching
    current filters" action -- from request.POST (where inventory.html
    mirrors the active filters as hidden fields under these same names)
    instead of duplicating this filtering logic and risking it drifting
    out of sync with the GET-driven list page.

    Deliberately does NOT include the 'design_installations' prefetch
    inventory_list adds for its own display purposes -- that's a rendering
    optimization for the "Used In Design" column, irrelevant (and wasted
    work) for the batch views, which only ever read pk/location/owner_*
    off the result."""
    q        = params.get('q', '')
    location = params.get('location', '')
    system   = params.get('system', '')
    group    = params.get('group', '')
    owner    = params.get('owner', '')
    design   = params.get('design', '')

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
    # 'unassigned' is a sentinel, not a Design pk -- see inventory_list.
    if design == 'unassigned':
        qs = qs.filter(design_installations__isnull=True)
    elif design:
        qs = qs.filter(design_installations__element__design_id=design)
    return qs


def _selected_inventory_queryset(request, user_group_ids):
    """Resolve which ComponentInstances a batch-update POST (from the
    inventory table's checkbox selection) is targeting, then narrow that
    to only the instances the requester is actually authorized to touch --
    same group-membership-or-superuser rule as every single-instance
    control (inventory_update_location, inventory_transfer_owner), applied
    here as a queryset filter instead of a single boolean.

    Two selection modes, matching the batch form's two states:
      - an explicit list of checked pks (POST['instances'], repeated), or
      - all_filtered=1 -- set when "Select all N matching current filters"
        was used instead of paging through checkboxes -- plus the filter
        params mirrored into hidden fields, re-derived via
        _filtered_inventory_queryset exactly as inventory_list would have
        rendered them (request.POST is empty of query-string filters on
        its own; the hidden fields are what carry them through the
        POST).

    In the explicit-pks mode, an unauthorized pk slipped into the POST
    (never possible through the rendered UI, which only ever emits a
    checkbox for a row the viewer can manage -- but this is the
    authoritative server-side check, not the UI, so it's still enforced
    here) is simply excluded rather than rejecting the whole batch."""
    if request.POST.get('all_filtered') == '1':
        qs = _filtered_inventory_queryset(request.POST)
    else:
        qs = ComponentInstance.objects.filter(pk__in=request.POST.getlist('instances'))
    if not request.user.is_superuser:
        qs = qs.filter(owner_group_id__in=user_group_ids)
    return qs


def _unique_design_name(base):
    """Return `base`, or `base` with an incrementing " (N)" suffix, so a
    recursively auto-created child Design (see _instantiate_design below)
    never collides with an existing one -- Design.name is globally unique."""
    if not Design.objects.filter(name=base).exists():
        return base
    n = 2
    while Design.objects.filter(name=f"{base} ({n})").exists():
        n += 1
    return f"{base} ({n})"


def _instantiate_design(template, name, requesting_user, _depth=0):
    """Instantiate `template` into a real Design -- one DesignElement per
    placeholder. For a COMPONENT placeholder this is exactly what
    design_list has always done. For a TEMPLATE placeholder (a nested
    sub-template, see DesignTemplateElement.child_template), a child
    Design is *also* auto-instantiated the same way (recursively, so
    multi-level templates produce a fully wired-up multi-level Design on
    the first instantiation, not an empty slot someone has to fill in by
    hand) and the resulting DesignElement's child_design points at it.
    quantity is left as-is either way: DesignBOMView.walk() / _build_bom()
    both already treat a child_design element's quantity as a multiplier
    over that child's own BOM contents, so "144 Half-Stave Assemblies"
    means one exemplar Half-Stave Design counted 144 times in the parts
    explosion, not 144 separate Design rows -- nothing downstream needs to
    change for this to compute correctly.

    Every nested sub-template carries its OWN owner_group into the child
    Design it produces (not the parent Design's), same as the top-level
    design already does; the acting user becomes owner_user throughout,
    top and every auto-created child alike. In practice a child_template
    is now required to share its parent template's owner_group (and
    project) -- see DesignTemplate.can_nest -- so this will typically be
    the same group as the parent anyway; it's written per-child rather
    than copied from the top level because that invariant lives on
    DesignTemplateElement, not here, and this function shouldn't have to
    assume it still holds. Authorization is checked once, by the caller,
    against the top-level template being instantiated -- a nested
    sub-template is instantiated as a cascading part of that single
    authorized action, not as a separate one requiring its own group
    membership.

    Bounded by TEMPLATE_NESTING_MAX_DEPTH as belt-and-suspenders on top of
    DesignTemplateElement.save() already making a self-nesting cycle
    impossible to create in the first place -- so in practice this
    recursion is guaranteed to terminate on any template tree that could
    exist at all, and this check should never actually trigger."""
    if _depth > TEMPLATE_NESTING_MAX_DEPTH:
        raise ValidationError(
            f"Template nesting exceeds the maximum supported depth "
            f"({TEMPLATE_NESTING_MAX_DEPTH}) while instantiating {template.name!r}."
        )
    design = Design.objects.create(
        name=name,
        description=template.description,
        project=template.project,
        template=template,
        owner_group=template.owner_group,
        owner_user=requesting_user,
    )
    for el in template.elements.select_related('component', 'child_template'):
        if el.child_template_id:
            child_design = _instantiate_design(
                el.child_template,
                _unique_design_name(f"{name} / {el.element_name}"),
                requesting_user,
                _depth + 1,
            )
            DesignElement.objects.create(
                design=design,
                element_name=el.element_name,
                child_design=child_design,
                quantity=el.quantity,
                description=el.description,
            )
        else:
            DesignElement.objects.create(
                design=design,
                element_name=el.element_name,
                component=el.component,
                quantity=el.quantity,
                description=el.description,
            )
    return design


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
    can_add_instance = (
        (bool(comp.owner_group_id) and comp.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    # Populated only when component_instance_create redirects back here
    # after rejecting a POST with no Location (see that view for why
    # Location is required). Query-string round trip, since that view
    # redirects rather than rendering this template itself.
    instance_error = request.GET.get('instance_error')
    instance_form_data = {
        'tag':           request.GET.get('itag', ''),
        'serial_number': request.GET.get('iserial', ''),
    }

    # Same group-membership check gates the "Current Owner" transfer
    # control -- only members of the component's owner_group may reassign
    # ownership, and the dropdown lists that group's members plus every
    # superuser -- superusers can own (or be assigned) a component outside
    # their own groups, so they must always be valid transfer targets, not
    # just valid initiators. The current owner is included even if they fit
    # neither bucket (e.g. removed from the group since), so the dropdown
    # never silently shows blank for a real, already-set owner.
    can_transfer_owner = can_add_instance or request.user.is_superuser
    owner_choices_q = Q(is_superuser=True)
    if comp.owner_group_id:
        owner_choices_q |= Q(groups=comp.owner_group_id)
    if comp.owner_user_id:
        owner_choices_q |= Q(pk=comp.owner_user_id)
    group_members = User.objects.filter(owner_choices_q).distinct().order_by('username')

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
        'instance_error':      instance_error,
        'instance_form_data':  instance_form_data,
        'open_instance_modal': bool(instance_error),
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
    owner_group -- or a superuser, same exception as can_edit_properties
    and can_transfer_owner elsewhere in this file -- may do this. The
    button is hidden from everyone else, but this is the authoritative,
    server-side check -- a POST here from anyone else (or against a
    component with no owner_group at all to check membership against, from
    a non-superuser) is rejected with 403 rather than silently creating an
    instance owned by a group the requester doesn't belong to.

    A physical inventory item always has a location -- even "at the
    manufacturer" or "in transit" is a location -- so Location is required.
    A POST missing it doesn't create anything; it bounces back to the
    component detail page with the modal reopened, the error shown, and the
    tag/serial the user had already typed preserved (via query string,
    since this view redirects rather than rendering the page itself)."""
    comp = get_object_or_404(Component, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_add = (
        (bool(comp.owner_group_id) and comp.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_add:
            return HttpResponseForbidden("You don't have permission to add instances of this component.")
        tag           = request.POST.get('tag', '').strip()
        serial_number = request.POST.get('serial_number', '').strip()
        location_id   = request.POST.get('location') or None

        if not location_id:
            params = urlencode({
                'instance_error': 'Please choose a location.',
                'itag':           tag,
                'iserial':        serial_number,
            })
            return redirect(f"{reverse('component-detail', kwargs={'pk': comp.pk})}?{params}")

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
    owner_group (or to any superuser), from the "Current Owner" control on
    the component detail page. Only members of the component's owner_group
    -- or a superuser -- may initiate a transfer -- same authorization
    pattern as component_instance_create, enforced with 403 on an
    unauthorized POST, not just hidden client-side.

    The new owner must themselves belong to the component's owner_group, or
    be a superuser -- the dropdown only ever offers that set, but a POST
    naming someone outside it is a business-rule violation from an
    otherwise authorized user, not an authorization breach, so it's
    silently ignored (component redirects unchanged) rather than rejected
    with 403."""
    comp = get_object_or_404(Component, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_transfer = (
        (bool(comp.owner_group_id) and comp.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_transfer:
            return HttpResponseForbidden("You don't have permission to change this component's owner.")
        new_owner_id = _to_pk_int(request.POST.get('owner_user'))
        valid_target_q = Q(is_superuser=True)
        if comp.owner_group_id:
            valid_target_q |= Q(groups=comp.owner_group_id)
        if new_owner_id is not None and User.objects.filter(valid_target_q, pk=new_owner_id).exists():
            if comp.owner_user_id != new_owner_id:
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
    pop-up form: a POST here (component, tag, serial number, location)
    creates a ComponentInstance and redirects to its detail page. The owner
    is always the logged-in user. owner_group is never chosen independently
    here -- it's always inherited from the selected component's own
    owner_group, the same rule component_instance_create uses, so an
    instance can never end up in a different group than the component it's
    an instance of.

    Only members of a component's own owner_group -- or a superuser -- may
    create an instance of it. Same rule as component_instance_create's
    can_add check (including the superuser exception, which matches
    can_edit_properties/can_transfer_owner elsewhere in this file), kept
    identical on purpose so the two "Add Instance" entry points behave the
    same way. The Component dropdown is pre-filtered to the components a
    given user is actually allowed to add (all of them, for a superuser),
    so this is normally impossible to hit through the UI; the check here is
    the authoritative, server-side backstop.

    Location is required -- a physical item always has one, even if that's
    "at the manufacturer" or in transit. On validation failure the list
    re-renders with the modal reopened and the entered values kept."""
    form_error = None
    form_data  = {}
    user_group_ids = set(request.user.groups.values_list('id', flat=True))

    if request.method == 'POST':
        tag           = request.POST.get('tag', '').strip()
        serial_number = request.POST.get('serial_number', '').strip()
        component_id  = request.POST.get('component') or None
        location_id   = request.POST.get('location') or None
        form_data = {
            'tag':           tag,
            'serial_number': serial_number,
            'component':     component_id or '',
            'location':      location_id or '',
        }

        component = Component.objects.filter(pk=component_id).first() if component_id else None

        if not component_id:
            form_error = 'Please choose a component.'
        elif not component:
            form_error = 'Please choose a valid component.'
        elif not (
            request.user.is_superuser
            or (component.owner_group_id and component.owner_group_id in user_group_ids)
        ):
            form_error = 'You can only add inventory items for components owned by a group you belong to.'
        elif not location_id:
            form_error = 'Please choose a location.'
        else:
            instance = ComponentInstance.objects.create(
                tag=tag,
                serial_number=serial_number,
                component_id=component_id,
                location_id=location_id,
                owner_group=component.owner_group,
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
    design      = request.GET.get('design', '')
    sort        = request.GET.get('sort', 'component')
    direction   = request.GET.get('dir', 'asc')

    # Filtering itself lives in _filtered_inventory_queryset, shared with
    # the batch-update views' "select all N matching current filters"
    # action -- see that function's docstring. The design_installations
    # prefetch here is purely this page's own "Used In Design" column, not
    # a filtering concern, so it's layered on top rather than folded in.
    qs = _filtered_inventory_queryset(request.GET).prefetch_related(
        'design_installations__element__design'
    )

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

    # How many of the currently-filtered instances the batch actions could
    # actually touch -- same group-membership-or-superuser rule
    # _selected_inventory_queryset applies. This, not
    # page_obj.paginator.count, is what "Select all N matching current
    # filters" should offer and count: showing the raw filtered total
    # there would include instances outside the viewer's own groups that
    # a batch action silently can't reach anyway.
    editable_total = qs.count() if request.user.is_superuser else qs.filter(
        owner_group_id__in=user_group_ids
    ).count()

    def _batch_int(name):
        try:
            return int(request.GET.get(name, ''))
        except (TypeError, ValueError):
            return None

    context = {
        'page_obj':       page_obj,
        'q':            q,
        'location':     location,
        'system':       system,
        'group':        group,
        'owner':        owner,
        'design':       design,
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
        # Only components owned by a group this user belongs to (all of
        # them, for a superuser) -- matches the server-side check on POST
        # above, so the dropdown never offers a choice that would just be
        # rejected.
        'components': (
            Component.objects.order_by('name') if request.user.is_superuser
            else Component.objects.filter(owner_group_id__in=user_group_ids).order_by('name')
        ),
        'locations':       Location.objects.select_related('institution').order_by('name'),
        'designs':         Design.objects.order_by('name'),
        'show_add_button': True,
        'user_group_ids':  user_group_ids,
        'editable_total':  editable_total,
        'batch_updated':   _batch_int('batch_updated'),
        'batch_skipped':   _batch_int('batch_skipped'),
        'form_error':      form_error,
        'form_data':       form_data,
        'open_modal':      bool(form_error),
    }
    return render(request, 'cdb/inventory.html', context)


def _inventory_batch_redirect(request, **result):
    """Redirect back to the inventory list, preserving the filters the
    batch form mirrored into hidden fields (so the viewer lands back on
    the same filtered view they acted on, not an unfiltered one) plus a
    batch_updated/batch_skipped summary for inventory_list to render as a
    small result banner. Deliberately resets to page 1 and default sort --
    a bulk edit is likely to have changed which rows even match the
    current sort/location filter, so re-showing whatever page N happened
    to be current is more likely to confuse than help."""
    params = {
        k: request.POST.get(k, '')
        for k in ('q', 'location', 'system', 'group', 'owner', 'design')
        if request.POST.get(k)
    }
    params.update({k: v for k, v in result.items() if v})
    return redirect(f"{reverse('inventory-list')}?{urlencode(params)}" if params
                     else reverse('inventory-list'))


@login_required
def inventory_batch_update_location(request):
    """Batch-move a set of ComponentInstances to a new Location, from the
    checkbox selection (or "Select all N matching current filters") in the
    inventory table's batch action bar. Same group-membership-or-superuser
    authorization as the single-item inventory_update_location, applied
    via _selected_inventory_queryset as a queryset filter instead of a
    single boolean -- a selection that (for a superuser browsing across
    groups) spans instances outside the requester's own groups only ever
    has its authorized subset touched; nothing in the rendered UI can
    produce that for a non-superuser, since it never emits a checkbox for
    a row outside their own groups in the first place.

    Old locations are read before the write so each changed instance still
    gets its own LogEntry ("moved from X to Y"), same as the single-item
    control -- then the actual field write is one UPDATE statement over
    the changed pks rather than N individual .save() calls, since at
    inventory-list scale a batch is exactly the case .save()-per-row was
    never sized for. LogEntry.id has no field-level default (see its
    save() override) -- bulk_create bypasses save(), so the id has to be
    set explicitly on each row here or every one would insert with a
    blank primary key.

    A physical item always has a location (see inventory_update_location),
    so -- same as that single-item control -- this only ever MOVES the
    selection to a different real Location, never clears it. A blank or
    unresolvable target_location is a no-op for the whole batch rather
    than "clear everyone's location," which is what treating a missing
    new_location as "the target is None" would otherwise do."""
    if request.method != 'POST':
        return redirect('inventory-list')

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    qs = _selected_inventory_queryset(request, user_group_ids)

    location_id  = request.POST.get('target_location') or None
    new_location = Location.objects.filter(pk=location_id).first() if location_id else None

    changed = []
    if new_location:
        changed = [
            inst for inst in qs.select_related('location')
            if inst.location_id != new_location.pk
        ]
    if changed:
        LogEntry.objects.bulk_create([
            LogEntry(
                id=str(uuid.uuid4()),
                component_instance=inst,
                topic='inventory',
                logged_by=request.user,
                entry=(
                    f"{inst.tag or inst.pk} moved from "
                    f"{inst.location or 'unassigned'} to {new_location} by "
                    f"{request.user.get_full_name() or request.user.username} (batch update)."
                ),
            )
            for inst in changed
        ])
        ComponentInstance.objects.filter(
            pk__in=[inst.pk for inst in changed]
        ).update(location_id=new_location.pk)

    return _inventory_batch_redirect(request, batch_updated=len(changed))


@login_required
def inventory_batch_update_owner(request):
    """Batch-reassign a set of ComponentInstances' owner_user, from the
    same batch action bar as inventory_batch_update_location. Deliberately
    narrower than that sibling action: owner_group affiliation itself is
    never touched here (out of scope by design, same as the single-item
    inventory_transfer_owner) -- only owner_user moves, and only to a
    target who is a valid owner for the instance being changed: a member
    of THAT instance's own owner_group, or any superuser.

    A selection authorized by _selected_inventory_queryset can still span
    several different owner_groups (possible for a superuser; not
    reachable for a regular user, whose own filtered view can't surface
    instances outside their groups to begin with), and the one target
    chosen in the batch bar may not be a valid owner for every group
    represented. inventory_transfer_owner treats that as "a business-rule
    violation from an otherwise authorized request, not an authorization
    breach" for a single instance, and silently no-ops; here, with many
    instances in play at once, each such case is instead excluded from
    the write and counted as batch_skipped so the result banner can say
    so, rather than going silently missing from what looked like a clean
    batch update."""
    if request.method != 'POST':
        return redirect('inventory-list')

    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    qs = _selected_inventory_queryset(request, user_group_ids)

    new_owner_id = _to_pk_int(request.POST.get('target_owner'))
    new_owner    = User.objects.filter(pk=new_owner_id).first() if new_owner_id else None

    changed = []
    skipped = 0
    if new_owner:
        # Groups new_owner is a valid target for, computed once rather
        # than re-querying per instance -- a superuser is a valid target
        # for every group without needing to actually belong to any of
        # them, same exception as inventory_transfer_owner's
        # valid_target_q.
        new_owner_group_ids = set(new_owner.groups.values_list('id', flat=True))
        for inst in qs.select_related('owner_user', 'owner_group'):
            valid = new_owner.is_superuser or inst.owner_group_id in new_owner_group_ids
            if not valid:
                skipped += 1
            elif inst.owner_user_id != new_owner.pk:
                changed.append(inst)

    if changed:
        LogEntry.objects.bulk_create([
            LogEntry(
                id=str(uuid.uuid4()),
                component_instance=inst,
                topic='inventory',
                logged_by=request.user,
                entry=(
                    f"Ownership of {inst.tag or inst.pk} transferred from "
                    f"{inst.owner_user.get_full_name() or inst.owner_user.username if inst.owner_user else 'unassigned'} "
                    f"to {new_owner.get_full_name() or new_owner.username} by "
                    f"{request.user.get_full_name() or request.user.username} (batch update)."
                ),
            )
            for inst in changed
        ])
        ComponentInstance.objects.filter(
            pk__in=[inst.pk for inst in changed]
        ).update(owner_user=new_owner)

    return _inventory_batch_redirect(request, batch_updated=len(changed), batch_skipped=skipped)


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
    # Group members plus every superuser -- superusers can own (or be
    # assigned) an instance outside their own groups, so they must always
    # be valid transfer targets. The current owner is included even if
    # they fit neither bucket, so the dropdown never silently shows blank
    # for a real, already-set owner (e.g. a superuser who created it).
    owner_choices_q = Q(is_superuser=True)
    if instance.owner_group_id:
        owner_choices_q |= Q(groups=instance.owner_group_id)
    if instance.owner_user_id:
        owner_choices_q |= Q(pk=instance.owner_user_id)
    group_members = User.objects.filter(owner_choices_q).distinct().order_by('username')
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
    Location row with its own correct institution.

    A physical item always has a location -- creation already enforces
    this (component_instance_create, the "Add Inventory Item" form) -- so
    this control only ever MOVES an instance to a different real Location,
    never clears it back to none. The dropdown no longer offers a blank
    "unassign" choice; a location_id that doesn't resolve to a real
    Location (blank, or a stale/tampered value) is simply ignored rather
    than treated as "set it to null," the same authoritative-server-side-
    check pattern used everywhere else in this file."""
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
        new_location = Location.objects.filter(pk=location_id).first() if location_id else None

        if new_location and new_location != old_location:
            instance.location_id = new_location.pk
            instance.save()
            LogEntry.objects.create(
                component_instance=instance,
                topic='inventory',
                logged_by=request.user,
                entry=(
                    f"{instance.tag or instance.pk} moved from "
                    f"{old_location or 'unassigned'} to {new_location} by "
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
    owner_group, or to any superuser, from the "Current Owner" control on
    the instance detail page. Same pattern as component_transfer_owner:
    group members (or any superuser) may initiate a transfer, enforced
    server-side with 403 on an unauthorized POST; a target user outside
    that set (group member or superuser) is a business-rule violation from
    an otherwise authorized user, not an authorization breach, so it's
    silently ignored rather than rejected."""
    instance = get_object_or_404(ComponentInstance, pk=pk)
    user_group_ids = set(request.user.groups.values_list('id', flat=True))
    can_transfer = (
        (bool(instance.owner_group_id) and instance.owner_group_id in user_group_ids)
        or request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_transfer:
            return HttpResponseForbidden("You don't have permission to change this instance's owner.")
        new_owner_id = _to_pk_int(request.POST.get('owner_user'))
        valid_target_q = Q(is_superuser=True)
        if instance.owner_group_id:
            valid_target_q |= Q(groups=instance.owner_group_id)
        if new_owner_id is not None and User.objects.filter(valid_target_q, pk=new_owner_id).exists():
            if instance.owner_user_id != new_owner_id:
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
        usable_templates = DesignTemplate.objects.prefetch_related('elements__child_template').order_by('name')
    else:
        usable_templates = DesignTemplate.objects.filter(
            owner_group_id__in=user_group_ids
        ).prefetch_related('elements__child_template').order_by('name')
    # Annotate each option with completeness so the dropdown can flag one
    # that would just be rejected below -- computed in Python (is_complete
    # isn't a DB column, see the model), which is fine at this table's
    # size. Kept as a plain list rather than re-assigning usable_templates
    # so it's still usable as a queryset elsewhere if that's ever needed.
    usable_templates = list(usable_templates)
    for tpl in usable_templates:
        tpl.is_complete_cached = tpl.is_complete()

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
        elif not template.is_complete():
            form_error = (
                f'"{template.name}" is incomplete -- one or more nested sub-templates '
                f'haven’t been uploaded yet, so it can’t be instantiated. '
                f'See its page for exactly what’s still pending.'
            )
        elif not name:
            form_error = 'Design name is required.'
        elif Design.objects.filter(name=name).exists():
            form_error = f'A design named "{name}" already exists.'
        else:
            # _instantiate_design recurses into any nested (child_template)
            # placeholders, auto-creating their own child Designs the same
            # way -- see its docstring. A depth-exceeded ValidationError is
            # not expected to actually happen (cycle prevention already
            # makes the template tree finite), but treat it as a form
            # error rather than a raw 500 if it somehow does.
            try:
                design = _instantiate_design(template, name, request.user)
            except ValidationError as exc:
                form_error = str(exc.message) if hasattr(exc, 'message') else str(exc)
            else:
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
        group_id    = _to_pk_int(request.POST.get('owner_group'))
        form_data   = {'name': name, 'description': description, 'owner_group': request.POST.get('owner_group', '')}

        allowed_group = group_id is not None and (
            request.user.is_superuser or group_id in user_group_ids
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

    q     = request.GET.get('q', '')
    group = request.GET.get('group', '')
    status = request.GET.get('status', '')
    qs = DesignTemplate.objects.select_related('owner_group', 'owner_user').annotate(
        placeholder_count=Count('elements', distinct=True),
        design_count=Count('designs', distinct=True),
    ).order_by('name')
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
    if group:
        qs = qs.filter(owner_group__name=group)

    # Completeness isn't a DB column (see DesignTemplate.is_complete) --
    # computed here so a stuck pending reference is visible on the list,
    # not just discoverable by opening every template individually.
    # Filtering by it happens in Python for the same reason, over the
    # already-filtered qs; fine at this table's size.
    templates_annotated = list(qs)
    for tpl in templates_annotated:
        tpl.is_complete_cached = tpl.is_complete()
    incomplete_count = sum(1 for tpl in templates_annotated if not tpl.is_complete_cached)
    if status == 'incomplete':
        templates_annotated = [tpl for tpl in templates_annotated if not tpl.is_complete_cached]

    paginator = Paginator(templates_annotated, PAGE_SIZE)
    page_obj  = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj':         page_obj,
        'q':                q,
        'group':            group,
        'status':           status,
        'incomplete_count': incomplete_count,
        'query_str':    _qs(request),
        'groups':       creatable_groups,
        # All groups, for the filter dropdown -- deliberately separate from
        # `groups` above (the "New Template" modal's *creatable* groups,
        # restricted to the acting user's own memberships). Filtering the
        # list is a read, which is unrestricted, so every group is offered
        # here regardless of who's logged in.
        'all_groups':   Group.objects.order_by('name'),
        'form_error':   form_error,
        'form_data':    form_data,
        'open_modal':   bool(form_error),
        'active_page':  'design-templates',
    }
    return render(request, 'cdb/templates_list.html', context)


@login_required
def template_detail(request, pk):
    """Design Template detail page: the placeholder table (read-only),
    the hierarchy breadcrumb, and template metadata (nesting levels,
    product_component, etc).

    The placeholder table is entirely read-only now -- adding, editing
    a quantity, and removing a placeholder (the old template_element_
    update / template_element_delete views) have all been removed.
    Design template hierarchies are defined in YAML and loaded via
    hdb_client / the "hdb load-template" CLI command (see
    client/README.md), so that a git-tracked YAML file is always the
    single source of truth and the database can never quietly drift
    from it -- a per-row quantity edit doesn't survive a re-upload the
    way a re-added placeholder does (get_or_create's defaults= only
    apply on row creation), so letting the web UI touch it independently
    was a silent, permanent fork of the file's numbers.
    This view now only serves GET; a stray POST (an old bookmark, a
    saved form) gets a clean 405 rather than silently doing nothing
    while looking like it worked, or -- worse -- silently succeeding
    the way it used to."""
    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET'])

    template = get_object_or_404(
        DesignTemplate.objects.select_related('owner_group', 'owner_user')
                              .prefetch_related('elements__component', 'elements__child_template'),
        pk=pk,
    )

    # Once at least one Design has been instantiated from a template, the
    # template becomes immutable: those designs were created from a specific
    # bill of placeholders, and letting the template drift afterwards would
    # make "instantiated from BEMC tower" mean different things at different
    # times. Surfaced as a banner below; editing the placeholder table
    # itself is no longer possible from the web UI regardless (see above).
    is_locked = template.designs.exists()
    # A template referenced as another template's child_template placeholder
    # can't be deleted either (child_template is PROTECTed -- see the model)
    # -- surfaced here too so the "Delete Template" button is hidden for the
    # same reason it's hidden for is_locked, rather than only failing on the
    # POST itself.
    is_referenced = template.parent_elements.exists()
    # Deletion is still a live web-UI action, restricted to superusers only
    # (unlike the removed per-row edits, deleting the whole template doesn't
    # let the DB drift from any YAML -- there's nothing left to disagree
    # with the file about).
    can_delete = request.user.is_superuser and not is_locked and not is_referenced

    # Breadcrumb: this template's place in the nesting hierarchy, root
    # first, with one-click navigation to any ancestor -- and, at any
    # slot where more than one template could legally sit (a "diamond"
    # shape, see DesignTemplate.parent_templates), the alternatives too,
    # so the trail never silently hides an ambiguous parent. This is a
    # read-only feature.
    breadcrumb_ancestors = template.breadcrumb_ancestors()

    # Completeness: whether every placeholder anywhere beneath this
    # template resolves to a real Component or a (itself complete) nested
    # DesignTemplate -- see DesignTemplate.is_complete(). YAML uploads are
    # asynchronous, so a template can sit incomplete for a while, waiting
    # on a sub-template someone hasn't uploaded yet; while it does, it
    # can't be instantiated into a Design (enforced in design_list below).
    # pending_placeholders() is just THIS template's own missing names --
    # if is_complete is False but that list is empty, the gap is further
    # down the hierarchy, in a nested sub-template's own placeholders.
    is_complete = template.is_complete()

    # Subsystem fingerprint: a composite hash over this template and
    # everything beneath it, from each one's own source_sha256 -- see
    # DesignTemplate.subsystem_fingerprint()'s docstring for the full
    # rationale and client/README.md's "Provenance and drift detection".
    # None (and nothing shown below) while incomplete -- same reasoning
    # as pending_placeholders() above, there's nothing meaningful to
    # fingerprint yet.
    subsystem_fingerprint = template.subsystem_fingerprint() if is_complete else None

    context = {
        'template':               template,
        'can_delete':             can_delete,
        'is_locked':              is_locked,
        'is_referenced':          is_referenced,
        'breadcrumb_ancestors':   breadcrumb_ancestors,
        'is_complete':            is_complete,
        'pending_placeholders':   template.pending_placeholders() if not is_complete else [],
        'subsystem_fingerprint':  subsystem_fingerprint,
        'designs_from':           template.designs.select_related('owner_user').order_by('name'),
        'active_page':            'design-templates',
    }
    return render(request, 'cdb/template_detail.html', context)


@login_required
def template_delete(request, pk):
    """Delete a Design Template entirely, from the "Delete Template" button
    on its detail page (confirmed client-side first) -- modeled on
    design_delete. Only possible while the template is unlocked, i.e. no
    Design has ever been instantiated from it, AND while no other
    template's placeholder nests this one as its child_template --
    child_template is PROTECTed (see the model) precisely so this can't
    silently orphan another template's placeholder. The button itself is
    hidden in both cases (see template_detail's is_locked/is_referenced),
    and this is the authoritative server-side check, not just a hidden
    control: without it, a direct POST past a PROTECTed reference would
    raise an unhandled ProtectedError instead of failing gracefully.

    Unlike editing a template (open to any owner_group member), deletion is
    superuser-only -- a deliberate, stricter policy than the usual
    OwnedModel write rules, since deleting the template removes it for
    every group member at once, not just the deleter's own view of it.
    Non-superusers get 403, even if they own the template's group."""
    template = get_object_or_404(DesignTemplate, pk=pk)
    can_delete = request.user.is_superuser

    if request.method == 'POST':
        if not can_delete:
            return HttpResponseForbidden("You don't have permission to delete this template.")
        if template.designs.exists() or template.parent_elements.exists():
            # Locked (a design exists based on this template) or referenced
            # as another template's nested sub-template placeholder --
            # either way a business-rule/state check, not an authorization
            # failure: ignore silently rather than 403 or crash, same
            # treatment as the other locked-template POSTs above.
            return redirect('template-detail', pk=template.pk)
        template.delete()
        return redirect('template-list')

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
    """List all technical systems with component and instance counts.
    Optionally filtered down to one responsible group via ?group=<name>,
    same pattern as the Components/Inventory/Users list pages."""
    group = request.GET.get('group', '')

    systems = TechnicalSystem.objects.select_related('group').annotate(
        component_count=Count('components', distinct=True),
        instance_count=Count('components__instances', distinct=True),
    ).order_by('name')
    if group:
        systems = systems.filter(group__name=group)

    context = {
        'systems':     systems,
        'groups':      Group.objects.order_by('name'),
        'group':       group,
        'active_page': 'systems',
    }
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
    # Round-trips a failed "+ Add Location" submission back here (see
    # location_create) -- loc_institution says which institution's modal
    # to reopen, since this page can have one such modal per institution
    # card, not just the single global modal every other list page has.
    context = {
        'institutions':    institutions,
        'active_page':     'institutions',
        'location_types':  Location.LOCATION_TYPES,
        'loc_error':       request.GET.get('loc_error'),
        'loc_institution': request.GET.get('loc_institution', ''),
        'loc_name':        request.GET.get('loc_name', ''),
        'loc_type':        request.GET.get('loc_type', 'room'),
        'loc_parent':      request.GET.get('loc_parent', ''),
        'loc_description': request.GET.get('loc_description', ''),
    }
    return render(request, 'cdb/institutions.html', context)


@login_required
def location_create(request, pk):
    """Create a new Location under this Institution, from the "+ Add
    Location" button in that institution's own panel on the Institutions
    page, and send the user straight to the new location's (empty)
    inventory page -- confirms it exists and is ready to receive items,
    same "go straight to what you just created" convention as
    component_instance_create.

    Institution and Location carry no owner_group of their own to check
    membership against -- unlike Component/ComponentInstance/Design, this
    is shared site infrastructure, not one group's own data -- so, same
    stricter policy as template_delete, creation is superuser-only rather
    than the usual OwnedModel group-membership-or-superuser rule. The
    button is hidden from everyone else; this is the authoritative
    server-side check, not just a hidden control.

    parent is optional and, when given, is required to already be a
    Location of THIS SAME institution -- the model itself doesn't enforce
    that (Location.parent has no such constraint, and full_path() would
    happily walk into a different institution's tree without complaint),
    but the building/room/cabinet/shelf hierarchy this app expects only
    makes sense within one institution, so this form doesn't offer a way
    to create the mismatch even though the schema technically allows it. A
    parent_id that doesn't resolve under this institution (blank, foreign,
    or tampered) is silently treated as "no parent" rather than rejected --
    a business-rule guard, not an authorization one."""
    institution = get_object_or_404(Institution, pk=pk)
    can_create = request.user.is_superuser

    if request.method == 'POST':
        if not can_create:
            return HttpResponseForbidden("You don't have permission to add a location.")
        name          = request.POST.get('name', '').strip()
        location_type = request.POST.get('location_type') or 'room'
        parent_id     = request.POST.get('parent') or None
        description   = request.POST.get('description', '').strip()
        parent = (
            Location.objects.filter(pk=parent_id, institution_id=institution.pk).first()
            if parent_id else None
        )

        if not name:
            params = urlencode({
                'loc_error':       'Name is required.',
                'loc_institution': institution.pk,
                'loc_name':        name,
                'loc_type':        location_type,
                'loc_parent':      parent_id or '',
                'loc_description': description,
            })
            return redirect(f"{reverse('institution-list')}?{params}")

        location = Location.objects.create(
            name=name,
            location_type=location_type,
            institution=institution,
            parent=parent,
            description=description,
        )
        return redirect('location-inventory', pk=location.pk)

    return redirect('institution-list')


@login_required
def location_update_parent(request, pk):
    """Set (or clear) a Room location's parent Building.

    This exists for legacy Room locations that predate location_create --
    the only way to set a parent used to be a direct DB edit, so every
    Location created before this feature existed has parent = NULL even
    where a real building association is known. This is a narrow repair
    tool for that gap, not a general "reparent any location" feature:
    reparenting an arbitrary location (e.g. a building under another
    building, or a room under another room) would need cycle-prevention
    logic this doesn't have, and nothing has asked for that.

    Deliberately scoped to two hard constraints, both enforced here
    server-side (not just hidden in the UI):
      - only a 'room' location's parent can be changed through this view;
      - the new parent, if any, must be a 'building' location in the SAME
        institution as the room.

    Same superuser-only authorization as location_create, for the same
    reason -- Location has no owner_group to check membership against.

    A blank submission and an unresolvable one are handled differently on
    purpose. Blank is the explicit "-- no building --" choice, so it does
    clear an existing parent. A non-blank parent_id that fails to resolve
    to a real Building in this institution (tampered, foreign-institution,
    or stale) is treated as a no-op that leaves the existing parent
    untouched, rather than silently wiping a valid assignment -- the same
    "don't clear on bad input" discipline used for inventory location
    edits."""
    location = get_object_or_404(Location, pk=pk)
    can_edit = request.user.is_superuser

    if request.method == 'POST':
        if not can_edit:
            return HttpResponseForbidden("You don't have permission to edit this location.")

        if location.location_type == 'room':
            parent_id = request.POST.get('parent') or None
            if parent_id:
                new_parent = Location.objects.filter(
                    pk=parent_id,
                    institution_id=location.institution_id,
                    location_type='building',
                ).first()
                if new_parent and new_parent != location.parent:
                    location.parent = new_parent
                    location.save()
                # else: unresolved -- no-op, leave the existing parent untouched.
            elif location.parent is not None:
                # Blank submission is the explicit "-- no building --" choice,
                # not an invalid value -- this one really does mean clear it.
                location.parent = None
                location.save()

    return redirect('institution-list')


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
    q         = request.GET.get('q', '')
    topic     = request.GET.get('topic', '')
    group     = request.GET.get('group', '')
    date_from = request.GET.get('date_from', '')
    date_to   = request.GET.get('date_to', '')

    qs = LogEntry.objects.select_related(
        'logged_by', 'component', 'component_instance', 'design',
    ).order_by('-timestamp')

    if q:
        qs = qs.filter(entry__icontains=q)
    if topic:
        qs = qs.filter(topic=topic)
    # parse_date rejects anything that isn't a real YYYY-MM-DD date, so a
    # malformed or tampered value (the <input type="date"> itself won't
    # submit one, but this is the authoritative server-side check, same
    # pattern as every other filter/form in this file) is simply ignored
    # rather than raising out of the __date__gte/lte lookup. Both bounds
    # are inclusive -- "from X to Y" reads as covering the whole of Y too,
    # matching how someone picking two calendar days on this kind of
    # filter actually expects it to behave.
    parsed_from = parse_date(date_from) if date_from else None
    parsed_to   = parse_date(date_to) if date_to else None
    if parsed_from:
        qs = qs.filter(timestamp__date__gte=parsed_from)
    if parsed_to:
        qs = qs.filter(timestamp__date__lte=parsed_to)
    if group:
        # A LogEntry has no owner_group of its own -- it's whichever of its
        # three optional targets (component / component_instance / design)
        # is actually set, and all three are OwnedModel subclasses sharing
        # the same owner_group field, so this is a uniform 3-way OR rather
        # than three separate filters. An entry whose target has since been
        # deleted (all three FKs null, e.g. via SET_NULL) matches no group
        # and simply won't appear under any group filter -- same as it
        # already not appearing under any of today's other filters.
        qs = qs.filter(
            Q(component__owner_group__name=group)
            | Q(component_instance__owner_group__name=group)
            | Q(design__owner_group__name=group)
        )

    per_page  = _resolve_page_size(request)
    paginator = Paginator(qs, per_page)
    page_obj  = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj':         page_obj,
        'q':                q,
        'topic':            topic,
        'group':            group,
        'date_from':        date_from,
        'date_to':          date_to,
        'today':            timezone.localdate(),
        'topics':           LogEntry.TOPIC_CHOICES,
        'groups':           Group.objects.order_by('name'),
        'per_page':         per_page,
        'per_page_choices': PAGE_SIZE_CHOICES,
        'query_str':        _qs(request),
        'active_page':      'logs',
    }
    return render(request, 'cdb/logs.html', context)
