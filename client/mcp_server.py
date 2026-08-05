"""
mcp_server.py — MCP server for the Hardware Database (HDB), user-facing.

Authentication model
---------------------
HTTP Basic Auth, validated against Django's own auth.User table via
django.contrib.auth.authenticate(). There is no separate credential
store: whatever accounts already exist in the HDB database (created via
/admin/, `createsuperuser`, or your own signup flow) are exactly the
accounts that can use this MCP server. Passwords are never handled by
this file beyond passing them straight into Django's authenticate() —
they are checked against Django's existing salted-hash storage and are
never logged, stored, or echoed back.

Every authenticated request is bound to a Django User for its duration,
via a contextvar (see AuthContext below). Every tool builds a
HDBClient(user=...) scoped to that user, so writes are permission-checked
and the ownership hooks in hdb_client/access.py apply uniformly.

Transport
---------
This server only supports Streamable HTTP (not stdio). Basic Auth only
makes sense when there's a request per call to attach credentials to;
stdio is a single trusted local process with no per-call identity, so
it is intentionally not wired up here. If you need a local/dev mode,
use the existing CLI (`client/hdb.py`) directly instead, via
HDBClient(user=None).

NOTE ON LIBRARY VERSIONS
-------------------------
This uses `mcp.server.fastmcp.FastMCP` from the official Python MCP SDK,
and wraps its ASGI app with a Starlette middleware for Basic Auth. The
exact method name FastMCP exposes for the Streamable HTTP ASGI app
(`streamable_http_app()` below) has changed across SDK versions — check
`python -c "from mcp.server.fastmcp import FastMCP; print(dir(FastMCP))"`
against your installed version and adjust the one call site marked below
if it differs.

Setup
-----
    pip install "mcp[cli]" django starlette uvicorn asgiref

    # from the epiHDB project root (next to manage.py):
    DJANGO_SETTINGS_MODULE=hdb_project.settings \
    HDB_PROJECT_ROOT=/path/to/epiHDB \
    HDB_MCP_PUBLIC_HOST=your-tunnel-hostname.trycloudflare.com \
    python client/mcp_server.py
"""
from __future__ import annotations

import base64
import os
import sys
import time
import threading
from contextvars import ContextVar
from typing import Optional

# ---------------------------------------------------------------------
# 1. Django bootstrap — must happen before any Django or hdb_client import
# ---------------------------------------------------------------------
PROJECT_ROOT = os.environ.get("HDB_PROJECT_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hdb_project.settings")

import django  # noqa: E402
import django.conf  # noqa: E402

if not django.conf.settings.configured:
    django.setup()

from django.contrib.auth import authenticate  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from asgiref.sync import sync_to_async  # noqa: E402

from hdb_client import HDBClient  # noqa: E402
from hdb_client.serializers import instance_brief, institution_brief  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402


# ---------------------------------------------------------------------
# 2. Per-request auth context
# ---------------------------------------------------------------------
# Populated by BasicAuthMiddleware before a request reaches the MCP
# handler; read by every tool via require_user(). Every tool body runs
# inside the django_tool() wrapper below, which offloads it to a worker
# thread via asgiref's sync_to_async — and sync_to_async explicitly
# copies the current contextvars.Context into that thread before running
# the function, so _current_user is guaranteed to still be set correctly
# even though the actual Django ORM calls happen off the event loop.
_current_user: ContextVar[Optional[User]] = ContextVar("_current_user", default=None)


def require_user() -> User:
    user = _current_user.get()
    if user is None:
        # Should be unreachable — BasicAuthMiddleware rejects unauthenticated
        # requests before they reach a tool — but tools must not silently
        # fall back to an unscoped HDBClient() if this ever fires.
        raise PermissionError("No authenticated user bound to this request.")
    return user


def scoped_client() -> HDBClient:
    return HDBClient(user=require_user())


