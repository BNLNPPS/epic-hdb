#!/usr/bin/env python3
"""
smoke_test.py — end-to-end check of mcp_server.py against a running
instance, using the real epic-hdb seed data (`python manage.py seed_hdb`).

Covers two layers:

  1. The AUTH BOUNDARY (raw HTTP, no MCP protocol needed):
       - no credentials            -> 401
       - wrong password            -> 401
       - too many wrong passwords  -> 429 (in-memory throttle)

  2. The MCP TOOL LAYER, authenticated as the seed user `crafts`
     (password `crafts`, member of group `BEMC`):
       - session handshake + tool listing
       - each read tool, against real seed records

  mcp_server.py exposes read-only tools only (no create/update/delete),
  so there is no write path to exercise here.

Usage
-----
    # 1. In one terminal, from the epic-hdb project root:
    python manage.py seed_hdb          # idempotent, safe to re-run
    HDB_PROJECT_ROOT=$(pwd) python client/mcp_server.py

    # 2. In another terminal:
    pip install httpx "mcp[cli]"
    python smoke_test.py
    python smoke_test.py --base-url http://127.0.0.1:8001 --username gnigmat --password gnigmat

NOTE ON LIBRARY VERSIONS
-------------------------
Section 2 uses `mcp.client.streamable_http.streamablehttp_client`, whose
exact import path / call signature has shifted across `mcp` SDK releases
(same caveat as `mcp_server.py`'s `streamable_http_app()`). If the import
at the top of run_mcp_checks() fails, check
`python -c "import mcp.client.streamable_http as m; print(dir(m))"`
against your installed version. Section 1 (the auth boundary) has no such
dependency — it's plain HTTP — so it will run and give you a real signal
even if the MCP client import needs adjusting.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid

import httpx

PASS = "PASS"
FAIL = "FAIL"
_results: list[tuple[str, str, str]] = []  # (status, name, detail)


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    _results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == FAIL else ""))
    return condition


# ---------------------------------------------------------------------
# 1. Auth boundary — plain HTTP, no MCP protocol needed
# ---------------------------------------------------------------------
def run_auth_boundary_checks(base_url: str, wrong_password: str) -> None:
    """
    Every check here uses a synthetic, guaranteed-not-real probe username —
    including the one that deliberately trips the lockout — never the
    account under test in section 2.

    Why: mcp_server.py's throttle is keyed on (client_ip, username)
    regardless of whether a given attempt's password was right or wrong.
    Tripping the lockout against a *real* username would then make the
    correct-password login in section 2 fail with 429 too, since a locked
    account is rejected before its password is even checked. Using a
    probe username that can't collide with any real account keeps this
    section's failure-injection from ever touching a real login.
    """
    print("\n== 1. Auth boundary (raw HTTP) ==")
    mcp_url = f"{base_url.rstrip('/')}/mcp/"
    probe_username = f"smoketest-probe-{uuid.uuid4().hex[:8]}"

    with httpx.Client(timeout=10) as client:
        r = client.post(mcp_url, json={"jsonrpc": "2.0", "method": "ping", "id": 1})
        check("no credentials -> 401", r.status_code == 401, f"got {r.status_code}")
        check(
            "no credentials -> WWW-Authenticate header present",
            "www-authenticate" in {k.lower() for k in r.headers},
        )

        r = client.post(mcp_url, json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                         auth=(probe_username, wrong_password))
        check("wrong password -> 401", r.status_code == 401, f"got {r.status_code}")

        # Trip the in-memory throttle (_MAX_FAILURES = 5 in mcp_server.py).
        # Uses probe_username throughout, so this can never lock out a real
        # account — see the docstring above.
        last_status = None
        for _ in range(6):
            r = client.post(mcp_url, json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                             auth=(probe_username, wrong_password))
            last_status = r.status_code
        check("repeated wrong passwords -> 429 lockout", last_status == 429,
              f"final attempt got {last_status}")


# ---------------------------------------------------------------------
# 2. Tool layer — via the official MCP client SDK
# ---------------------------------------------------------------------
def run_mcp_checks(base_url: str, username: str, password: str) -> None:
    print("\n== 2. MCP tool layer (authenticated as %r) ==" % username)
    try:
        import anyio
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:
        print(f"[SKIP] MCP client SDK not importable as expected ({exc}). "
              f"See the NOTE ON LIBRARY VERSIONS at the top of this file.")
        return

    mcp_url = f"{base_url.rstrip('/')}/mcp/"
    creds = httpx.BasicAuth(username, password)

    async def _run():
        async with streamablehttp_client(mcp_url, auth=creds) as (read, write, _get_sid):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # -- tool discovery --------------------------------------
                tools = (await session.list_tools()).tools
                names = {t.name for t in tools}
                expected = {
                    "hdb_whoami", "hdb_search", "hdb_where_is",
                    "hdb_component_search", "hdb_component_summary",
                    "hdb_instance_search", "hdb_instance_detail",
                    "hdb_instances_at_institution", "hdb_design_search",
                    "hdb_design_summary", "hdb_design_bom",
                    "hdb_list_institutions", "hdb_location_tree",
                    "hdb_systems_overview",
                }
                check("no write tool is registered",
                      "hdb_create_instance" not in names and
                      not any(n.startswith(("hdb_create", "hdb_update", "hdb_delete")) for n in names),
                      f"got tool names: {sorted(names)}")
                check("all expected tools registered", expected <= names,
                      f"missing: {expected - names}")

                # Tools whose declared return type is a bare list, not a
                # dict. Needed because this SDK version has been observed
                # to represent such a result three different ways:
                #   (a) one content block holding a single JSON array,
                #   (b) one content block PER LIST ITEM (each a bare JSON
                #       object) — reading only content[0] here silently
                #       keeps just one item and discards the rest,
                #   (c) one content block holding {"result": [...]}.
                # Normalize all three into an actual Python list rather
                # than assuming any single one of these shapes.
                LIST_TOOLS = {
                    "hdb_component_search", "hdb_instance_search", "hdb_design_search",
                    "hdb_instances_at_institution", "hdb_list_institutions",
                    "hdb_location_tree", "hdb_systems_overview", "hdb_design_bom",
                }

                async def call(tool_name: str, **kwargs):
                    result = await session.call_tool(tool_name, kwargs)
                    if result.isError:
                        text = result.content[0].text if result.content else ""
                        raise RuntimeError(f"{tool_name}: {text}")

                    if tool_name in LIST_TOOLS:
                        items = []
                        for block in result.content:
                            parsed = json.loads(block.text)
                            if isinstance(parsed, list):
                                items.extend(parsed)
                            elif isinstance(parsed, dict) and set(parsed.keys()) == {"result"}:
                                items.extend(parsed["result"])
                            else:
                                items.append(parsed)
                        return items

                    # Dict-returning tools: a single content block, one
                    # JSON object -- content[0] is the canonical source;
                    # structuredContent is a fallback (not always populated
                    # depending on SDK version and return annotation).
                    if result.content:
                        parsed = json.loads(result.content[0].text)
                    elif result.structuredContent is not None:
                        parsed = result.structuredContent
                    else:
                        return None
                    if isinstance(parsed, dict) and set(parsed.keys()) == {"result"}:
                        parsed = parsed["result"]
                    return parsed

                # -- identity ---------------------------------------------
                who = await call("hdb_whoami")
                check("hdb_whoami reports correct username",
                      isinstance(who, dict) and who.get("username") == username, f"got {who}")
                check("hdb_whoami reports BEMC group membership",
                      isinstance(who, dict) and "BEMC" in who.get("groups", []), f"got {who}")

                # -- reads against real seed data --------------------------
                search = await call("hdb_search", query="Crystal")
                comp_names = ({c["name"] for c in search.get("components", [])}
                              if isinstance(search, dict) else set())
                check("hdb_search('Crystal') finds PbWO4 Crystal",
                      "PbWO4 Crystal" in comp_names, f"got {comp_names}")

                summary = await call("hdb_component_summary", component_name="PbWO4 Crystal")
                check("hdb_component_summary returns instance_count >= 1",
                      isinstance(summary, dict) and summary.get("instance_count", 0) >= 1,
                      f"got {summary}")

                inst_hits = await call("hdb_instance_search", query="BEMC-CRYSTAL")
                have_inst_hits = check(
                    "hdb_instance_search finds a BEMC-CRYSTAL instance",
                    isinstance(inst_hits, list) and len(inst_hits) >= 1, f"got {inst_hits}",
                )
                inst_id = inst_hits[0]["id"] if have_inst_hits else None

                if inst_id:
                    detail = await call("hdb_instance_detail", instance_id=inst_id)
                    check("hdb_instance_detail matches searched instance",
                          isinstance(detail, dict) and detail.get("component") == "PbWO4 Crystal",
                          f"got {detail}")

                    where = await call("hdb_where_is", instance_id=inst_id)
                    check("hdb_where_is reports an institution",
                          isinstance(where, dict) and bool(where.get("institution")), f"got {where}")
                else:
                    check("hdb_instance_detail matches searched instance", False,
                          "skipped: no instance id from previous search")
                    check("hdb_where_is reports an institution", False,
                          "skipped: no instance id from previous search")

                at_cua = await call("hdb_instances_at_institution", institution_abbreviation="CUA")
                check("hdb_instances_at_institution('CUA') returns a list",
                      isinstance(at_cua, list), f"got {at_cua}")

                designs = await call("hdb_design_search", query="BEMC")
                have_designs = check("hdb_design_search returns a list",
                                      isinstance(designs, list), f"got {designs}")
                design_names = {d["name"] for d in designs} if have_designs else set()
                check("hdb_design_search('BEMC') finds BEMC tower",
                      "BEMC tower" in design_names, f"got {design_names}")

                bom = await call("hdb_design_bom", design_name="BEMC tower")
                have_bom = check("hdb_design_bom returns a list", isinstance(bom, list), f"got {bom}")
                bom_elements = {row["element"] for row in bom} if have_bom else set()
                check("hdb_design_bom('BEMC tower') has Crystal + SiPM elements",
                      {"Crystal", "SiPM"} <= bom_elements, f"got {bom_elements}")

                insts = await call("hdb_list_institutions")
                have_insts = check("hdb_list_institutions returns a list",
                                    isinstance(insts, list), f"got {insts}")
                inst_abbrs = {i["abbreviation"] for i in insts} if have_insts else set()
                check("hdb_list_institutions includes seed institutions",
                      {"CUA", "UIC", "BNL", "UH"} <= inst_abbrs, f"got {inst_abbrs}")

                tree = await call("hdb_location_tree", institution_abbreviation="CUA")
                check("hdb_location_tree('CUA') returns a non-empty list",
                      isinstance(tree, list) and len(tree) > 0, f"got {tree}")

                systems = await call("hdb_systems_overview")
                check("hdb_systems_overview returns a list", isinstance(systems, list), f"got {systems}")

    anyio.run(_run)


# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8001")
    ap.add_argument("--username", default="crafts")
    ap.add_argument("--password", default="crafts")
    ap.add_argument("--wrong-password", default="definitely-not-it")
    args = ap.parse_args()

    run_auth_boundary_checks(args.base_url, args.wrong_password)
    run_mcp_checks(args.base_url, args.username, args.password)

    print("\n== Summary ==")
    failed = [r for r in _results if r[0] == FAIL]
    print(f"{len(_results) - len(failed)}/{len(_results)} checks passed.")
    if failed:
        print("Failures:")
        for status, name, detail in failed:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
