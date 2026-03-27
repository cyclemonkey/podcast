"""
FastAPI implementation for Myers Podcast generation service.

This module provides REST endpoints for podcast generation and audio serving,
with configuration management and temporary file handling.

Authentication is handled via OIDC/OAuth2 against Authentik, with session cookies
managed by Starlette's SessionMiddleware.
"""

import json
import logging
import uuid
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth, OAuthError
import os
import shutil
import yaml
from typing import Dict, Any, List
from pathlib import Path
from ..client import generate_podcast
import uvicorn

logger = logging.getLogger(__name__)

# ── Server-level API keys (fallback when user has not set their own) ──────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")

# ── Authentik / OIDC config ───────────────────────────────────────────────────
AUTHENTIK_URL           = os.getenv("AUTHENTIK_URL", "").rstrip("/")
AUTHENTIK_CLIENT_ID     = os.getenv("AUTHENTIK_CLIENT_ID", "")
AUTHENTIK_CLIENT_SECRET = os.getenv("AUTHENTIK_CLIENT_SECRET", "")
AUTHENTIK_SLUG          = os.getenv("AUTHENTIK_SLUG", "")   # Authentik application slug
APP_URL                 = os.getenv("APP_URL", "").rstrip("/")
SESSION_SECRET          = os.getenv("SESSION_SECRET", "change-me-in-production")

LLM_MODEL_MAP = {
    "google": "gemini-2.5-flash",
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
}


def load_base_config() -> Dict[Any, Any]:
    config_path = Path(__file__).parent.parent / "conversation_config.yaml"
    try:
        with open(config_path, 'r') as file:
            return yaml.safe_load(file)
    except Exception as e:
        print(f"Warning: Could not load base config: {e}")
        return {}

def merge_configs(base_config: Dict[Any, Any], user_config: Dict[Any, Any]) -> Dict[Any, Any]:
    """Merge user configuration with base configuration, preferring user values."""
    merged = base_config.copy()
    if 'text_to_speech' in merged and 'text_to_speech' in user_config:
        merged['text_to_speech'].update(user_config.get('text_to_speech', {}))
    for key, value in user_config.items():
        if key != 'text_to_speech' and value is not None:
            merged[key] = value
    return merged

def _resolve_llm(alias: str) -> tuple:
    alias = (alias or "google").lower()
    model_name = LLM_MODEL_MAP.get(alias, LLM_MODEL_MAP["google"])
    api_key_label = "OPENAI_API_KEY" if "gpt" in model_name else "GEMINI_API_KEY"
    return model_name, api_key_label


# ── Per-user storage ──────────────────────────────────────────────────────────

USER_DATA_DIR = os.getenv(
    "USER_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "user_data"),
)

def _user_dir(username: str) -> str:
    safe = "".join(c for c in username if c.isalnum() or c in "-_.")
    safe = safe or "anonymous"
    path = os.path.join(USER_DATA_DIR, safe)
    os.makedirs(path, exist_ok=True)
    return path

def _user_upload_dir(username: str) -> str:
    path = os.path.join(_user_dir(username), "files")
    os.makedirs(path, exist_ok=True)
    return path

def _load_profile(username: str) -> dict:
    settings_path = os.path.join(_user_dir(username), "settings.json")
    try:
        with open(settings_path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_profile(username: str, data: dict) -> None:
    settings_path = os.path.join(_user_dir(username), "settings.json")
    with open(settings_path, "w") as f:
        json.dump(data, f)

def _session_user(request: Request) -> str:
    """Return the logged-in username from the session, or empty string."""
    return request.session.get("username", "")


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Myers Podcast")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=APP_URL.startswith("https://") if APP_URL else False,
)