def django_tool(fn):
    """
    Wrap a synchronous, Django-ORM-calling tool function so its body
    always runs on a real worker thread via asgiref.sync_to_async,
    regardless of how a given `mcp` SDK version schedules plain `def`
    tool functions.

    Why this exists: Django's ORM refuses a synchronous DB call made
    directly on the event-loop thread and raises exactly
    "You cannot call this from an async context — use a thread or
    sync_to_async." Some `mcp` SDK versions call `def` tools directly on
    the loop instead of offloading them to a thread, which trips that
    check the moment the tool touches the database. Wrapping explicitly
    here removes the dependency on the SDK's (undocumented, version-
    dependent) scheduling behavior entirely.

    thread_sensitive=False is deliberate: each call gets its own
    thread-pool worker rather than being serialized onto a single
    "main thread" — Django's ORM manages its own per-thread connections
    safely, so there's no need to pin all calls to one thread.

    Note on introspection: FastMCP builds each tool's argument schema
    from the function's signature. functools.wraps sets `__wrapped__`
    on the returned async wrapper, and inspect.signature() follows
    `__wrapped__` by default — so FastMCP still sees the original
    parameter names, types, and defaults correctly, even though the
    object it actually registers and awaits is this wrapper.
    """
    import functools

    async_fn = sync_to_async(fn, thread_sensitive=False)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await async_fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------
# 3. Basic Auth middleware
# ---------------------------------------------------------------------
# Minimal in-memory throttle: dev-grade brute-force friction only. It is
# per-process and resets on restart, so it does NOT replace a real
# solution for a production, multi-worker deployment — use django-axes
# (or an equivalent account-lockout / rate-limiting package tied to the
# Django DB) there instead.
_FAILURE_WINDOW_S = 60
_MAX_FAILURES = 5
_LOCKOUT_S = 30
_failures: dict[str, list[float]] = {}
_lockouts: dict[str, float] = {}
_lock = threading.Lock()


def _throttled(key: str) -> bool:
    with _lock:
        until = _lockouts.get(key)
        return bool(until and until > time.time())


def _record_failure(key: str) -> None:
    now = time.time()
    with _lock:
        attempts = [t for t in _failures.get(key, []) if now - t < _FAILURE_WINDOW_S]
        attempts.append(now)
        _failures[key] = attempts
        if len(attempts) >= _MAX_FAILURES:
            _lockouts[key] = now + _LOCKOUT_S
            _failures[key] = []


def _record_success(key: str) -> None:
    with _lock:
        _failures.pop(key, None)
        _lockouts.pop(key, None)


# Paths an MCP client is expected to probe WITHOUT credentials, before it
# knows how (or whether) to authenticate: OAuth Protected Resource Metadata
# (RFC 9728), OAuth Authorization Server Metadata (RFC 8414), and Dynamic
# Client Registration (RFC 7591). This server doesn't implement OAuth, so
# the correct response is Starlette's normal 404 for an undefined route --
# that 404 is precisely how a spec-compliant client learns "no OAuth here,
# fall back to another configured auth method" (in our case, the Request
# headers Basic Auth value set on the connector). Gating these behind Basic
# Auth turns that harmless, expected 404 into a 401, which reads as a fatal
# handshake failure instead of "try something else" -- this was observed
# directly in server logs as the reason a Claude custom connector's
# "Connect" step failed outright rather than using the configured header.
_UNAUTHENTICATED_PATHS = {
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/register",
}


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates HTTP Basic credentials against Django's User model on every
    request, then binds the resulting User for the lifetime of the request
    via the _current_user contextvar. Unauthenticated or invalid
    credentials get a 401 with a WWW-Authenticate challenge and never
    reach the MCP handler or any tool.

    Exception: paths in _UNAUTHENTICATED_PATHS pass straight through to
    the app with no credential check, so OAuth discovery/registration
    probes get a genuine 404 (route not defined) instead of a 401. See the
    comment on _UNAUTHENTICATED_PATHS for why this distinction matters.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _UNAUTHENTICATED_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        auth_header = request.headers.get("authorization", "")

        if not auth_header.lower().startswith("basic "):
            return self._challenge("Missing credentials.")

        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, _, password = decoded.partition(":")
        except Exception:
            return self._challenge("Malformed Authorization header.")

        if not username or not password:
            return self._challenge("Missing username or password.")

        throttle_key = f"{client_ip}:{username}"
        if _throttled(throttle_key):
            return JSONResponse(
                {"error": "Too many failed attempts. Try again shortly."}, status_code=429
            )

        # Django's authenticate() checks the password against the stored
        # salted hash (PBKDF2 by default) — this file never sees or stores
        # a plaintext-comparable credential beyond this single call.
        user = await self._authenticate(username, password)

        if user is None or not user.is_active:
            _record_failure(throttle_key)
            return self._challenge("Invalid username or password.")

        _record_success(throttle_key)

        token = _current_user.set(user)
        try:
            response = await call_next(request)
        finally:
            _current_user.reset(token)
        return response

    @staticmethod
    async def _authenticate(username: str, password: str) -> Optional[User]:
        # authenticate() hits the DB synchronously; Django's ORM is sync
        # by default, so run it off the event loop the same way any other
        # sync Django call in an ASGI app should be run.
        from asgiref.sync import sync_to_async

        return await sync_to_async(authenticate)(username=username, password=password)

    @staticmethod
    def _challenge(detail: str) -> Response:
        return JSONResponse(
            {"error": detail},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="epiHDB"'},
        )


