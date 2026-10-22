import keyring
import pytest
from keyring.backend import KeyringBackend


class MemKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.d = {}

    def get_password(self, s, u):
        return self.d.get((s, u))

    def set_password(self, s, u, p):
        self.d[(s, u)] = p

    def delete_password(self, s, u):
        try:
            del self.d[(s, u)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError(u)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRICA_WEBEX_MCP_HOME", str(tmp_path))
    keyring.set_keyring(MemKeyring())
    from fabrica_webex_mcp import auth
    auth._locks.clear()
