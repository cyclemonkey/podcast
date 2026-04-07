"""
Authentik OAuth2/OIDC authentication template for FastAPI.

Copy this file into your project and integrate as follows:
  1. Install dependencies:  pip install fastapi authlib starlette itsdangerous
  2. Set the required environment variables (see README.md / example.env)
  3. Mount the routes onto your FastAPI app (see "Integration" section at bottom)
  4. Protect routes with Depends(require_auth) or check session directly

This template matches the implementation in the podcast app at
podcastfy/api/fast_app.py and is tested against Authentik 2024.x.
"""

import os
from typing import Optional

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

# ── Environment variables ─────────────────────────────────────────────────────
# All four AUTHENTIK_* vars must be set to enable authentication.
# If any are missing the app falls back to an anonymous session (useful locally).

AUTHENTIK_URL = os.getenv("AUTHENTIK_URL", "").rstrip("/")
AUTHENTIK_CLIENT_ID = os.getenv("AUTHENTIK_CLIENT_ID", "")
AUTHENTIK_CLIENT_SECRET = os.getenv("AUTHENTIK_CLIENT_SECRET", "")
AUTHENTIK_SLUG = os.getenv("AUTHENTIK_SLUG", "")

# APP_URL is used to build the OAuth callback URI.
# Must be the public URL of this app, e.g. "https://myapp.example.com"
APP_URL = os.getenv("APP_URL", "").rstrip("/")

# SESSION_SECRET signs the session cookie. Generate with: openssl rand -hex 32
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-in-production")

# ADMIN_USERS: comma-separated usernames with elevated access.
# If empty, all authenticated users are treated as admins (solo/dev setup).
ADMIN_USERS = {u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()}

# ── FastAPI app ───────────────────────────────────────────────────────────────
# TODO: If you already have a FastAPI app instance, remove this line and
#       add the middleware + routes to your existing app instead.
app = FastAPI()

# ── Session middleware ────────────────────────────────────────────────────────
# Stores session data in a signed cookie. Must be added before any routes.
# https_only=True is enforced when APP_URL uses HTTPS (required in production).
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=APP_URL.startswith("https://") if APP_URL else False,
)

# ── OAuth2 / OIDC client ──────────────────────────────────────────────────────
# Uses OIDC discovery to auto-configure all endpoints from Authentik.
# The discovery URL pattern for Authentik is:
#   {AUTHENTIK_URL}/application/o/{AUTHENTIK_SLUG}/.well-known/openid-configuration

oauth = OAuth()

if AUTHENTIK_CLIENT_ID and AUTHENTIK_CLIENT_SECRET and AUTHENTIK_SLUG and AUTHENTIK_URL:
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

# ── Session helpers ───────────────────────────────────────────────────────────

def get_session_user(request: Request) -> str:
    """Return the username stored in the session, or empty string if not logged in."""
    return request.session.get("username", "")


def is_admin(request: Request) -> bool:
    """
    Return True if the current user has admin access.
    When ADMIN_USERS is not configured, all users are admins (solo/dev setup).
    """
    username = get_session_user(request)
    if not ADMIN_USERS:
        return True
    return username in ADMIN_USERS


# ── Auth dependencies (use with FastAPI Depends) ──────────────────────────────

