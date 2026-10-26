"""Non-secret org registry, stored outside the repo (override with FABRICA_WEBEX_MCP_HOME)."""
import json
import os
from pathlib import Path


class ConfigError(Exception):
    pass


def home() -> Path:
    """FABRICA_WEBEX_MCP_HOME, else %APPDATA%\\fabrica-webex-mcp on Windows, else $XDG_CONFIG_HOME (or ~/.config)/fabrica-webex-mcp."""
    override = os.environ.get("FABRICA_WEBEX_MCP_HOME")
    if override:
        base = Path(override)
    elif os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"]) / "fabrica-webex-mcp"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "fabrica-webex-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _path() -> Path:
    return home() / "orgs.json"


def load() -> dict:
    p = _path()
    if not p.exists():
        return {"default": None, "orgs": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save(reg: dict) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def get_org(alias: str) -> dict:
    orgs = load()["orgs"]
    if alias not in orgs:
        known = ", ".join(sorted(orgs)) or "(none - run `fabrica-webex-mcp add-org`)"
        raise ConfigError(f"Unknown org '{alias}'. Known orgs: {known}")
    return orgs[alias]


def token_alias(alias: str) -> str:
    """The alias whose tokens authenticate `alias`: a managed org borrows its parent's ("via")."""
    return load()["orgs"].get(alias, {}).get("via") or alias


def managed_org_id(alias: str) -> str | None:
    """The target orgId to send on every call if `alias` is a managed org, else None."""
    o = load()["orgs"].get(alias, {})
    return o.get("org_id") if o.get("via") else None


def resolve(alias: str | None) -> str:
    """Return a concrete alias, falling back to the default org."""
    reg = load()
    alias = alias or reg.get("default")
    if not alias:
        known = ", ".join(sorted(reg["orgs"])) or "(none)"
        raise ConfigError(f"No org given and no default set. Known orgs: {known}")
    get_org(alias)
    return alias


def all_aliases() -> list[str]:
    return sorted(load()["orgs"])
