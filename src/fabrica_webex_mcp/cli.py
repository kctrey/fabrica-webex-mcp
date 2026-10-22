"""Admin CLI: fabrica-webex-mcp setup | add-org | list-orgs | test-org | remove-org | set-writable | set-default | serve"""
import argparse
import asyncio
import getpass
import sys

import httpx

from . import auth, config, login, store
from .client import WebexClient


def cmd_setup(a) -> None:
    secret = getpass.getpass("Integration client secret: ")
    store.set_client(a.client_id, secret)
    print("Stored client credentials in the OS keyring (Windows Credential Manager, macOS Keychain or Secret Service).")


async def _acquire(alias: str, refresh_token: str | None, code: str | None, redirect: str | None) -> dict:
    """Get tokens (from a login code or a pasted refresh token), then identify the org and admin."""
    async with httpx.AsyncClient(timeout=30) as http:
        if code:
            await auth.exchange_code(alias, code, redirect, http)
        else:
            await auth.refresh(alias, refresh_token, http)
        c = WebexClient(http)
        me = (await c.request(alias, "GET", "/people/me"))["data"]
        name = None
        try:
            name = (await c.request(alias, "GET", f"/organizations/{me['orgId']}"))["data"].get("displayName")
        except Exception:
            pass  # org name is cosmetic
    return {"org_id": me.get("orgId"), "name": name, "admin": me.get("emails", ["?"])[0]}


def cmd_add_org(a) -> None:
    reg = config.load()
    if a.alias in reg["orgs"] and not a.replace:
        sys.exit(f"Org '{a.alias}' already exists. Use --replace to overwrite its tokens.")
    if not store.get_client():
        sys.exit("Run `fabrica-webex-mcp setup <client_id>` first.")
    code = redirect = refresh_token = None
    if a.refresh_token:
        refresh_token = getpass.getpass(f"Refresh token for '{a.alias}': ").strip()
    else:
        code, redirect = login.run_login(a.port)
    old = store.get_tokens(a.alias)
    try:
        org = asyncio.run(_acquire(a.alias, refresh_token, code, redirect))
    except Exception as e:
        # don't leave half-saved tokens behind from a failed attempt
        store.set_tokens(a.alias, old) if old else store.delete_tokens(a.alias)
        sys.exit(f"Validation failed: {e}")
    if org["org_id"] in {o.get("org_id") for k, o in reg["orgs"].items() if k != a.alias}:
        print("Warning: another alias already points at this org.")
    reg["orgs"][a.alias] = {
        "org_id": org["org_id"], "name": org["name"], "admin": org["admin"],
        "writable": reg["orgs"].get(a.alias, {}).get("writable", False),
    }
    reg["default"] = reg.get("default") or a.alias
    config.save(reg)
    print(f"Added '{a.alias}': {org['name']} ({org['org_id']}) as {org['admin']}. Read-only; "
          f"enable writes with `fabrica-webex-mcp set-writable {a.alias} on`.")


def cmd_list_orgs(a) -> None:
    reg = config.load()
    for alias, o in sorted(reg["orgs"].items()):
        mark = "*" if alias == reg.get("default") else " "
        print(f"{mark} {alias:15} {o.get('name')}  writable={o.get('writable', False)}  tokens={'yes' if store.get_tokens(alias) else 'NO'}")


def cmd_test_org(a) -> None:
    alias = config.resolve(a.alias)

    async def go():
        async with httpx.AsyncClient(timeout=30) as http:
            return await WebexClient(http).request(alias, "GET", "/organizations", max_items=1)
    r = asyncio.run(go())
    print(f"OK: {alias} -> {r['items'][0].get('displayName') if r['items'] else '?'}")


def cmd_remove_org(a) -> None:
    reg = config.load()
    reg["orgs"].pop(a.alias, None)
    if reg.get("default") == a.alias:
        reg["default"] = next(iter(sorted(reg["orgs"])), None)
    config.save(reg)
    store.delete_tokens(a.alias)
    print(f"Removed '{a.alias}'.")


def cmd_set_writable(a) -> None:
    reg = config.load()
    config.get_org(a.alias)
    reg["orgs"][a.alias]["writable"] = a.state == "on"
    config.save(reg)
    print(f"{a.alias}: writable={a.state == 'on'}")


def cmd_set_default(a) -> None:
    reg = config.load()
    config.get_org(a.alias)
    reg["default"] = a.alias
    config.save(reg)


def cmd_serve(a) -> None:
    from .server import run
    run()


def main() -> None:
    p = argparse.ArgumentParser(prog="fabrica-webex-mcp")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="store Integration client id + secret"); s.add_argument("client_id"); s.set_defaults(f=cmd_setup)
    s = sub.add_parser("add-org", help="register an org via browser login (admin of that org)")
    s.add_argument("alias"); s.add_argument("--replace", action="store_true")
    s.add_argument("--port", type=int, default=login.DEFAULT_PORT)
    s.add_argument("--refresh-token", action="store_true", help="paste a refresh token instead of logging in")
    s.set_defaults(f=cmd_add_org)
    sub.add_parser("list-orgs").set_defaults(f=cmd_list_orgs)
    s = sub.add_parser("test-org"); s.add_argument("alias", nargs="?"); s.set_defaults(f=cmd_test_org)
    s = sub.add_parser("remove-org"); s.add_argument("alias"); s.set_defaults(f=cmd_remove_org)
    s = sub.add_parser("set-writable"); s.add_argument("alias"); s.add_argument("state", choices=["on", "off"]); s.set_defaults(f=cmd_set_writable)
    s = sub.add_parser("set-default"); s.add_argument("alias"); s.set_defaults(f=cmd_set_default)
    sub.add_parser("serve", help="run the stdio MCP server").set_defaults(f=cmd_serve)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
