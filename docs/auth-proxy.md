# Proxy-Level Authentication with Authentik Forward Auth

Authentik sits in front of the app via Traefik. The app never sees unauthenticated
requests — Authentik intercepts them and forces login. Identity is passed to the
app via HTTP headers.

**Warning:** This method is difficult to get working reliably in Coolify and has
several sharp edges. See the pitfalls section before choosing this approach.

---

## How it works

1. Request hits Traefik
2. Traefik's `forwardAuth` middleware sends the request to Authentik's outpost for verification
3. If not logged in, Authentik redirects to its login page
4. After login, Authentik sets a cookie and Traefik forwards the original request to the app
5. Authentik injects headers (`X-authentik-username`, `X-authentik-email`, etc.) into the forwarded request
6. The app reads identity from those headers — no session management needed

---

## App code

No auth middleware or login routes required. Just read the headers:

```python
from fastapi import FastAPI, Request

app = FastAPI()

def current_user(request: Request) -> str:
    return request.headers.get("X-authentik-username", "")

@app.get("/me")
async def me(request: Request):
    return {
        "username": request.headers.get("X-authentik-username", ""),
        "name":     request.headers.get("X-authentik-name", ""),
        "email":    request.headers.get("X-authentik-email", ""),
    }
```

Headers injected by Authentik:

| Header                    | Content                        |
|---------------------------|--------------------------------|
| `X-authentik-username`    | User's username                |
| `X-authentik-name`        | User's display name            |
| `X-authentik-email`       | User's email address           |
| `X-authentik-uid`         | User's unique ID               |
| `X-authentik-groups`      | Comma-separated group list     |

---

## Authentik setup

1. **Deploy an Authentik Outpost** (embedded or standalone)
   - In Authentik admin → Outposts → create a Proxy outpost
   - Select your existing Authentik instance as the integration

2. **Create a Proxy Provider**
   - Type: `Proxy Provider`
   - Mode: `Forward auth (single application)`
   - External host: `https://your-app-domain.com`
   - Authorization flow: `default-provider-authorization-explicit-consent`

3. **Create an Application**
   - Bind the proxy provider
   - Slug: e.g. `my-app`

4. **Bind the application to the outpost**
   - Outposts → edit → add the application

---

## Traefik / docker-compose labels

Authentik provides a Traefik middleware via its outpost. Reference it in your service labels:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.myapp.rule=Host(`your-app-domain.com`)"
  - "traefik.http.routers.myapp.middlewares=authentik-auth@file"
  - "traefik.http.services.myapp.loadbalancer.server.port=8000"
```

The `authentik-auth@file` middleware is defined in Traefik's dynamic config by the
Authentik outpost — it must already exist before adding the label. In Coolify the
outpost container typically creates this automatically when deployed.

---

## Coolify-specific notes

Coolify manages Traefik itself. To use a custom Traefik middleware:

1. The Authentik outpost must be deployed in the **same Docker network** as Traefik
   (usually the `coolify` network)
2. The outpost registers the `authentik-auth@file` middleware dynamically via
   Traefik's file provider — check Traefik's dashboard to confirm it exists
3. In Coolify, add the middleware label under **Custom Labels** for the service

---

## Logout

The app itself cannot log users out — the session is held by Authentik. Link to:
```
https://auth.yourdomain.com/if/session-end/
```

Or, for a specific application:
```
https://auth.yourdomain.com/if/session-end/?next=https://your-app-domain.com
```

---

## Pitfalls (from experience)

- **Headers not forwarded:** If Authentik's outpost is misconfigured or not bound
  to the application, headers arrive empty even when the user is "logged in". The
  app gets blank username/name/email with no error.

- **Authentik outpost returns 404:** If the outpost doesn't recognise the app URL,
  it intercepts all requests and returns `{"detail":"Not Found"}` instead of
  forwarding them. Fix: ensure the External host in the Proxy Provider exactly
  matches the URL Traefik routes to the app.

- **Middleware label rejected:** The label `traefik.http.middlewares.authentik-auth@file`
  (without `=value`) is Coolify convention but confuses Traefik. The correct form is
  `traefik.http.routers.<name>.middlewares=authentik-auth@file`.

- **Race condition on deploy:** If the app restarts before the outpost registers its
  middleware, Traefik may serve 503s until both are healthy.

- **No fallback:** If Authentik is down, the app is completely inaccessible.

---

## Pros / cons

**Pros**
- Zero auth code in the app — just read headers
- Single sign-on across multiple apps automatically
- Authentik handles MFA, login UI, session expiry

**Cons**
- Hard to debug when headers are missing
- Authentik outpost must be running and correctly configured at all times
- Logout requires redirecting to Authentik
- Fragile in Coolify — outpost, Traefik middleware, and app must all align
- App is unauthenticated in local dev unless you mock the headers