# OAuth client — only usable when OIDC env vars are set
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

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp_audio")
os.makedirs(TEMP_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
async def login(request: Request):
    """Redirect to Authentik OIDC login page."""
    if not AUTHENTIK_CLIENT_ID:
        # OIDC not configured — grant anonymous access
        request.session["username"] = "anonymous"
        request.session["name"] = "Anonymous"
        request.session["email"] = ""
        return RedirectResponse(url="/")
    callback_url = f"{APP_URL}/auth/callback"
    return await oauth.authentik.authorize_redirect(request, callback_url)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    """Handle OIDC callback from Authentik, set session, redirect to app."""
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
    """Clear session and redirect to Authentik logout."""
    request.session.clear()
    if AUTHENTIK_URL:
        return RedirectResponse(url=f"{AUTHENTIK_URL}/if/session-end/")
    return RedirectResponse(url="/login")


# ── Frontend routes ───────────────────────────────────────────────────────────

@app.get("/")
async def root(request: Request):
    if not _session_user(request):
        return RedirectResponse(url="/login")
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return RedirectResponse(url="/login")


@app.get("/user/profile")
async def profile_page(request: Request):
    if not _session_user(request):
        return RedirectResponse(url="/login")
    page = FRONTEND_DIR / "profile.html"
    if page.exists():
        return HTMLResponse(page.read_text())
    raise HTTPException(status_code=404, detail="Profile page not found")


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/me")
async def me(request: Request):
    """Return the authenticated user's info from the session."""
    username = _session_user(request)
    return {
        "username": username,
        "name":     request.session.get("name", ""),
        "email":    request.session.get("email", ""),
        "logout_url": "/logout",
        "profile_url": "/user/profile",
    }


@app.get("/profile")
async def get_profile(request: Request):
    """Return the current user's stored API keys."""
    username = _session_user(request) or "anonymous"
    profile = _load_profile(username)
    return {
        "gemini_key":         profile.get("gemini_key", ""),
        "openai_key":         profile.get("openai_key", ""),
        "elevenlabs_key":     profile.get("elevenlabs_key", ""),
        "gemini_key_set":     bool(profile.get("gemini_key")),
        "openai_key_set":     bool(profile.get("openai_key")),
        "elevenlabs_key_set": bool(profile.get("elevenlabs_key")),
    }


@app.post("/profile")
async def save_profile(request: Request, data: dict):
    """Save the current user's API keys. Pass empty string to clear a key."""
    username = _session_user(request) or "anonymous"
    profile = _load_profile(username)
    for field in ("gemini_key", "openai_key", "elevenlabs_key"):
        if field in data:
            val = str(data[field]).strip()
            if val:
                profile[field] = val
            else:
                profile.pop(field, None)
    _save_profile(username, profile)
    return {"saved": True}


@app.post("/upload")
async def upload_files(request: Request, files: List[UploadFile] = File(...)):
    """Upload files (PDF, TXT, images) for podcast generation."""
    username = _session_user(request) or "anonymous"
    upload_dir = _user_upload_dir(username)
    uploaded = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
            )
        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"File too large: {f.filename} (max 20 MB)")
        file_id = uuid.uuid4().hex[:12]
        safe_name = f"{file_id}{ext}"
        dest = os.path.join(upload_dir, safe_name)
        with open(dest, "wb") as out:
            out.write(content)
        meta_path = dest + ".meta"
        with open(meta_path, "w") as mf:
            json.dump({"name": f.filename}, mf)
        uploaded.append({"id": safe_name, "name": f.filename, "size": len(content)})
    return {"files": uploaded}


@app.get("/files")
async def list_uploaded_files(request: Request):
    """List all uploaded files for the current user."""
    username = _session_user(request) or "anonymous"
    upload_dir = _user_upload_dir(username)
    files = []
    for name in sorted(os.listdir(upload_dir)):
        if name.endswith(".meta"):
            continue
        path = os.path.join(upload_dir, name)
        if os.path.isfile(path):
            original_name = name
            try:
                with open(path + ".meta") as mf:
                    original_name = json.load(mf).get("name", name)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            files.append({"id": name, "name": original_name, "size": os.path.getsize(path)})
    return {"files": files}


