"""Secrets in the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service), via keyring.

Entries are kept small and separate because Credential Manager limits blob size.
"""
import json
from dataclasses import dataclass

import keyring

SERVICE = "fabrica-webex-mcp"


@dataclass
class Tokens:
    access: str
    refresh: str
    access_expires_at: float = 0.0
    refresh_expires_at: float = 0.0


def get_client() -> tuple[str, str] | None:
    cid = keyring.get_password(SERVICE, "client_id")
    secret = keyring.get_password(SERVICE, "client_secret")
    return (cid, secret) if cid and secret else None


def set_client(client_id: str, client_secret: str) -> None:
    keyring.set_password(SERVICE, "client_id", client_id)
    keyring.set_password(SERVICE, "client_secret", client_secret)


def get_tokens(alias: str) -> Tokens | None:
    refresh = keyring.get_password(SERVICE, f"org:{alias}:refresh")
    if not refresh:
        return None
    meta = json.loads(keyring.get_password(SERVICE, f"org:{alias}:meta") or "{}")
    return Tokens(
        access=keyring.get_password(SERVICE, f"org:{alias}:access") or "",
        refresh=refresh,
        access_expires_at=meta.get("ae", 0.0),
        refresh_expires_at=meta.get("re", 0.0),
    )


def set_tokens(alias: str, t: Tokens) -> None:
    keyring.set_password(SERVICE, f"org:{alias}:access", t.access)
    keyring.set_password(SERVICE, f"org:{alias}:refresh", t.refresh)
    keyring.set_password(
        SERVICE, f"org:{alias}:meta", json.dumps({"ae": t.access_expires_at, "re": t.refresh_expires_at})
    )


def delete_tokens(alias: str) -> None:
    for part in ("access", "refresh", "meta"):
        try:
            keyring.delete_password(SERVICE, f"org:{alias}:{part}")
        except keyring.errors.PasswordDeleteError:
            pass