# ---------------------------------------------------------------------
# 4. MCP server + tools
# ---------------------------------------------------------------------
mcp = FastMCP("epiHDB")

# The mcp SDK's own DNS-rebinding protection (separate from
# BasicAuthMiddleware -- this runs inside the SDK's transport layer) only
# trusts Host headers matching localhost/127.0.0.1 by default. That's the
# right default for a server normally reached only locally, but it means
# a public tunnel hostname (ngrok, cloudflared, etc.) gets rejected with
# "Invalid Host header" before a request ever reaches our own auth check.
#
# Since every real request is already gated behind Basic Auth against
# actual Django credentials, DNS-rebinding protection is largely
# redundant for this server specifically -- that protection matters most
# for servers with no other authentication. Rather than disable it
# outright, explicitly trust the current tunnel hostname, read from an
# env var so a new `cloudflared` hostname (issued on every restart,
# since this isn't a named/persistent tunnel) doesn't require editing
# this file each time -- just set the env var before launching:
#
#   HDB_MCP_PUBLIC_HOST=new-vhs-roses-chronicles.trycloudflare.com \
#   HDB_PROJECT_ROOT=/path/to/epiHDB python client/mcp_server.py
_PUBLIC_HOST = os.environ.get("HDB_MCP_PUBLIC_HOST")
_allowed_hosts = ["127.0.0.1:*", "localhost:*"]
_allowed_origins = [
    "http://127.0.0.1:*", "http://localhost:*",
    "https://127.0.0.1:*", "https://localhost:*",
]
if _PUBLIC_HOST:
    _allowed_hosts.append(_PUBLIC_HOST)
    _allowed_origins.append(f"https://{_PUBLIC_HOST}")
else:
    print(
        "WARNING: HDB_MCP_PUBLIC_HOST is not set. Requests arriving "
        "through a public tunnel hostname will be rejected with "
        "'Invalid Host header' by the SDK's DNS-rebinding protection. "
        "Set HDB_MCP_PUBLIC_HOST to your current tunnel hostname "
        "(no scheme, e.g. abc123.trycloudflare.com) before launching.",
        file=sys.stderr,
    )

mcp.settings.transport_security = TransportSecuritySettings(
    allowed_hosts=_allowed_hosts,
    allowed_origins=_allowed_origins,
)


@mcp.tool()
@django_tool
def hdb_whoami() -> dict:
    """Return the authenticated user's username, email, and group memberships."""
    user = require_user()
    return {
        "username": user.username,
        "email": user.email,
        "is_staff": user.is_staff,
        "groups": [g.name for g in user.groups.all()],
    }


@mcp.tool()
@django_tool
def hdb_search(query: str, limit: int = 15) -> dict:
    """
    Cross-domain keyword search over components, physical inventory
    instances, and designs. Returns brief results per domain (id, name,
    and a few identifying fields) — use the *_detail tools for full data
    on a specific hit.
    """
    return scoped_client().search_all(query, limit=limit)


@mcp.tool()
@django_tool
def hdb_where_is(instance_id: str) -> dict:
    """
    Current physical location, institution, and ownership of a single
    inventory item, looked up by its UUID primary key.
    """
    return scoped_client().where_is(instance_id)


@mcp.tool()
@django_tool
def hdb_component_search(query: str, limit: int = 25) -> list[dict]:
    """Search the Component Catalog by name, alternate name, model number, or description."""
    return scoped_client().catalog.search_brief(query, limit=limit)


