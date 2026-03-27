"""
FastAPI implementation for Myers Podcast generation service.

This module provides REST endpoints for podcast generation and audio serving,
with configuration management and temporary file handling.
"""

import json
import logging
import uuid
from fastapi import FastAPI, HTTPException, Header, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import os
import shutil
import yaml
from typing import Dict, Any, List
from pathlib import Path
from ..client import generate_podcast
import uvicorn

logger = logging.getLogger(__name__)

# Server-level API keys (fallback when user has not set their own)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")

# Authentik base URL for account management and logout links
# e.g. https://auth.yourdomain.com  (no trailing slash)
AUTHENTIK_URL = os.getenv("AUTHENTIK_URL", "").rstrip("/")

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
        if key != 'text_to_speech':
            if value is not None:
                merged[key] = value

    return merged


def _resolve_llm(alias: str) -> tuple:
    """Map a user-friendly model alias to (model_name, api_key_label)."""
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
    """Return (and create) the per-user data directory."""
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

# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Myers Podcast")

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp_audio")
os.makedirs(TEMP_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

@app.get("/")
def root():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return {"service": "Myers Podcast", "status": "running", "docs": "/docs"}


@app.get("/me")
def me(
    x_authentik_username: str = Header(default="", alias="X-authentik-username"),
    x_authentik_email: str = Header(default="", alias="X-authentik-email"),
    x_authentik_name: str = Header(default="", alias="X-authentik-name"),
):
    """Return the authenticated user's info from Authentik headers."""
    manage_url = f"{AUTHENTIK_URL}/if/user/" if AUTHENTIK_URL else ""
    logout_url = f"{AUTHENTIK_URL}/if/session-end/" if AUTHENTIK_URL else ""
    return {
        "username": x_authentik_username,
        "email": x_authentik_email,
        "name": x_authentik_name,
        "manage_url": manage_url,
        "logout_url": logout_url,
    }


@app.get("/profile")
def get_profile(
    x_authentik_username: str = Header(default="anonymous", alias="X-authentik-username"),
):
    """Return the current user's stored API keys (values masked)."""
    profile = _load_profile(x_authentik_username)
    # Return masked versions so the UI can show whether a key is set
    return {
        "gemini_key_set": bool(profile.get("gemini_key")),
        "openai_key_set": bool(profile.get("openai_key")),
        "elevenlabs_key_set": bool(profile.get("elevenlabs_key")),
    }


@app.post("/profile")
def save_profile(
    data: dict,
    x_authentik_username: str = Header(default="anonymous", alias="X-authentik-username"),
):
    """Save the current user's API keys. Pass empty string to clear a key."""
    profile = _load_profile(x_authentik_username)
    for field in ("gemini_key", "openai_key", "elevenlabs_key"):
        if field in data:
            val = str(data[field]).strip()
            if val:
                profile[field] = val
            else:
                profile.pop(field, None)
    _save_profile(x_authentik_username, profile)
    return {"saved": True}


@app.post("/upload")
async def upload_files(
    files: List[UploadFile] = File(...),
    x_authentik_username: str = Header(default="anonymous", alias="X-authentik-username"),
):
    """Upload files (PDF, TXT, images) for podcast generation."""
    upload_dir = _user_upload_dir(x_authentik_username)
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
        # Store original filename alongside the file
        meta_path = dest + ".meta"
        with open(meta_path, "w") as mf:
            json.dump({"name": f.filename}, mf)
        uploaded.append({"id": safe_name, "name": f.filename, "size": len(content)})
    return {"files": uploaded}


@app.get("/files")
def list_uploaded_files(
    x_authentik_username: str = Header(default="anonymous", alias="X-authentik-username"),
):
    """List all uploaded files for the current user."""
    upload_dir = _user_upload_dir(x_authentik_username)
    files = []
    for name in sorted(os.listdir(upload_dir)):
        if name.endswith(".meta"):
            continue
        path = os.path.join(upload_dir, name)
        if os.path.isfile(path):
            meta_path = path + ".meta"
            original_name = name
            try:
                with open(meta_path) as mf:
                    original_name = json.load(mf).get("name", name)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            files.append({"id": name, "name": original_name, "size": os.path.getsize(path)})
    return {"files": files}


@app.delete("/files/{file_id}")
def delete_uploaded_file(
    file_id: str,
    x_authentik_username: str = Header(default="anonymous", alias="X-authentik-username"),
):
    """Delete an uploaded file belonging to the current user."""
    upload_dir = _user_upload_dir(x_authentik_username)
    safe = Path(file_id).name  # prevent path traversal
    path = os.path.join(upload_dir, safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(path)
    meta_path = path + ".meta"
    if os.path.exists(meta_path):
        os.remove(meta_path)
    return {"deleted": safe}


@app.post("/generate")
def generate_podcast_endpoint(
    data: dict,
    x_authentik_username: str = Header(default="anonymous", alias="X-authentik-username"),
):
    try:
        logger.info("Request from user: %s", x_authentik_username)

        # --- Load base configuration ---
        base_config = load_base_config()

        # --- TTS model and voice resolution ---
        tts_model = data.get('tts_model', base_config.get('text_to_speech', {}).get('default_tts_model', 'openai'))
        tts_base_config = base_config.get('text_to_speech', {}).get(tts_model, {})
        voices = data.get('voices', {})
        default_voices = tts_base_config.get('default_voices', {})

        # --- LLM model resolution ---
        llm_model_name, api_key_label = _resolve_llm(data.get('llm_model'))

        # --- API keys: request body > user profile > server defaults ---
        profile = _load_profile(x_authentik_username)
        gemini_key = (data.get('user_gemini_api_key') or profile.get('gemini_key') or '').strip()
        openai_key = (data.get('user_openai_api_key') or profile.get('openai_key') or '').strip()
        elevenlabs_key = (data.get('user_elevenlabs_api_key') or profile.get('elevenlabs_key') or '').strip()
        if gemini_key:
            os.environ['GEMINI_API_KEY'] = gemini_key
        if openai_key:
            os.environ['OPENAI_API_KEY'] = openai_key
        if elevenlabs_key:
            os.environ['ELEVENLABS_API_KEY'] = elevenlabs_key

        # --- Build conversation config ---
        user_config = {
            'creativity': float(data.get('creativity', base_config.get('creativity', 0.7))),
            'conversation_style': data.get('conversation_style', base_config.get('conversation_style', [])),
            'roles_person1': data.get('roles_person1', base_config.get('roles_person1')),
            'roles_person2': data.get('roles_person2', base_config.get('roles_person2')),
            'dialogue_structure': data.get('dialogue_structure', base_config.get('dialogue_structure', [])),
            'podcast_name': data.get('name', base_config.get('podcast_name')),
            'podcast_tagline': data.get('tagline', base_config.get('podcast_tagline')),
            'output_language': data.get('output_language', base_config.get('output_language', 'English')),
            'user_instructions': data.get('user_instructions', base_config.get('user_instructions', '')),
            'engagement_techniques': data.get('engagement_techniques', base_config.get('engagement_techniques', [])),
            'text_to_speech': {
                'default_tts_model': tts_model,
                'model': tts_base_config.get('model'),
                'default_voices': {
                    'question': voices.get('question', default_voices.get('question')),
                    'answer': voices.get('answer', default_voices.get('answer'))
                }
            }
        }

        conversation_config = merge_configs(base_config, user_config)

        # --- Resolve uploaded file IDs to local paths ---
        upload_dir = _user_upload_dir(x_authentik_username)
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

        # --- Generate podcast ---
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

        # --- Handle result ---
        if isinstance(result, str) and os.path.isfile(result):
            filename = f"podcast_{os.urandom(8).hex()}.mp3"
            output_path = os.path.join(TEMP_DIR, filename)
            shutil.copy2(result, output_path)
            return {"audioUrl": f"/audio/{filename}"}
        elif hasattr(result, 'audio_path'):
            filename = f"podcast_{os.urandom(8).hex()}.mp3"
            output_path = os.path.join(TEMP_DIR, filename)
            shutil.copy2(result.audio_path, output_path)
            return {"audioUrl": f"/audio/{filename}"}
        else:
            raise HTTPException(status_code=500, detail="Invalid result format")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/audio/{filename}")
def serve_audio(filename: str):
    """Get audio file from the server."""
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
