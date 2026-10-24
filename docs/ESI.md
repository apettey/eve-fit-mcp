# ESI setup: real character skills (and in-game fittings)

> **Using eve-fit together with [eve-esi-mcp](https://github.com/apettey/eve-esi-mcp)?** Then eve-esi owns the EVE login and passes skills and implants to eve-fit (see [INTEGRATION.md](INTEGRATION.md)), so you don't need anything on this page. These `esi_*` tools are a **standalone fallback** for using eve-fit on its own. Don't log the same character into both with the same EVE app, because EVE rotates refresh tokens.

eve-fit can log in to EVE Online through EVE's SSO, import a character's trained skills as a **pilot**,
and read the fittings saved in-game. Fits calculated with that pilot use your real skills, and the
optimiser only picks modules and ammo the character can use.

Only read access is requested:

| Scope | Used for |
|---|---|
| `esi-skills.read_skills.v1` | importing trained skill levels |
| `esi-fittings.read_fittings.v1` | `esi_list_fittings` / `esi_import_fitting` |

## Option A: your own EVE application (recommended)

Your own app means the EVE login screen names *your* application, you control its scopes, and you don't
depend on Pyfa's hosted callback page.

### 1. Register the application

1. Go to <https://developers.eveonline.com/> and sign in with your EVE account.
2. **Create new application**:
   - **Name / description**: anything, e.g. `eve-fit (personal)`.
   - **Connection type**: *Authentication & API Access*.
   - **Permissions (scopes)**: `esi-skills.read_skills.v1` and `esi-fittings.read_fittings.v1`.
   - **Callback URL**: `http://localhost:8765/callback`
3. Save, then open the application and note the **Client ID** and **Secret Key**.

The callback must match **exactly** (scheme, host, port, path) what eve-fit uses. To use a different
port, register e.g. `http://localhost:9123/callback` and set `EVE_ESI_CALLBACK_URL` to the same value.

### 2. Give eve-fit the credentials

eve-fit reads three environment variables:

| Variable | Required | Value |
|---|---|---|
| `EVE_ESI_CLIENT_ID` | yes | the Client ID |
| `EVE_ESI_CLIENT_SECRET` | recommended | the Secret Key |
| `EVE_ESI_CALLBACK_URL` | no | defaults to `http://localhost:8765/callback` |

Put them in the MCP server's registration, **not in this repository**. For Claude Code (user scope),
edit `~/.claude.json` (Windows: `C:\Users\<you>\.claude.json`) and add an `env` block to the `eve-fit`
server:

```json
"eve-fit": {
  "type": "stdio",
  "command": "<path to eve-fit-mcp>\\.venv\\Scripts\\python.exe",
  "args": ["-m", "eve_fit_mcp.server"],
  "env": {
    "EVE_ESI_CLIENT_ID": "<client id>",
    "EVE_ESI_CLIENT_SECRET": "<secret key>"
  }
}
```

Or re-register with the CLI:

```powershell
claude mcp remove --scope user eve-fit
claude mcp add --scope user eve-fit -e EVE_ESI_CLIENT_ID=<client id> -e EVE_ESI_CLIENT_SECRET=<secret key> -- "<path to eve-fit-mcp>\.venv\Scripts\python.exe" -m eve_fit_mcp.server
```

Restart Claude Code (or start a new session) so the server picks up the environment. `esi_app_info`
confirms which app is active (`"app": "own"`) without revealing the secret.

> Treat the secret like a password. Never commit it, and don't paste it into chats or issues. If it
> leaks, regenerate it on the developer portal and update the env var.

### 3. Log in

Ask Claude to run `esi_login` (optionally with a pilot name). It returns an EVE login URL:

1. Open the URL, log in, choose the character, and approve the scopes.
2. EVE redirects to `http://localhost:8765/callback`; eve-fit exchanges the code, imports the skills,
   and the browser shows a blank "success" response.
3. `esi_login_status` shows `done` with the pilot name, skill count and total SP.

Repeat for each character. Then use the pilot anywhere: `load_fit(..., pilot="<character name>")`,
`set_fit_options(fit_id, pilot=...)`, `esi_import_fitting("<character>", fitting_id)`.

Skills are a snapshot. Run `esi_refresh_skills("<character>")` after training finishes.

## Option B: Pyfa's application (no setup)

With no `EVE_ESI_CLIENT_ID` set, eve-fit uses Pyfa's public application, the same one the Pyfa desktop
app uses. The EVE consent page will say **pyfa**, and EVE redirects through
`https://pyfa-org.github.io/Pyfa/callback`, which forwards to eve-fit's local callback server. If that
forward fails, the page shows a code; pass it to `esi_login_complete`.

## Security notes

- Login uses the authorization-code flow with PKCE. With a secret, token requests also use HTTP Basic
  auth, as EVE's SSO docs require for confidential apps.
- The `state` parameter is checked on the callback, and access tokens are validated against EVE's
  published signing keys (Pyfa's JWT validation).
- Refresh tokens are encrypted with Pyfa's per-install key and stored in `data/esi_tokens.json`
  (gitignored). Each token remembers which client ID issued it; switching apps means logging in again.
- `esi_logout` deletes the stored token. You can also revoke access on EVE's
  [third-party applications page](https://community.eveonline.com/support/third-party-applications/).
- The callback server only listens on localhost, and only while a login is pending (10 minutes max).

## Troubleshooting

| Symptom | Fix |
|---|---|
| EVE says the redirect URI doesn't match | the registered Callback URL and `EVE_ESI_CALLBACK_URL` differ (check port and `/callback`) |
| `Can't listen on http://localhost:8765/callback` | another program uses the port: pick another and update both places |
| `state mismatch` | an old login tab was used; run `esi_login` again and use the new URL |
| `logged in with a different EVE app` | you switched between own/Pyfa app; run `esi_login` again |
| `invalid_client` on token request | wrong client ID/secret, or the secret was regenerated |
