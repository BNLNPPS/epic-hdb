"""
maintenance.py — destructive, superuser-only database maintenance.

Deliberately NOT wired into HDBClient and NOT exposed through the MCP server:
resetting the database is an operator action, reachable only from the CLI
(`hdb nuke-database`), which authenticates the operator with a real
Django username + password first.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from django.apps import apps
from django.contrib.auth import authenticate
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.db import connection


def authenticate_superuser(username: str, password: str) -> User:
    """Verify username/password against Django auth and require an active
    superuser. Raises PermissionError with a deliberately vague message on
    bad credentials, and a specific one for a valid non-superuser."""
    user = authenticate(username=username, password=password)
    if user is None:
        raise PermissionError("Authentication failed.")
    if not (user.is_active and user.is_superuser):
        raise PermissionError(
            f"User {user.username!r} is not a superuser; only superusers "
            f"may reset the database."
        )
    return user


def describe_database() -> dict:
    """Identify the database that would be wiped, and the phrase the
    operator must type to confirm it."""
    s = connection.settings_dict
    vendor = connection.vendor
    if vendor == "sqlite":
        path = Path(str(s["NAME"]))
        label, phrase = str(path), f"NUKE {path.name}"
    else:
        label = f"{s['NAME']} on {s.get('HOST') or 'localhost'}:{s.get('PORT') or ''}"
        phrase = f"NUKE {s['NAME']}@{s.get('HOST') or 'localhost'}"
    return {"vendor": vendor, "label": label, "phrase": phrase,
            "sqlite_path": Path(str(s["NAME"])) if vendor == "sqlite" else None}


def row_counts() -> list[tuple[str, int]]:
    """Non-empty tables that the reset will destroy (hdb app + users/groups)."""
    models = list(apps.get_app_config("hdb").get_models()) + [User, Group]
    out = []
    for m in models:
        n = m.objects.count()
        if n:
            out.append((m._meta.label, n))
    return out


def backup_sqlite(info: dict) -> Path | None:
    """Copy the SQLite file aside before wiping. No-op for other backends."""
    src = info["sqlite_path"]
    if src is None or not src.exists():
        return None
    dst = src.with_name(f"{src.name}.bak-prenuke-{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(src, dst)
    return dst


def reset_database(user: User) -> None:
    """Remove ALL rows from ALL tables (schema and migrations are kept) and
    reset sequences. The acting user is re-checked here so the library call
    is safe on its own, not only via the CLI."""
    if user is None or not (user.is_active and user.is_superuser):
        raise PermissionError("Only an active superuser may reset the database.")
    call_command("flush", interactive=False, verbosity=0)