@app.delete("/files/{file_id}")
async def delete_uploaded_file(file_id: str, request: Request):
    """Delete an uploaded file belonging to the current user."""
    username = _session_user(request) or "anonymous"
    upload_dir = _user_upload_dir(username)
    safe = Path(file_id).name
    path = os.path.join(upload_dir, safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(path)
    meta_path = path + ".meta"
    if os.path.exists(meta_path):
        os.remove(meta_path)
    return {"deleted": safe}


@app.post("/generate")
async def generate_podcast_endpoint(request: Request, data: dict):
    try:
        username = _session_user(request) or "anonymous"
        logger.info("Request from user: %s", username)

        base_config = load_base_config()

        tts_model = data.get('tts_model', base_config.get('text_to_speech', {}).get('default_tts_model', 'openai'))
        tts_base_config = base_config.get('text_to_speech', {}).get(tts_model, {})
        voices = data.get('voices', {})
        default_voices = tts_base_config.get('default_voices', {})

        llm_model_name, api_key_label = _resolve_llm(data.get('llm_model'))

        # API keys: request body > user profile > server env var
        profile = _load_profile(username)
        gemini_key     = (data.get('user_gemini_api_key')     or profile.get('gemini_key')     or GEMINI_API_KEY).strip()
        openai_key     = (data.get('user_openai_api_key')     or profile.get('openai_key')     or OPENAI_API_KEY).strip()
        elevenlabs_key = (data.get('user_elevenlabs_api_key') or profile.get('elevenlabs_key') or ELEVENLABS_API_KEY).strip()
        if gemini_key:
            os.environ['GEMINI_API_KEY'] = gemini_key
        if openai_key:
            os.environ['OPENAI_API_KEY'] = openai_key
        if elevenlabs_key:
            os.environ['ELEVENLABS_API_KEY'] = elevenlabs_key

        user_config = {
            'creativity':          float(data.get('creativity', base_config.get('creativity', 0.7))),
            'conversation_style':  data.get('conversation_style', base_config.get('conversation_style', [])),
            'roles_person1':       data.get('roles_person1', base_config.get('roles_person1')),
            'roles_person2':       data.get('roles_person2', base_config.get('roles_person2')),
            'dialogue_structure':  data.get('dialogue_structure', base_config.get('dialogue_structure', [])),
            'podcast_name':        data.get('name', base_config.get('podcast_name')),
            'podcast_tagline':     data.get('tagline', base_config.get('podcast_tagline')),
            'output_language':     data.get('output_language', base_config.get('output_language', 'English')),
            'user_instructions':   data.get('user_instructions', base_config.get('user_instructions', '')),
            'engagement_techniques': data.get('engagement_techniques', base_config.get('engagement_techniques', [])),
            'text_to_speech': {
                'default_tts_model': tts_model,
                'model': tts_base_config.get('model'),
                'default_voices': {
                    'question': voices.get('question', default_voices.get('question')),
                    'answer':   voices.get('answer',   default_voices.get('answer')),
                }
            }
        }

        conversation_config = merge_configs(base_config, user_config)

        upload_dir = _user_upload_dir(username)
        urls = list(data.get('urls', []))
        image_paths = []
        text_input = data.get('text', '')
        for fid in data.get('file_ids', []):
            safe = Path(fid).name
            fpath = os.path.join(upload_dir, safe)
            if not os.path.isfile(fpath):
                raise HTTPException(status_code=400, detail=f"Uploaded file not found: {fid}")
            ext = Path(fpath).suffix.lower()
            if ext == '.pdf':
                urls.append(fpath)
            elif ext == '.txt':
                text_input += "\n" + Path(fpath).read_text(errors='replace')
            elif ext in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
                image_paths.append(fpath)

        gen_kwargs = dict(
            conversation_config=conversation_config,
            tts_model=tts_model,
            longform=bool(data.get('is_long_form', False)),
            llm_model_name=llm_model_name,
            api_key_label=api_key_label,
        )
        if urls:
            gen_kwargs['urls'] = urls
        if image_paths:
            gen_kwargs['image_paths'] = image_paths
        if text_input.strip():
            gen_kwargs['text'] = text_input.strip()

        result = generate_podcast(**gen_kwargs)

        if isinstance(result, str) and os.path.isfile(result):
            filename = f"podcast_{os.urandom(8).hex()}.mp3"
            shutil.copy2(result, os.path.join(TEMP_DIR, filename))
            return {"audioUrl": f"/audio/{filename}"}
        elif hasattr(result, 'audio_path'):
            filename = f"podcast_{os.urandom(8).hex()}.mp3"
            shutil.copy2(result.audio_path, os.path.join(TEMP_DIR, filename))
            return {"audioUrl": f"/audio/{filename}"}
        else:
            raise HTTPException(status_code=500, detail="Invalid result format")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/audio/{filename}")
def serve_audio(filename: str):
    file_path = os.path.join(TEMP_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@app.get("/health")
def healthcheck():
    return {"status": "healthy"}


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host=host, port=port)
