"""
FastAPI implementation for Myers Podcast generation service.

This module provides REST endpoints for podcast generation and audio serving,
with configuration management and temporary file handling.
"""

import secrets
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

# Read API keys once at startup from environment (set via Render env vars)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")

# APP_USERS format: "alice:secret1,bob:secret2"
def _parse_users(raw: str) -> Dict[str, str]:
    users = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            username, password = entry.split(":", 1)
            users[username.strip()] = password.strip()
    return users

APP_USERS: Dict[str, str] = _parse_users(os.getenv("APP_USERS", ""))

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

    # Handle special cases for nested dictionaries
    if 'text_to_speech' in merged and 'text_to_speech' in user_config:
        merged['text_to_speech'].update(user_config.get('text_to_speech', {}))

    # Update top-level keys
    for key, value in user_config.items():
        if key != 'text_to_speech':  # Skip text_to_speech as it's handled above
            if value is not None:  # Only update if value is not None
                merged[key] = value

    return merged


def _check_password(data: dict, x_app_password: str = "") -> str:
    """
    Validate user credentials against APP_USERS and return the authenticated username.
    Accepts credentials from the JSON body (user + password fields) or the
    X-App-Password header (format: "username:password").
    Returns the username so it can be logged.
    If APP_USERS is not configured, open access is allowed (local dev).
    """
    if not APP_USERS:
        return "anonymous"

    # Extract from body first, then fall back to header
    username = data.get("user", "")
    password = data.get("password", "")
    if not username and x_app_password and ":" in x_app_password:
        username, password = x_app_password.split(":", 1)

    if not username or not password:
        raise HTTPException(status_code=401, detail="Authentication required: provide 'user' and 'password'")

    expected = APP_USERS.get(username)
    if expected is None or not secrets.compare_digest(password, expected):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return username


def _resolve_llm(alias: str) -> tuple:
    """Map a user-friendly model alias to (model_name, api_key_label)."""
    alias = (alias or "google").lower()
    model_name = LLM_MODEL_MAP.get(alias, LLM_MODEL_MAP["google"])
    api_key_label = "OPENAI_API_KEY" if "gpt" in model_name else "GEMINI_API_KEY"
    return model_name, api_key_label


app = FastAPI(title="Myers Podcast")

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp_audio")
os.makedirs(TEMP_DIR, exist_ok=True)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

@app.get("/")
def root():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return {"service": "Myers Podcast", "status": "running", "docs": "/docs"}

@app.post("/auth")
def check_auth(data: dict):
    """Validate credentials without generating anything. Returns auth_required flag."""
    if not APP_USERS:
        return {"authenticated": True, "auth_required": False, "user": "anonymous"}
    username = data.get("user", "")
    password = data.get("password", "")
    if not username or not password:
        raise HTTPException(status_code=401, detail="Authentication required")
    expected = APP_USERS.get(username)
    if expected is None or not secrets.compare_digest(password, expected):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"authenticated": True, "auth_required": True, "user": username}


@app.get("/auth/status")
def auth_status():
    """Check whether authentication is required (no credentials needed)."""
    return {"auth_required": bool(APP_USERS)}



@app.post("/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """Upload files (PDF, TXT, images) for podcast generation."""
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
        dest = os.path.join(UPLOAD_DIR, safe_name)
        with open(dest, "wb") as out:
            out.write(content)
        uploaded.append({"id": safe_name, "name": f.filename, "size": len(content)})
    return {"files": uploaded}


@app.get("/files")
def list_uploaded_files():
    """List all uploaded files."""
    files = []
    for name in sorted(os.listdir(UPLOAD_DIR)):
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.isfile(path):
            files.append({"id": name, "size": os.path.getsize(path)})
    return {"files": files}


@app.delete("/files/{file_id}")
def delete_uploaded_file(file_id: str):
    """Delete an uploaded file."""
    safe = Path(file_id).name  # prevent path traversal
    path = os.path.join(UPLOAD_DIR, safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(path)
    return {"deleted": safe}


@app.post("/generate")
def generate_podcast_endpoint(
    data: dict,
    x_app_password: str = Header(default="", alias="X-App-Password"),
):
    try:
        # --- Authentication ---
        username = _check_password(data, x_app_password)
        logger.info("Request from user: %s", username)

        # --- Load base configuration ---
        base_config = load_base_config()

        # --- TTS model and voice resolution ---
        tts_model = data.get('tts_model', base_config.get('text_to_speech', {}).get('default_tts_model', 'openai'))
        tts_base_config = base_config.get('text_to_speech', {}).get(tts_model, {})
        voices = data.get('voices', {})
        default_voices = tts_base_config.get('default_voices', {})

        # --- LLM model resolution ---
        llm_model_name, api_key_label = _resolve_llm(data.get('llm_model'))

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
        urls = list(data.get('urls', []))
        image_paths = []
        text_input = data.get('text', '')
        for fid in data.get('file_ids', []):
            safe = Path(fid).name
            fpath = os.path.join(UPLOAD_DIR, safe)
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
        raise  # Re-raise auth/validation errors as-is
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