@mcp.tool()
@django_tool
def hdb_component_summary(component_name: str) -> dict:
    """
    Full detail for a single catalog Component: description, technical
    system, vendors/sources with cost, typed properties, recent log
    entries, and instance count.
    """
    return scoped_client().catalog.summary(component_name)


@mcp.tool()
@django_tool
def hdb_instance_search(query: str, limit: int = 25) -> list[dict]:
    """Search physical inventory instances by tag, serial number, or component name."""
    return scoped_client().inventory.search_brief(query, limit=limit)


@mcp.tool()
@django_tool
def hdb_instance_detail(instance_id: str) -> dict:
    """
    Full detail for a single physical inventory instance, looked up by
    UUID: location, ownership, typed properties, and log history.
    """
    return scoped_client().inventory.detail(instance_id)


@mcp.tool()
@django_tool
def hdb_instances_at_institution(institution_abbreviation: str, limit: int = 25) -> list[dict]:
    """List physical inventory instances currently located at a given institution (e.g. 'BNL')."""
    client = scoped_client()
    return [instance_brief(i) for i in client.inventory.at_institution(institution_abbreviation, limit=limit)]


@mcp.tool()
@django_tool
def hdb_design_search(query: str, limit: int = 25) -> list[dict]:
    """Search the Design Library by name or description."""
    return scoped_client().designs.search_brief(query, limit=limit)


@mcp.tool()
@django_tool
def hdb_design_summary(design_name: str) -> dict:
    """Full detail for a design, including its recursive Bill of Materials."""
    return scoped_client().designs.summary(design_name)


@mcp.tool()
@django_tool
def hdb_design_bom(design_name: str) -> list[dict]:
    """Recursive Bill of Materials for a named design (nested components and sub-designs)."""
    return scoped_client().designs.bom(design_name)


@mcp.tool()
@django_tool
def hdb_list_institutions() -> list[dict]:
    """List all institutions participating in the collaboration (reference data, not scoped)."""
    return [institution_brief(i) for i in scoped_client().locations.all_institutions()]


@mcp.tool()
@django_tool
def hdb_location_tree(institution_abbreviation: str) -> list[dict]:
    """Nested Building -> Room -> Cabinet -> Shelf hierarchy for one institution."""
    return scoped_client().locations.location_tree(institution_abbreviation)


@mcp.tool()
@django_tool
def hdb_systems_overview() -> list[dict]:
    """Technical systems (Tracking, Calorimetry, ...) with component and instance counts."""
    return scoped_client().systems.instance_counts()


@mcp.tool()
@django_tool
def hdb_create_instance(
    component_name: str,
    tag: str = "",
    serial_number: str = "",
    description: str = "",
    location_name: Optional[str] = None,
    owner_group_name: Optional[str] = None,
) -> dict:
    """
    Create a new physical inventory instance of an existing catalog
    Component. The instance is always owned by the authenticated caller;
    owner_group_name must name a group the caller actually belongs to
    (write access to that record is then governed by group_writeable,
    which is set automatically when an owner_group is supplied).
    """
    return scoped_client().inventory.create(
        component_name=component_name,
        tag=tag,
        serial_number=serial_number,
        description=description,
        location_name=location_name,
        owner_group_name=owner_group_name,
    )


# ---------------------------------------------------------------------
# 5. ASGI app: MCP Streamable HTTP wrapped in Basic Auth
# ---------------------------------------------------------------------
# `streamable_http_app()` is the FastMCP method name as of the current
# `mcp` SDK release at time of writing. Verify against your installed
# version — this is the one line most likely to need adjusting after
# `pip install`.
#
# IMPORTANT: attach the middleware to the app FastMCP already built —
# do NOT reconstruct a new Starlette app from `mcp_asgi_app.routes`.
# The returned app's *lifespan* starts the Streamable HTTP session
# manager (the background task group that tracks in-flight MCP
# sessions/streams); copying only `.routes` into a fresh Starlette()
# instance silently drops that lifespan, so the session manager never
# starts and the first real MCP request (`initialize`) 500s. Requests
# that get rejected at 401 (wrong/missing credentials) never reach that
# code path, so this bug is invisible to the auth-boundary checks and
# only shows up once a request actually authenticates.
app = mcp.streamable_http_app()
app.add_middleware(BasicAuthMiddleware)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
