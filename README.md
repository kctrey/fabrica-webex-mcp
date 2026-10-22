# fabrica-webex-mcp

A local [MCP](https://modelcontextprotocol.io) server that lets an AI assistant query, report on and (with
confirmation) change Webex orgs you administer, plus a skill that tells the assistant how to use it well.

- Runs on your machine over stdio. No hosted service; your tokens never leave it.
- Multi-org: register several orgs under aliases and read across all of them (`org="all"`).
- Read-only by default. Writes need `allow_write=true` on the call **and** the org marked writable by you.
- Auth, token refresh, pagination and 429 retry are handled for you.

## Tools

| Tool | Does |
|---|---|
| `list_orgs` | Registered orgs, default, write permission, refresh-token age |
| `webex_request` | Any Webex REST call (`webexapis.com`, plus the CDR analytics host), with field filtering and size caps |
| `people_with_licenses` | People with license IDs resolved to names |
| `workspace_inventory` | Workspaces with location names and calling type |
| `license_assignment_by_location` | Webex Calling Professional / Workspace / Outbound Calling Plan counts per location, computed server-side (one call instead of ~100) |
| `user_registrations` | Live SIP registrations (device, MAC, IPs, expiry) for a user, workspace or virtual line, by name or email, via Xsi-Actions |

The skill (`skill/webex-org/SKILL.md`) adds the workflow rules (field filtering, "describe the change and get a
yes before any write", verify afterwards) and report recipes (org health, license utilization).

## Requirements

- Python 3.11+
- You are an admin of each org you register. The token acts as that admin, so what works depends on your roles.
- Your own Webex Integration (below). Integrations are per-developer; don't share one client secret between people.

## 1. Create an Integration

1. Sign in at <https://developer.webex.com/my-apps> and create an **Integration**.
2. Redirect URI: `http://localhost:8765/callback` (or another port; then pass `--port` to `add-org`).
3. Tick the scopes you plan to use (see `scopes.example.txt`). Anything in your `scopes.txt` must also be
   ticked here, or the login is refused.
4. Copy the Client ID and Client Secret.

Some scopes need roles beyond full admin (for example Compliance Officer for compliance scopes). If your
Integration asks for admin scopes, your org may require approving it in Control Hub.

## 2. Install

```
pipx install git+https://github.com/kctrey/fabrica-webex-mcp
```

or from a checkout: `pip install -e .` (add `[dev]` to run the tests).

## 3. Configure

```
fabrica-webex-mcp setup <client_id>        # prompts for the client secret; stored in the OS keyring
```

Create `scopes.txt` in the config folder from `scopes.example.txt` (see "Where files live").

```
fabrica-webex-mcp add-org myorg            # opens a browser; log in as an admin of that org
fabrica-webex-mcp test-org myorg
fabrica-webex-mcp list-orgs
```

Use a private browser window to log in to a different org. To allow writes for one org:

```
fabrica-webex-mcp set-writable myorg on
```

Other commands: `set-default`, `remove-org`, `add-org <alias> --replace` (re-login), `add-org --refresh-token`
(paste a refresh token instead of a browser login).

## 4. Connect your assistant

The server is the command `fabrica-webex-mcp serve` (stdio). In any MCP client that supports local stdio servers,
add an entry like:

```json
{ "mcpServers": { "fabrica-webex": { "command": "fabrica-webex-mcp", "args": ["serve"] } } }
```

- **Claude Code:** `claude mcp add fabrica-webex -- fabrica-webex-mcp serve`, then copy `skill/webex-org/` to
  `~/.claude/skills/webex-org/`.
- **Other clients (Cursor, etc.):** add the MCP entry in the client's MCP settings. The `SKILL.md` format is
  Claude's; if your client has no skill support, paste its content into the client's rules or instructions file
  (for example `AGENTS.md`). The write guard is enforced by the server either way, but the "ask before you
  write" behaviour comes from those instructions.

## Where files live

| What | Where |
|---|---|
| Org list, `scopes.txt` | `%APPDATA%\fabrica-webex-mcp` on Windows; `$XDG_CONFIG_HOME/fabrica-webex-mcp` or `~/.config/fabrica-webex-mcp` elsewhere. Override with `FABRICA_WEBEX_MCP_HOME`. |
| Client ID/secret, access and refresh tokens | OS keyring (Windows Credential Manager, macOS Keychain, Secret Service) under the service name `fabrica-webex-mcp` |

Nothing secret is written to the config folder or the repo. A refresh token that goes unused for about 90 days
expires; `list_orgs` shows days left, and `add-org <alias> --replace` logs in again.

## Safety

- Tokens are only sent to `webexapis.com`, `analytics.webexapis.com`, and (for registrations) the org's own
  Xsi-Actions endpoint on a `.webex.com` host.
- Writes are refused unless the call sets `allow_write=true` and the org is writable; they cannot target `org="all"`.
- These are real orgs. Test writes on a demo org first.

## Known limits

- The license report finds licenses by name (`Webex Calling - Professional`, `Webex Calling - Workspaces`). If
  your org's plan names differ, those columns can read zero; the report warns when it cannot recognise any
  calling workspace license.
- Outbound Calling Plan status is one lookup per licensed person/workspace and only applies to Cisco Calling
  Plan locations (others show as "not applicable"). Use `include_ocp=false` for a quick report on large orgs.
- Registrations need the `spark-admin:xsi` scope and the subject must be a Webex Calling user, workspace or
  virtual line. Live Xsi-Events subscriptions are deliberately not supported.

## Development

```
pip install -e ".[dev]"
pytest
```

## License

MIT. See `LICENSE`.
