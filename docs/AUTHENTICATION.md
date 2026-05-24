# Authentication

Auth is off by default. All endpoints are open until you run `bandsox auth
init`, which creates `auth.json` in the storage directory. Delete that file
to disable auth again.

## Enabling auth

```bash
sudo bandsox auth init --storage /var/lib/sandbox
```

This generates an admin password and an initial API key, and prints both to
stdout. The API key is only shown once. The CLI will offer to save it to
`~/.bandsox/credentials` (mode 600).

## How it works

When `auth.json` exists, the server supports two methods, checked by a
single FastAPI dependency:

**API keys** for CLI, SDK, and direct HTTP calls. Pass them as a Bearer
token:

```
Authorization: Bearer bsx_your_key_here
```

Keys are stored as SHA-256 hashes in `auth.json`. The plaintext is only
shown at creation time.

**Session cookies** for the web dashboard. Log in at `/login` with the
admin password to get a `bandsox_session` cookie. Sessions last 24 hours.
They're HMAC-signed tokens (expiry + SHA-256 signature), so they survive
server restarts. The signing secret is in `auth.json`.

WebSocket terminal passes auth via a `token` query parameter since the
browser WebSocket API can't send custom headers. The dashboard handles
this automatically after login.

## Auth endpoints

These endpoints work whether auth is enabled or not:

- `POST /api/auth/login` -- log in with admin password, get a session
  cookie. Rate-limited to 10 attempts per minute per IP. Returns 404 if
  auth isn't enabled.
  - Body: `{ "password": "..." }`
  - Returns: `{ "status": "ok", "token": "..." }`
- `POST /api/auth/logout` -- clear cookie.
- `GET /api/auth/check` -- returns `{ "authenticated": true/false }`.
  Always returns true when auth is disabled.

These endpoints require authentication (when auth is enabled):

- `GET /api/auth/keys` -- list all API keys (IDs and names, no secrets).
- `POST /api/auth/keys` -- create a new API key. Returns the plaintext
  key once.
  - Body: `{ "name": "my-key" }`
  - Returns: `{ "key_id": "bsx_k_...", "key": "bsx_...", "name": "my-key" }`
- `DELETE /api/auth/keys/{key_id}` -- revoke a key.

## CLI auth commands

```bash
# Enable auth (generates password + API key)
sudo bandsox auth init --storage /var/lib/sandbox

# Set or reset the admin password (direct file access, no server needed)
sudo bandsox auth set-password --storage /var/lib/sandbox

# Create a key via the server API (requires existing auth)
bandsox auth create-key my-key

# List and revoke keys
bandsox auth list-keys
bandsox auth revoke-key bsx_k_<id>
```

Credentials are stored at `~/.bandsox/credentials` with mode 600.

## SDK auth

TypeScript:

```ts
const bs = new BandSox({
  baseUrl: "http://localhost:8000",
  headers: { Authorization: "Bearer bsx_your_key_here" },
});
```

Python:

```python
bs = BandSox("http://localhost:8000", headers={"Authorization": "Bearer bsx_your_key_here"})
```

## Notes

- Sessions are signed tokens, so they survive server restarts. Both
  sessions and API keys are validated using secrets stored in `auth.json`.
- The `set-password` command works directly on the storage directory, so
  you can reset the password even if you're locked out of the dashboard.
- WebSocket terminal auth uses a `token` query parameter because the
  browser WebSocket API doesn't support custom headers.
