# Authentik OAuth2/OIDC Authentication — Reusable Setup Guide

This guide documents how to integrate [Authentik](https://goauthentik.io/) as an OAuth2/OIDC identity provider into any self-hosted app on a Coolify server. It is based on the working implementation in this podcast app and uses standard OAuth2/OIDC — the pattern applies to any language/framework.

For a ready-to-use Python/FastAPI implementation see [`fastapi_template.py`](./fastapi_template.py).

---

## Overview

Authentik acts as the identity provider (IdP). Each app you deploy is an OAuth2 **client** that:

1. Redirects unauthenticated users to Authentik's login page
2. Receives an authorization code at a callback URL
3. Exchanges it for an access token + user info
4. Stores the user's identity in a server-side session cookie

The protocol is standard **OpenID Connect (OIDC)** over OAuth2. Any OIDC-compatible library works.

---

## Step 1 — Create a Provider in Authentik

In the Authentik admin UI (`https://<your-authentik-url>/if/admin/`):

1. Go to **Applications → Providers → Create**
2. Choose **OAuth2/OpenID Provider**
3. Configure:
   - **Name**: anything descriptive (e.g. `my-app-provider`)
   - **Client type**: `Confidential`
   - **Client ID**: auto-generated — copy this value
   - **Client Secret**: auto-generated — copy this value
   - **Redirect URIs**: `https://<your-app-url>/auth/callback`
   - **Scopes**: ensure `openid`, `profile`, `email` are selected
4. Save the provider

---

## Step 2 — Create an Application in Authentik

1. Go to **Applications → Applications → Create**
2. Configure:
   - **Name**: anything descriptive (e.g. `My App`)
   - **Slug**: a short identifier with no spaces (e.g. `my-app`) — **copy this value**
   - **Provider**: select the provider you just created
3. Optionally assign the application to groups to restrict access
4. Save

---

## Step 3 — Environment Variables

Set these variables in your app's environment (in Coolify: app → **Environment Variables** tab):

| Variable | Required | Description |
|---|---|---|
| `AUTHENTIK_URL` | Yes | Base URL of your Authentik instance, e.g. `https://auth.example.com` |
| `AUTHENTIK_CLIENT_ID` | Yes | OAuth2 Client ID from the provider |
| `AUTHENTIK_CLIENT_SECRET` | Yes | OAuth2 Client Secret from the provider |
| `AUTHENTIK_SLUG` | Yes | Application slug you set in Step 2 |
| `APP_URL` | Yes | Public URL of your app, e.g. `https://myapp.example.com` |
| `SESSION_SECRET` | Yes | Random secret used to sign session cookies |
| `ADMIN_USERS` | No | Comma-separated usernames that get elevated access |

Generate a secure `SESSION_SECRET`:
```bash
openssl rand -hex 32
```

See [`example.env`](./example.env) for a template.

---

## Step 4 — Register the Callback URL

The redirect URI you enter in Authentik **must exactly match** the URL your app uses:

```
https://<your-app-url>/auth/callback
```

If your app runs on a non-standard port (e.g. `https://myapp.example.com:8080`), include the port.

---

## OAuth2 / OIDC Flow

```
User visits protected page
        │
        ▼
App checks session → no session found
        │
        ▼
GET /login
        │  redirect_uri = APP_URL/auth/callback
        ▼
Authentik login page  ←── user enters credentials
        │
        ▼  authorization code
GET /auth/callback?code=...
        │
        ▼
App exchanges code for token (server-to-server)
        │
        ▼
App reads userinfo from token:
  preferred_username  → session["username"]
  name                → session["name"]
  email               → session["email"]
        │
        ▼
Redirect to /  (session cookie set)
```

---

## OIDC Discovery URL

Authentik exposes an OIDC discovery document at:

```
{AUTHENTIK_URL}/application/o/{AUTHENTIK_SLUG}/.well-known/openid-configuration
```

Most OIDC client libraries accept this URL and auto-configure all endpoints (authorization, token, userinfo, JWKS). Always prefer this over hardcoding individual endpoint URLs.

---

## Session Claims

The three OIDC claims used from the `userinfo` response:

| OIDC Claim | Fallback | Stored as |
|---|---|---|
| `preferred_username` | `sub` | `session["username"]` |
| `name` | — | `session["name"]` |
| `email` | — | `session["email"]` |

The `sub` claim is a stable, unique user identifier — useful as a fallback if `preferred_username` is absent.

---

## Logout

To fully end the user's SSO session (not just your app's session), redirect to:

```
{AUTHENTIK_URL}/if/session-end/
```

This logs the user out of Authentik itself, so they are also logged out of all other apps sharing the same Authentik session. If you only want to log out of your app, just clear the session cookie and redirect to `/login`.

---

## Anonymous Fallback Pattern

Useful for local development without Authentik configured. Check if the four Authentik env vars are set — if not, bypass OAuth and set a default session:

```
if AUTHENTIK is not configured:
    set session.username = "anonymous"
    redirect to /
else:
    redirect to Authentik for login
```

This lets you run the app locally without any Authentik setup.

---

## Coolify Deployment Notes

- **Environment variables**: Set them in your service's **Environment Variables** tab. Mark `AUTHENTIK_CLIENT_SECRET` and `SESSION_SECRET` as secret/sensitive.
- **HTTPS is required**: Session cookies must use `Secure` flag, which requires HTTPS. Coolify's built-in Traefik proxy handles TLS termination automatically when you configure a domain.
- **Callback URL**: After deploying, verify the `APP_URL` env var matches the domain Coolify assigned to your service. The callback URL registered in Authentik must match exactly.
- **Multiple apps**: Each app gets its own Authentik Provider + Application with its own Client ID/Secret and redirect URI. They can all share the same Authentik instance and user pool.

---

## Adapting to Other Stacks

The pattern is the same regardless of framework. You need:

1. **OIDC client library** — handles the OAuth2 code exchange and OIDC discovery
2. **Session middleware** — stores the user identity server-side after login
3. **Three routes**: `GET /login`, `GET /auth/callback`, `GET /logout`

### Pseudocode (language-agnostic)

```
# Setup
oidc_client = OIDCClient(
    discovery_url = AUTHENTIK_URL + "/application/o/" + SLUG + "/.well-known/openid-configuration",
    client_id     = AUTHENTIK_CLIENT_ID,
    client_secret = AUTHENTIK_CLIENT_SECRET,
    scopes        = ["openid", "profile", "email"],
)

# Login route
GET /login:
    redirect_to oidc_client.authorization_url(redirect_uri = APP_URL + "/auth/callback")

# Callback route
GET /auth/callback:
    token    = oidc_client.exchange_code(request.query["code"])
    userinfo = token.userinfo
    session["username"] = userinfo["preferred_username"] or userinfo["sub"]
    session["name"]     = userinfo["name"]
    session["email"]    = userinfo["email"]
    redirect_to /

# Logout route
GET /logout:
    session.clear()
    redirect_to AUTHENTIK_URL + "/if/session-end/"
```

### Library recommendations by stack

| Stack | Library |
|---|---|
| Python / FastAPI or Flask | [`authlib`](https://docs.authlib.org/) (used in this app) |
| Python / Django | [`mozilla-django-oidc`](https://mozilla-django-oidc.readthedocs.io/) |
| Node.js / Express | [`openid-client`](https://github.com/panva/node-openid-client) or [`passport-openidconnect`](https://github.com/jaredhanson/passport-openidconnect) |
| Node.js / Next.js | [`next-auth`](https://next-auth.js.org/) with a generic OIDC provider |
| Go | [`coreos/go-oidc`](https://github.com/coreos/go-oidc) |
| PHP | [`jumbojett/OpenID-Connect-PHP`](https://github.com/jumbojett/OpenID-Connect-PHP) |

### Frontend — displaying user info

After login the session is set server-side. Expose a `/me` endpoint returning the current user's info as JSON, then fetch it from the frontend:

```javascript
fetch('/me')
  .then(r => r.json())
  .then(user => {
    document.getElementById('username').textContent = user.name || user.username;
  });
```

Provide a logout link pointing to `/logout`.

---

## Reference Implementation

See [`fastapi_template.py`](./fastapi_template.py) for a complete, copy-paste-ready Python/FastAPI implementation with detailed comments.