def require_auth(request: Request) -> str:
    """
    FastAPI dependency that enforces authentication.
    Raises HTTP 401 if no session is found.

    Usage:
        @app.get("/protected")
        async def protected_route(username: str = Depends(require_auth)):
            return {"hello": username}

    For browser routes that should redirect to /login instead of returning 401,
    use require_auth_redirect below.
    """
    username = get_session_user(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


def require_auth_redirect(request: Request) -> str:
    """
    FastAPI dependency that redirects to /login when not authenticated.
    Use this for browser-facing HTML routes.

    Usage:
        @app.get("/dashboard")
        async def dashboard(username: str = Depends(require_auth_redirect)):
            return HTMLResponse("<h1>Hello " + username + "</h1>")
    """
    username = get_session_user(request)
    if not username:
        raise HTTPException(
            status_code=307,
            headers={"Location": "/login"},
            detail="Not authenticated",
        )
    return username


def require_admin(request: Request) -> str:
    """
    FastAPI dependency that enforces admin access.
    Raises HTTP 403 if the user is not in ADMIN_USERS.
    """
    username = get_session_user(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    return username


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
async def login(request: Request):
    """
    Start the OAuth2 login flow.

    - If Authentik is not configured (AUTHENTIK_CLIENT_ID is empty), falls back
      to an anonymous session so the app works without any auth setup (local dev).
    - Otherwise redirects to Authentik's authorization endpoint.
    """
    if not AUTHENTIK_CLIENT_ID:
        # Anonymous fallback — remove this block if you want to require auth always
        request.session["username"] = "anonymous"
        request.session["name"] = "Anonymous"
        request.session["email"] = ""
        return RedirectResponse(url="/")

    return await oauth.authentik.authorize_redirect(
        request,
        redirect_uri=f"{APP_URL}/auth/callback",
    )


@app.get("/auth/callback")
async def auth_callback(request: Request):
    """
    OAuth2 callback. Authentik redirects here with an authorization code.
    Exchanges the code for a token, extracts user info, and stores it in the session.
    """
    try:
        token = await oauth.authentik.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=f"OAuth error: {exc}")

    userinfo = token.get("userinfo") or {}

    # Map OIDC claims to session keys.
    # preferred_username is the Authentik username; sub is a stable fallback UUID.
    request.session["username"] = (
        userinfo.get("preferred_username") or userinfo.get("sub", "unknown")
    )
    request.session["name"] = userinfo.get("name", "")
    request.session["email"] = userinfo.get("email", "")

    # TODO: redirect to the page the user originally requested if you stored it
    return RedirectResponse(url="/")


@app.get("/logout")
async def logout(request: Request):
    """
    Clear the local session and redirect to Authentik's session-end endpoint.
    This fully logs the user out of the SSO session, affecting all apps.

    If you only want to log out of this app (keep SSO alive), remove the
    Authentik redirect and just redirect to /login.
    """
    request.session.clear()

    if AUTHENTIK_URL:
        # Ends the Authentik SSO session globally
        return RedirectResponse(url=f"{AUTHENTIK_URL}/if/session-end/")

    return RedirectResponse(url="/login")


# ── User info endpoint ────────────────────────────────────────────────────────

@app.get("/me")
async def me(request: Request):
    """
    Returns the current user's session data as JSON.
    Used by the frontend to display username, show/hide admin UI, etc.

    Example response:
        {
            "username": "alice",
            "name": "Alice Smith",
            "email": "alice@example.com",
            "is_admin": true,
            "logout_url": "/logout",
            "profile_url": "/user/profile"
        }
    """
    return {
        "username": get_session_user(request),
        "name": request.session.get("name", ""),
        "email": request.session.get("email", ""),
        "is_admin": is_admin(request),
        "logout_url": "/logout",
        "profile_url": "/user/profile",  # TODO: adjust or remove if you have no profile page
    }


# ── Example protected routes ──────────────────────────────────────────────────

@app.get("/")
async def root(request: Request):
    """
    Browser-facing root. Redirects to /login if not authenticated.
    TODO: replace HTMLResponse with your actual frontend.
    """
    if not get_session_user(request):
        return RedirectResponse(url="/login")

    username = get_session_user(request)
    return HTMLResponse(f"<h1>Hello, {username}!</h1><a href='/logout'>Log out</a>")


@app.get("/api/data")
async def api_data(username: str = Depends(require_auth)):
    """
    Example API route protected with require_auth dependency.
    Returns 401 JSON if not authenticated.
    """
    # TODO: replace with your actual logic
    return {"message": f"Hello {username}, here is your data."}


@app.get("/admin/settings")
async def admin_settings(username: str = Depends(require_admin)):
    """
    Example admin-only route. Returns 403 if not in ADMIN_USERS.
    """
    # TODO: replace with your actual admin logic
    return {"message": f"Admin panel for {username}"}


# ── Integration notes ─────────────────────────────────────────────────────────
#
# If you have an existing FastAPI app, do NOT use the `app` instance above.
# Instead, add to YOUR existing app:
#
#   from fastapi import FastAPI
#   from starlette.middleware.sessions import SessionMiddleware
#   from authlib.integrations.starlette_client import OAuth, OAuthError
#
#   your_app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, ...)
#   # Then register oauth and include the route functions above
#
# The three required routes are:
#   GET /login          → start OAuth flow
#   GET /auth/callback  → receive code, set session
#   GET /logout         → clear session + redirect to Authentik session-end
#
# Everything else (helpers, /me, dependencies) is optional but recommended.
