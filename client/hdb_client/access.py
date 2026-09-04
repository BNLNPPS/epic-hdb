"""
access.py — Permission and visibility scoping for the Hardware Database.

Every domain object that inherits OwnedModel (Component, ComponentInstance,
Design) carries three fields relevant to access control:

    owner_user       FK -> auth.User    (nullable)
    owner_group      FK -> auth.Group   (nullable — Django's built-in Group,
                                          confirmed via `from django.contrib
                                          .auth.models import User, Group`
                                          in hdb/models.py)
    group_writeable  bool               (only meaningful together with owner_group)

POLICY (explicit design decision — tighten if it doesn't match your needs):

  READ  : all authenticated users can read all catalog/inventory/design
          rows. epic-hdb is a shared collaboration database spanning many
          institutions, and the schema has no per-row read-privacy flag.
          If you need per-institution or per-group read isolation, add
          the filter in `visible_to()` below — the hook is already wired
          into every domain client's queryset method.

  WRITE : a user may create/modify/delete a row if any of:
            - they are its owner_user
            - they are Django staff/superuser
            - they are a member of owner_group AND group_writeable is True
          A user creating a *new* row may only set owner_user to
          themselves, and owner_group to a Group they actually belong to
          (see resolve_owner_group_for_create). Nothing under caller
          control can assign ownership to someone else — this closes the
          hole in the original CLI's `create-instance --owner <anyone>`.
"""
from __future__ import annotations

from django.contrib.auth.models import User, Group


def visible_to(queryset, user: User | None):
    """
    Apply read-visibility scoping to a queryset of an OwnedModel subclass.
    Currently a no-op per the READ policy above — kept as an explicit hook
    so scoping can be tightened later without touching every call site,
    e.g.:

        if user is not None and not (user.is_staff or user.is_superuser):
            queryset = queryset.filter(
                Q(owner_group__in=user.groups.all()) | Q(owner_group__isnull=True)
            )
    """
    return queryset


def can_write(obj, user: User | None) -> bool:
    """Return True if `user` may create/modify/delete `obj` (an OwnedModel instance)."""
    if user is None:
        return False
    if user.is_superuser or user.is_staff:
        return True
    if obj.owner_user_id and obj.owner_user_id == user.id:
        return True
    if obj.owner_group_id and obj.group_writeable:
        return user.groups.filter(pk=obj.owner_group_id).exists()
    return False


def assert_can_write(obj, user: User | None) -> None:
    if not can_write(obj, user):
        who = user.username if user else "<anonymous>"
        raise PermissionError(
            f"User {who!r} does not have write access to "
            f"{obj.__class__.__name__} {getattr(obj, 'pk', '?')!r}."
        )


def resolve_owner_group_for_create(group_name: str | None, user: User) -> Group | None:
    """
    Resolve a caller-supplied owner_group name into a Group instance for use
    on a *new* record — but only if `user` actually belongs to that group
    (or is staff/superuser). This is what prevents an authenticated user
    from creating inventory "owned" by a group they have no connection to.
    """
    if not group_name:
        return None
    try:
        group = Group.objects.get(name=group_name)
    except Group.DoesNotExist:
        raise ValueError(f"Group not found: {group_name!r}")
    if not (user.is_staff or user.is_superuser or user.groups.filter(pk=group.pk).exists()):
        raise PermissionError(
            f"User {user.username!r} is not a member of group {group_name!r} "
            f"and cannot create records owned by it."
        )
    return group
