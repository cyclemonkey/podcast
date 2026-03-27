"""
FastAPI implementation for Myers Podcast generation service.
Authentication via OIDC/OAuth2 against Authentik with session cookies.
"""

import asyncio
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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

# ── Server-level API keys (fallback) ─────────────────────────────────────────
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")

# ── Authentik / OIDC config ───────────────────────────────────────────────────
AUTHENTIK_URL           = os.getenv("AUTHENTIK_URL", "").rstrip("/")
AUTHENTIK_CLIENT_ID     = os.getenv("AUTHENTIK_CLIENT_ID", "")
AUTHENTIK_CLIENT_SECRET = os.getenv("AUTHENTIK_CLIENT_SECRET", "")
AUTHENTIK_SLUG          = os.getenv("AUTHENTIK_SLUG", "")
APP_URL                 = os.getenv("APP_URL", "").rstrip("/")
SESSION_SECRET          = os.getenv("SESSION_SECRET", "change-me-in-production")

LLM_MODEL_MAP = {
    "google": "gemini-2.5-flash",
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
}

_executor = ThreadPoolExecutor(max_workers=2)


# ── Config helpers ────────────────────────────────────────────────────────────

def load_base_config() -> Dict[Any, Any]:
    config_path = Path(__file__).parent.parent / "conversation_config.yaml"
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning("Could not load base config: %s", e)
        return {}


def merge_configs(base_config: dict, user_config: dict) -> dict:
    merged = base_config.copy()
    if "text_to_speech" in merged and "text_to_speech" in user_config:
        merged["text_to_speech"].update(user_config.get("text_to_speech", {}))
    for key, value in user_config.items():
        if key != "text_to_speech" and value is not None:
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


def _user_podcasts_dir(username: str) -> str:
    path = os.path.join(_user_dir(username), "podcasts")
    os.makedirs(path, exist_ok=True)
    return path


