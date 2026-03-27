# In-App OIDC Authentication with Authentik

The app handles login/logout itself using OAuth2/OIDC directly with Authentik.
No Traefik middleware required. Works reliably in Coolify.

---

## How it works

1. User visits `/` → redirected to `/login` if no session
2. `/login` → redirects to Authentik login page
3. User logs in on Authentik → redirected back to `/auth/callback`
4. App exchanges the code for a token, stores username/name/email in a signed cookie session
5. All routes read identity from the session cookie

---

## Dependencies

```
authlib>=1.3.0
itsdangerous>=2.1.0
```

`itsdangerous` is required by Starlette's `SessionMiddleware`.

---

## FastAPI setup

```python
import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth, OAuthError

AUTHENTIK_URL           = os.getenv("AUTHENTIK_URL", "").rstrip("/")
AUTHENTIK_CLIENT_ID     = os.getenv("AUTHENTIK_CLIENT_ID", "")
AUTHENTIK_CLIENT_SECRET = os.getenv("AUTHENTIK_CLIENT_SECRET", "")
AUTHENTIK_SLUG          = os.getenv("AUTHENTIK_SLUG", "")
APP_URL                 = os.getenv("APP_URL", "").rstrip("/")
SESSION_SECRET          = os.getenv("SESSION_SECRET", "change-me")

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=APP_URL.startswith("https://"),
)

oauth = OAuth()
if AUTHENTIK_CLIENT_ID and AUTHENTIK_CLIENT_SECRET:
    oauth.register(
        name="authentik",
        client_id=AUTHENTIK_CLIENT_ID,
        client_secret=AUTHENTIK_CLIENT_SECRET,
        server_metadata_url=(
            f"{AUTHENTIK_URL}/application/o/{AUTHENTIK_SLUG}"
            "/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid profile email"},
    )

def current_user(request: Request) -> str:
    return request.session.get("username", "")

@app.get("/login")
async def login(request: Request):
    if not AUTHENTIK_CLIENT_ID:
        # Dev fallback: skip auth
        request.session["username"] = "dev"
        request.session["name"] = "Dev User"
        request.session["email"] = ""
        return RedirectResponse(url="/")
    return await oauth.authentik.authorize_redirect(request, f"{APP_URL}/auth/callback")

@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.authentik.authorize_access_token(request)
    except OAuthError as e:
        raise HTTPException(status_code=400, detail=f"OAuth error: {e}")
    user = token.get("userinfo") or {}
    request.session["username"] = user.get("preferred_username") or user.get("sub", "unknown")
    request.session["name"]     = user.get("name", "")
    request.session["email"]    = user.get("email", "")
    return RedirectResponse(url="/")

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    if AUTHENTIK_URL:
        return RedirectResponse(url=f"{AUTHENTIK_URL}/if/session-end/")
    return RedirectResponse(url="/login")

@app.get("/me")
async def me(request: Request):
    return {
        "username": request.session.get("username", ""),
        "name":     request.session.get("name", ""),
        "email":    request.session.get("email", ""),
    }

@app.get("/")
async def root(request: Request):
    if not current_user(request):
        return RedirectResponse(url="/login")
    # serve your app...
```

Use `current_user(request)` in every protected route. Return 401 or redirect to `/login` if empty.

---

## Authentik setup

1. **Create an OAuth2/OIDC Provider**
   - Name: anything (e.g. `My App`)
   - Authorization flow: `default-provider-authorization-explicit-consent`
   - Client type: `Confidential`
   - Redirect URI: `https://your-app-domain/auth/callback`
   - Scopes: `openid`, `profile`, `email` (defaults)
   - Subject mode: `Based on the User's username`
   - Copy the **Client ID** and **Client Secret**

2. **Create an Application**
   - Name: anything
   - Slug: e.g. `my-app` → this is `AUTHENTIK_SLUG`
   - Provider: select the one above
   - Launch URL: `https://your-app-domain`

---

## Environment variables

| Variable                | Example value                        |
|-------------------------|--------------------------------------|
| `AUTHENTIK_URL`         | `https://auth.yourdomain.com`        |
| `AUTHENTIK_CLIENT_ID`   | from Authentik provider              |
| `AUTHENTIK_CLIENT_SECRET` | from Authentik provider            |
| `AUTHENTIK_SLUG`        | `my-app`                             |
| `APP_URL`               | `https://your-app-domain.com`        |
| `SESSION_SECRET`        | long random hex string               |

Generate `SESSION_SECRET` with:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

All vars are **runtime** (not build-time). Do not change `SESSION_SECRET` after deployment — it invalidates all sessions.

---

## docker-compose.yml

```yaml
environment:
  - AUTHENTIK_URL=${AUTHENTIK_URL}
  - AUTHENTIK_CLIENT_ID=${AUTHENTIK_CLIENT_ID}
  - AUTHENTIK_CLIENT_SECRET=${AUTHENTIK_CLIENT_SECRET}
  - AUTHENTIK_SLUG=${AUTHENTIK_SLUG}
  - APP_URL=${APP_URL}
  - SESSION_SECRET=${SESSION_SECRET}
```

No special Traefik labels needed beyond the standard routing ones.

---

## Pros / cons

**Pros**
- Works reliably in Coolify without any Traefik middleware configuration
- Full control over login/logout flow within the app
- Works even if the app is accessed directly (no proxy dependency)

**Cons**
- Requires Authentik OAuth2 provider + application to be configured
- Adds ~3 dependencies and auth routes to the app
- Sessions are tied to `SESSION_SECRET` — rotating it logs everyone out
