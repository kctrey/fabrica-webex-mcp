"""Browser login (OAuth authorization-code flow) with a one-shot localhost listener."""
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from . import config, store

AUTH_URL = "https://webexapis.com/v1/authorize"
DEFAULT_PORT = 8765
# Hidden behind a Cisco-controlled toggle and unavailable to most orgs: Webex rejects the whole login if the
# Integration requests a scope the org has not been approved for, so it is only requested on --with-xsi.
XSI_SCOPE = "spark-admin:xsi"


def scopes_path():
    return config.home() / "scopes.txt"


def load_scopes(with_xsi: bool = False) -> str:
    p = scopes_path()
    if not p.exists():
        raise config.ConfigError(
            f"No scope list at {p}. Create it with one scope per line (the scopes registered on your Integration)."
        )
    lines = [l.strip() for l in p.read_text(encoding="utf-8-sig").splitlines()]
    wanted = [l for l in lines if l and not l.startswith("#") and l != XSI_SCOPE]
    return " ".join(dict.fromkeys(wanted + ([XSI_SCOPE] if with_xsi else [])))


def authorize_url(client_id: str, redirect_uri: str, scope: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode(
        {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
         "scope": scope, "state": state}
    )


def wait_for_code(port: int, state: str, timeout: float = 300) -> str:
    """Listen on 127.0.0.1 only; return the auth code once a callback with the right state arrives."""
    result: dict = {}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlsplit(self.path)
            if u.path != "/callback":
                self.send_response(404); self.end_headers(); return
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if q.get("state") != state:
                self.send_response(400); self.end_headers(); self.wfile.write(b"State mismatch."); return
            result.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Webex login complete. You can close this tab.")
            done.set()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", port), H)
    srv.timeout = 1
    deadline = threading.Timer(timeout, done.set)
    deadline.start()
    try:
        while not done.is_set():
            srv.handle_request()
    finally:
        deadline.cancel()
        srv.server_close()
    if "code" not in result:
        why = result.get("error_description") or result.get("error") or "no callback received"
        hint = (f" A requested scope was rejected. If you used --with-xsi, {XSI_SCOPE} needs Cisco approval for "
                f"your org; re-run add-org without it." if result.get("error") == "invalid_scope" else "")
        raise TimeoutError(f"Login failed or timed out: {why}.{hint}")
    return result["code"]


def run_login(port: int = DEFAULT_PORT, with_xsi: bool = False) -> tuple[str, str]:
    """Returns (code, redirect_uri)."""
    client = store.get_client()
    if not client:
        raise config.ConfigError("Run `fabrica-webex-mcp setup <client_id>` first.")
    redirect = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(24)
    scope = load_scopes(with_xsi)
    if with_xsi:
        print(f"Requesting {XSI_SCOPE}, which Cisco must have approved for your org. If Webex shows an invalid-scope "
              "error, press Ctrl+C and re-run add-org without --with-xsi.")
    else:
        print(f"Not requesting {XSI_SCOPE}: the user_registrations tool will not work for this org "
              "(add --with-xsi if Cisco has enabled it for you).")
    url = authorize_url(client[0], redirect, scope, state)
    print("Opening browser. Log in as an admin of the org you want to add (use a private window for a different org).")
    print(f"If it doesn't open, visit:\n{url}\n")
    webbrowser.open(url)
    return wait_for_code(port, state), redirect