def _load_profile(username: str) -> dict:
    path = os.path.join(_user_dir(username), "settings.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_profile(username: str, data: dict) -> None:
    path = os.path.join(_user_dir(username), "settings.json")
    with open(path, "w") as f:
        json.dump(data, f)


def _load_resources(username: str) -> dict:
    path = os.path.join(_user_dir(username), "resources.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"urls": [], "texts": []}


def _save_resources(username: str, data: dict) -> None:
    path = os.path.join(_user_dir(username), "resources.json")
    with open(path, "w") as f:
        json.dump(data, f)


def _load_jobs(username: str) -> list:
    path = os.path.join(_user_dir(username), "jobs.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_jobs(username: str, jobs: list) -> None:
    path = os.path.join(_user_dir(username), "jobs.json")
    with open(path, "w") as f:
        json.dump(jobs, f)


def _update_job(username: str, job_id: str, updates: dict) -> None:
    jobs = _load_jobs(username)
    for job in jobs:
        if job["id"] == job_id:
            job.update(updates)
            break
    _save_jobs(username, jobs)


def _session_user(request: Request) -> str:
    return request.session.get("username", "")


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Myers Podcast")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=APP_URL.startswith("https://") if APP_URL else False,
)

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

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_FILE_SIZE = 20 * 1024 * 1024

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


# ── Background generation ─────────────────────────────────────────────────────

def _do_generate_sync(username: str, job_id: str, gen_data: dict) -> None:
    """Blocking generation — runs in thread pool executor."""
    try:
        base_config = load_base_config()

        tts_model       = gen_data.get("tts_model", "edge")
        tts_base_config = base_config.get("text_to_speech", {}).get(tts_model, {})
        voices          = gen_data.get("voices", {})
        default_voices  = tts_base_config.get("default_voices", {})

        llm_model_name, api_key_label = _resolve_llm(gen_data.get("llm_model"))

        profile        = _load_profile(username)
        gemini_key     = (gen_data.get("user_gemini_api_key")     or profile.get("gemini_key")     or GEMINI_API_KEY).strip()
        openai_key     = (gen_data.get("user_openai_api_key")     or profile.get("openai_key")     or OPENAI_API_KEY).strip()
        elevenlabs_key = (gen_data.get("user_elevenlabs_api_key") or profile.get("elevenlabs_key") or ELEVENLABS_API_KEY).strip()
        if gemini_key:     os.environ["GEMINI_API_KEY"]     = gemini_key
        if openai_key:     os.environ["OPENAI_API_KEY"]     = openai_key
        if elevenlabs_key: os.environ["ELEVENLABS_API_KEY"] = elevenlabs_key

        user_config = {
            "creativity":           float(gen_data.get("creativity", base_config.get("creativity", 0.7))),
            "conversation_style":   gen_data.get("conversation_style",  base_config.get("conversation_style", [])),
            "roles_person1":        gen_data.get("roles_person1",        base_config.get("roles_person1")),
            "roles_person2":        gen_data.get("roles_person2",        base_config.get("roles_person2")),
            "dialogue_structure":   gen_data.get("dialogue_structure",   base_config.get("dialogue_structure", [])),
            "podcast_name":         gen_data.get("name",                 base_config.get("podcast_name")),
            "podcast_tagline":      gen_data.get("tagline",              base_config.get("podcast_tagline")),
            "output_language":      gen_data.get("output_language",      base_config.get("output_language", "English")),
            "user_instructions":    gen_data.get("user_instructions",    base_config.get("user_instructions", "")),
            "engagement_techniques": gen_data.get("engagement_techniques", base_config.get("engagement_techniques", [])),
            "text_to_speech": {
                "default_tts_model": tts_model,
                "model":             tts_base_config.get("model"),
                "default_voices": {
                    "question": voices.get("question", default_voices.get("question")),
                    "answer":   voices.get("answer",   default_voices.get("answer")),
                },
            },
        }

        conversation_config = merge_configs(base_config, user_config)

        upload_dir  = _user_upload_dir(username)
        urls        = list(gen_data.get("urls", []))
        image_paths = []
        text_input  = gen_data.get("text", "")

        for fid in gen_data.get("file_ids", []):
            safe  = Path(fid).name
            fpath = os.path.join(upload_dir, safe)
            if not os.path.isfile(fpath):
                raise FileNotFoundError(f"Uploaded file not found: {fid}")
            ext = Path(fpath).suffix.lower()
            if ext == ".pdf":
                urls.append(fpath)
            elif ext == ".txt":
                text_input += "\n" + Path(fpath).read_text(errors="replace")
            elif ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                image_paths.append(fpath)

        gen_kwargs: dict = dict(
            conversation_config=conversation_config,
            tts_model=tts_model,
            longform=bool(gen_data.get("is_long_form", False)),
            llm_model_name=llm_model_name,
            api_key_label=api_key_label,
        )
        if urls:               gen_kwargs["urls"]        = urls
        if image_paths:        gen_kwargs["image_paths"] = image_paths
        if text_input.strip(): gen_kwargs["text"]        = text_input.strip()

        result = generate_podcast(**gen_kwargs)

        if isinstance(result, str) and os.path.isfile(result):
            src_path = result
        elif hasattr(result, "audio_path") and os.path.isfile(result.audio_path):
            src_path = result.audio_path
        else:
            raise RuntimeError(f"Unexpected generate_podcast result: {result!r}")

        audio_filename = f"{job_id}.mp3"
        dest_path      = os.path.join(_user_podcasts_dir(username), audio_filename)
        shutil.copy2(src_path, dest_path)

        file_size = os.path.getsize(dest_path)
        if file_size == 0:
            raise RuntimeError("Generated audio file is empty (0 bytes)")

        _update_job(username, job_id, {
            "status":       "done",
            "audio_file":   audio_filename,
            "file_size":    file_size,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Job %s done for %s — %d bytes", job_id, username, file_size)

    except Exception as e:
        logger.exception("Job %s failed for %s: %s", job_id, username, e)
        _update_job(username, job_id, {
            "status":       "failed",
            "error":        str(e),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })


async def _bg_generate(username: str, job_id: str, gen_data: dict) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_executor, _do_generate_sync, username, job_id, gen_data)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
async def login(request: Request):
    if not AUTHENTIK_CLIENT_ID:
        request.session["username"] = "anonymous"
        request.session["name"]     = "Anonymous"
        request.session["email"]    = ""
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


# ── User API routes ───────────────────────────────────────────────────────────

@app.get("/me")
async def me(request: Request):
    username = _session_user(request)
    return {
        "username":    username,
        "name":        request.session.get("name", ""),
        "email":       request.session.get("email", ""),
        "logout_url":  "/logout",
        "profile_url": "/user/profile",
    }


@app.get("/profile")
async def get_profile(request: Request):
    username = _session_user(request) or "anonymous"
    profile  = _load_profile(username)
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
    username = _session_user(request) or "anonymous"
    profile  = _load_profile(username)
    for field in ("gemini_key", "openai_key", "elevenlabs_key"):
        if field in data:
            val = str(data[field]).strip()
            if val:
                profile[field] = val
            else:
                profile.pop(field, None)
    _save_profile(username, profile)
    return {"saved": True}


# ── Resource routes ───────────────────────────────────────────────────────────

@app.get("/resources")
async def list_resources(request: Request):
    username = _session_user(request) or "anonymous"
    return _load_resources(username)


@app.post("/resources/url")
async def add_resource_url(request: Request, data: dict):
    username = _session_user(request) or "anonymous"
    url = data.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL required")
    resources = _load_resources(username)
    entry = {
        "id":       uuid.uuid4().hex[:12],
        "url":      url,
        "label":    data.get("label", "").strip(),
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    resources["urls"].append(entry)
    _save_resources(username, resources)
    return entry


@app.delete("/resources/url/{rid}")
async def delete_resource_url(rid: str, request: Request):
    username  = _session_user(request) or "anonymous"
    resources = _load_resources(username)
    resources["urls"] = [u for u in resources["urls"] if u["id"] != rid]
    _save_resources(username, resources)
    return {"deleted": rid}


@app.post("/resources/text")
async def add_resource_text(request: Request, data: dict):
    username = _session_user(request) or "anonymous"
    content  = data.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content required")
    resources = _load_resources(username)
    entry = {
        "id":       uuid.uuid4().hex[:12],
        "content":  content,
        "label":    data.get("label", "").strip(),
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    resources["texts"].append(entry)
    _save_resources(username, resources)
    return entry


@app.delete("/resources/text/{rid}")
async def delete_resource_text(rid: str, request: Request):
    username  = _session_user(request) or "anonymous"
    resources = _load_resources(username)
    resources["texts"] = [t for t in resources["texts"] if t["id"] != rid]
    _save_resources(username, resources)
    return {"deleted": rid}


# ── File routes ───────────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_files(request: Request, files: List[UploadFile] = File(...)):
    username   = _session_user(request) or "anonymous"
    upload_dir = _user_upload_dir(username)
    uploaded   = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")
        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"File too large: {f.filename} (max 20 MB)")
        file_id   = uuid.uuid4().hex[:12]
        safe_name = f"{file_id}{ext}"
        dest      = os.path.join(upload_dir, safe_name)
        with open(dest, "wb") as out:
            out.write(content)
        with open(dest + ".meta", "w") as mf:
            json.dump({"name": f.filename}, mf)
        uploaded.append({"id": safe_name, "name": f.filename, "size": len(content)})
    return {"files": uploaded}


@app.get("/files")
async def list_uploaded_files(request: Request):
    username   = _session_user(request) or "anonymous"
    upload_dir = _user_upload_dir(username)
    files      = []
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
    username   = _session_user(request) or "anonymous"
    upload_dir = _user_upload_dir(username)
    safe       = Path(file_id).name
    path       = os.path.join(upload_dir, safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(path)
    meta = path + ".meta"
    if os.path.exists(meta):
        os.remove(meta)
    return {"deleted": safe}


# ── Generation routes ─────────────────────────────────────────────────────────

@app.post("/generate")
async def generate_podcast_endpoint(request: Request, data: dict):
    username = _session_user(request) or "anonymous"
    job_id   = uuid.uuid4().hex[:16]

    urls     = data.get("urls", [])
    file_ids = data.get("file_ids", [])
    text     = data.get("text", "")
    if urls:
        title = urls[0][:80]
    elif file_ids:
        title = f"{len(file_ids)} file(s)"
    elif text:
        title = text[:80].replace("\n", " ")
    else:
        title = "Podcast"

    jobs = _load_jobs(username)
    jobs.insert(0, {
        "id":         job_id,
        "status":     "generating",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "title":      title,
        "audio_file": None,
        "error":      None,
    })
    _save_jobs(username, jobs)

    asyncio.create_task(_bg_generate(username, job_id, dict(data)))
    return {"job_id": job_id}


@app.get("/jobs")
async def list_jobs(request: Request):
    username = _session_user(request) or "anonymous"
    return {"jobs": _load_jobs(username)}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    username = _session_user(request) or "anonymous"
    job      = next((j for j in _load_jobs(username) if j["id"] == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str, request: Request):
    username = _session_user(request) or "anonymous"
    jobs     = _load_jobs(username)
    job      = next((j for j in jobs if j["id"] == job_id), None)
    if job and job.get("audio_file"):
        audio_path = os.path.join(_user_podcasts_dir(username), job["audio_file"])
        if os.path.isfile(audio_path):
            os.remove(audio_path)
    _save_jobs(username, [j for j in jobs if j["id"] != job_id])
    return {"deleted": job_id}


@app.get("/audio/{job_id}")
async def serve_audio(job_id: str, request: Request):
    username = _session_user(request) or "anonymous"
    job      = next((j for j in _load_jobs(username) if j["id"] == job_id), None)
    if not job or job.get("status") != "done" or not job.get("audio_file"):
        raise HTTPException(status_code=404, detail="Audio not found")
    audio_path = os.path.join(_user_podcasts_dir(username), job["audio_file"])
    if not os.path.isfile(audio_path):
        raise HTTPException(status_code=404, detail="Audio file not found on disk")
    return FileResponse(
        audio_path,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'inline; filename="{job_id}.mp3"'},
    )


@app.get("/health")
def healthcheck():
    return {"status": "healthy"}


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host=host, port=port)
