"""
FastAPI implementation for Podcast generation service.
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

# ── Admin users ──────────────────────────────────────────────────────────────
ADMIN_USERS = {u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()}

# ── Authentik / OIDC config ───────────────────────────────────────────────────
AUTHENTIK_URL           = os.getenv("AUTHENTIK_URL", "").rstrip("/")
AUTHENTIK_CLIENT_ID     = os.getenv("AUTHENTIK_CLIENT_ID", "")
AUTHENTIK_CLIENT_SECRET = os.getenv("AUTHENTIK_CLIENT_SECRET", "")
AUTHENTIK_SLUG          = os.getenv("AUTHENTIK_SLUG", "")
APP_URL                 = os.getenv("APP_URL", "").rstrip("/")
SESSION_SECRET          = os.getenv("SESSION_SECRET", "change-me-in-production")

GEMINI_LLM_MODEL   = os.getenv("GEMINI_LLM_MODEL", "gemini-2.5-flash")
OPENAI_LLM_MODEL   = os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini")
DEEPSEEK_LLM_MODEL = os.getenv("DEEPSEEK_LLM_MODEL", "deepseek-chat")
DEEPSEEK_API_KEY   = os.getenv("DEEPSEEK_API_KEY", "")

LLM_MODEL_MAP = {
    "google":   GEMINI_LLM_MODEL,
    "gemini":   GEMINI_LLM_MODEL,
    "openai":   OPENAI_LLM_MODEL,
    "deepseek": DEEPSEEK_LLM_MODEL,
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
    if "deepseek" in model_name:
        api_key_label = "DEEPSEEK_API_KEY"
    elif "gpt" in model_name:
        api_key_label = "OPENAI_API_KEY"
    else:
        api_key_label = "GEMINI_API_KEY"
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


def _load_projects(username: str) -> list:
    path = os.path.join(_user_dir(username), "projects.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_projects(username: str, projects: list) -> None:
    path = os.path.join(_user_dir(username), "projects.json")
    with open(path, "w") as f:
        json.dump(projects, f)


def _load_share_tokens() -> dict:
    path = os.path.join(USER_DATA_DIR, "share_tokens.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_share_tokens(tokens: dict) -> None:
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    path = os.path.join(USER_DATA_DIR, "share_tokens.json")
    with open(path, "w") as f:
        json.dump(tokens, f)


def _create_share_token(username: str, job_id: str) -> str:
    token = uuid.uuid4().hex
    tokens = _load_share_tokens()
    tokens[token] = {"username": username, "job_id": job_id}
    _save_share_tokens(tokens)
    return token


def _session_user(request: Request) -> str:
    return request.session.get("username", "")


def _is_admin(request: Request) -> bool:
    username = _session_user(request)
    # If no ADMIN_USERS configured, anonymous users get admin access (solo setup)
    if not ADMIN_USERS:
        return True
    return username in ADMIN_USERS


def _load_admin_keys() -> dict:
    """Load API keys set by an admin user (stored in _admin/settings.json)."""
    path = os.path.join(USER_DATA_DIR, "_admin", "settings.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_admin_keys(data: dict) -> None:
    dir_path = os.path.join(USER_DATA_DIR, "_admin")
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, "settings.json")
    with open(path, "w") as f:
        json.dump(data, f)


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Podcast")

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
MAX_FILE_SIZE = 120 * 1024 * 1024

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "180"))  # 6 months


# ── Automatic cleanup of old files and episodes ─────────────────────────────

def _run_cleanup() -> None:
    """Delete files and completed episodes older than RETENTION_DAYS unless marked keep."""
    cutoff = datetime.now(timezone.utc).timestamp() - (RETENTION_DAYS * 86400)
    if not os.path.isdir(USER_DATA_DIR):
        return
    tokens = _load_share_tokens()
    tokens_changed = False
    for username_dir in os.listdir(USER_DATA_DIR):
        user_path = os.path.join(USER_DATA_DIR, username_dir)
        if not os.path.isdir(user_path) or username_dir.startswith("_"):
            continue

        # Clean old episodes
        jobs_path = os.path.join(user_path, "jobs.json")
        if os.path.isfile(jobs_path):
            try:
                with open(jobs_path) as f:
                    jobs = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                jobs = []
            kept_jobs = []
            for job in jobs:
                if job.get("keep"):
                    kept_jobs.append(job)
                    continue
                ts = job.get("completed_at") or job.get("created_at", "")
                try:
                    job_time = datetime.fromisoformat(ts).timestamp()
                except (ValueError, TypeError):
                    kept_jobs.append(job)
                    continue
                if job_time >= cutoff:
                    kept_jobs.append(job)
                    continue
                # Expired — delete audio file
                if job.get("audio_file"):
                    audio = os.path.join(user_path, "podcasts", job["audio_file"])
                    if os.path.isfile(audio):
                        os.remove(audio)
                # Remove share token
                token = job.get("share_token")
                if token and token in tokens:
                    del tokens[token]
                    tokens_changed = True
                logger.info("Cleanup: removed expired episode %s for %s", job.get("id"), username_dir)
            if len(kept_jobs) != len(jobs):
                with open(jobs_path, "w") as f:
                    json.dump(kept_jobs, f)

        # Clean old uploaded files
        files_dir = os.path.join(user_path, "files")
        if os.path.isdir(files_dir):
            for fname in os.listdir(files_dir):
                if fname.endswith(".meta"):
                    continue
                fpath = os.path.join(files_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                # Check keep flag
                meta_path = fpath + ".meta"
                try:
                    with open(meta_path) as mf:
                        meta = json.load(mf)
                    if meta.get("keep"):
                        continue
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                # Check file age
                file_mtime = os.path.getmtime(fpath)
                if file_mtime >= cutoff:
                    continue
                os.remove(fpath)
                if os.path.exists(meta_path):
                    os.remove(meta_path)
                logger.info("Cleanup: removed expired file %s for %s", fname, username_dir)

    if tokens_changed:
        _save_share_tokens(tokens)


@app.on_event("startup")
async def startup_cleanup():
    """Run cleanup in background thread on startup."""
    import threading
    threading.Thread(target=_run_cleanup, daemon=True).start()


# ── Background generation ─────────────────────────────────────────────────────

def _friendly_error(exc: Exception) -> str:
    """Convert raw API/library exceptions into short, user-readable messages."""
    msg = str(exc)
    low = msg.lower()

    # Rate limits
    if "rate" in low and "limit" in low:
        if "too large" in low or "requested" in low:
            return "The content is too large for the selected AI model. Try using fewer or smaller sources, or switch to Gemini which supports larger inputs."
        return "Rate limit reached. Please wait a minute and try again."

    # Quota / billing
    if "quota" in low or "exceeded" in low or "billing" in low:
        return "API quota exceeded. Please wait a few minutes and try again, or ask an admin to check the API plan."

    # Auth / key errors
    if "invalid" in low and ("key" in low or "api" in low or "auth" in low):
        return "Invalid API key. Ask an admin to check the API key configuration."

    # Timeout
    if "timeout" in low or "timed out" in low:
        return "The request timed out. Try with less content or try again later."

    # Keep short messages as-is, truncate very long ones
    if len(msg) > 200:
        return msg[:200] + "…"
    return msg


def _do_generate_sync(username: str, job_id: str, gen_data: dict) -> None:
    """Blocking generation — runs in thread pool executor."""
    try:
        base_config = load_base_config()

        tts_model       = gen_data.get("tts_model", "edge")
        tts_base_config = base_config.get("text_to_speech", {}).get(tts_model, {})
        voices          = gen_data.get("voices", {})
        default_voices  = tts_base_config.get("default_voices", {})

        llm_model_name, api_key_label = _resolve_llm(gen_data.get("llm_model"))

        admin_keys     = _load_admin_keys()
        gemini_key     = (admin_keys.get("gemini_key")     or GEMINI_API_KEY).strip()
        openai_key     = (admin_keys.get("openai_key")     or OPENAI_API_KEY).strip()
        elevenlabs_key = (admin_keys.get("elevenlabs_key") or ELEVENLABS_API_KEY).strip()
        deepseek_key   = (admin_keys.get("deepseek_key")   or DEEPSEEK_API_KEY).strip()

        # Pre-flight: verify the required LLM key is present
        if api_key_label == "GEMINI_API_KEY" and not gemini_key:
            raise ValueError("Gemini LLM selected but no Gemini API key is set. Ask an admin to configure API keys.")
        if api_key_label == "OPENAI_API_KEY" and not openai_key:
            raise ValueError("OpenAI LLM selected but no OpenAI API key is set. Ask an admin to configure API keys.")
        if api_key_label == "DEEPSEEK_API_KEY" and not deepseek_key:
            raise ValueError("DeepSeek LLM selected but no DeepSeek API key is set. Ask an admin to configure API keys.")
        if tts_model in ("openai",) and not openai_key:
            raise ValueError("OpenAI TTS selected but no OpenAI API key is set. Ask an admin to configure API keys.")
        if tts_model in ("elevenlabs",) and not elevenlabs_key:
            raise ValueError("ElevenLabs TTS selected but no ElevenLabs API key is set. Ask an admin to configure API keys.")

        if gemini_key:     os.environ["GEMINI_API_KEY"]     = gemini_key
        if openai_key:     os.environ["OPENAI_API_KEY"]     = openai_key
        if elevenlabs_key: os.environ["ELEVENLABS_API_KEY"] = elevenlabs_key
        if deepseek_key:   os.environ["DEEPSEEK_API_KEY"]   = deepseek_key

        # Episode length → word count instruction
        LENGTH_WORDS = {"5": 650, "10": 1300, "15": 1950, "20": 2600, "30": 3900}
        episode_mins = str(gen_data.get("episode_length", "10"))
        target_words = LENGTH_WORDS.get(episode_mins, 1300)
        base_instructions = gen_data.get("user_instructions", base_config.get("user_instructions", ""))
        length_instruction = f"Target approximately {target_words} words total in the conversation (about {episode_mins} minutes of audio)."
        # Inject selected topics as focus areas
        topics = gen_data.get("topics", [])
        topic_instruction = ""
        if topics:
            topic_list = "; ".join(t if isinstance(t, str) else t.get("title", "") for t in topics)
            topic_instruction = f"Focus the episode on these topics: {topic_list}."
        combined_instructions = f"{length_instruction} {topic_instruction} {base_instructions}".strip()

        user_config = {
            "creativity":           float(gen_data.get("creativity", base_config.get("creativity", 0.7))),
            "conversation_style":   gen_data.get("conversation_style",  base_config.get("conversation_style", [])),
            "roles_person1":        gen_data.get("roles_person1",        base_config.get("roles_person1")),
            "roles_person2":        gen_data.get("roles_person2",        base_config.get("roles_person2")),
            "dialogue_structure":   gen_data.get("dialogue_structure",   base_config.get("dialogue_structure", [])),
            "podcast_name":         gen_data.get("name",                 base_config.get("podcast_name")),
            "podcast_tagline":      gen_data.get("tagline",              base_config.get("podcast_tagline")),
            "output_language":      gen_data.get("output_language",      base_config.get("output_language", "English")),
            "user_instructions":    combined_instructions,
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

        # Truncate text to avoid exceeding LLM token limits (~4 chars per token).
        # OpenAI gpt-4o-mini: 128k context, 200k TPM → keep under ~100k chars.
        # DeepSeek: 64k context → keep under ~50k chars.
        # Gemini 2.5 Flash: 1M context → 400k chars is safe.
        llm_alias = (gen_data.get("llm_model") or "").lower()
        if "deepseek" in llm_alias:
            max_input_chars = 50_000
        elif "openai" in llm_alias:
            max_input_chars = 100_000
        else:
            max_input_chars = 400_000
        if text_input and len(text_input) > max_input_chars:
            text_input = text_input[:max_input_chars]
            logger.warning("Job %s: text input truncated to %d chars", job_id, max_input_chars)

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
        if file_size < 5000:
            raise RuntimeError(
                f"Generated audio appears empty or corrupt ({file_size} bytes). "
                "Check that your API key is valid and the selected content is not empty."
            )

        share_token = _create_share_token(username, job_id)
        _update_job(username, job_id, {
            "status":       "done",
            "audio_file":   audio_filename,
            "file_size":    file_size,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "share_token":  share_token,
        })
        logger.info("Job %s done for %s — %d bytes", job_id, username, file_size)

    except Exception as e:
        logger.exception("Job %s failed for %s: %s", job_id, username, e)
        _update_job(username, job_id, {
            "status":       "failed",
            "error":        _friendly_error(e),
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


# ── Model discovery ───────────────────────────────────────────────────────────

@app.get("/gemini-models")
async def gemini_models(request: Request):
    """Return available Gemini text-generation models for the user's API key."""
    username = _session_user(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    profile  = _load_profile(username)
    api_key  = (profile.get("gemini_key") or GEMINI_API_KEY).strip()
    if not api_key:
        return {"models": [], "error": "No Gemini API key set"}
    try:
        import urllib.request, json as _json
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = _json.loads(resp.read())
        models = [
            m["name"].replace("models/", "")
            for m in data.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
            and "flash" in m["name"].lower() or "pro" in m["name"].lower()
        ]
        # sort: latest first (simple lexicographic works for gemini-X.Y-* naming)
        models.sort(reverse=True)
        return {"models": models, "current": GEMINI_LLM_MODEL}
    except Exception as e:
        return {"models": [], "error": str(e), "current": GEMINI_LLM_MODEL}


# ── User API routes ───────────────────────────────────────────────────────────

@app.get("/me")
async def me(request: Request):
    username = _session_user(request)
    return {
        "username":    username,
        "name":        request.session.get("name", ""),
        "email":       request.session.get("email", ""),
        "is_admin":    _is_admin(request),
        "logout_url":  "/logout",
        "profile_url": "/user/profile",
    }


@app.get("/profile")
async def get_profile(request: Request):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    admin_keys = _load_admin_keys()
    return {
        "gemini_key":         admin_keys.get("gemini_key", ""),
        "openai_key":         admin_keys.get("openai_key", ""),
        "elevenlabs_key":     admin_keys.get("elevenlabs_key", ""),
        "deepseek_key":       admin_keys.get("deepseek_key", ""),
        "gemini_key_set":     bool(admin_keys.get("gemini_key")),
        "openai_key_set":     bool(admin_keys.get("openai_key")),
        "elevenlabs_key_set": bool(admin_keys.get("elevenlabs_key")),
        "deepseek_key_set":   bool(admin_keys.get("deepseek_key")),
    }


@app.post("/profile")
async def save_profile(request: Request, data: dict):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    admin_keys = _load_admin_keys()
    for field in ("gemini_key", "openai_key", "elevenlabs_key", "deepseek_key"):
        if field in data:
            val = str(data[field]).strip()
            if val:
                admin_keys[field] = val
            else:
                admin_keys.pop(field, None)
    _save_admin_keys(admin_keys)
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
            raise HTTPException(status_code=400, detail=f"File too large: {f.filename} (max 120 MB)")
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
            keep = False
            try:
                with open(path + ".meta") as mf:
                    meta = json.load(mf)
                    original_name = meta.get("name", name)
                    keep = meta.get("keep", False)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            files.append({"id": name, "name": original_name, "size": os.path.getsize(path), "keep": keep})
    return {"files": files}


@app.patch("/files/{file_id}")
async def patch_file(file_id: str, request: Request, data: dict):
    username   = _session_user(request) or "anonymous"
    upload_dir = _user_upload_dir(username)
    safe       = Path(file_id).name
    path       = os.path.join(upload_dir, safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    meta_path = path + ".meta"
    meta = {}
    try:
        with open(meta_path) as mf:
            meta = json.load(mf)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if "keep" in data:
        meta["keep"] = bool(data["keep"])
    with open(meta_path, "w") as mf:
        json.dump(meta, mf)
    return {"id": safe, "keep": meta.get("keep", False)}


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


# ── Topic routes ─────────────────────────────────────────────────────────────

def _load_topics(username: str) -> list:
    path = os.path.join(_user_dir(username), "topics.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_topics(username: str, topics: list) -> None:
    path = os.path.join(_user_dir(username), "topics.json")
    with open(path, "w") as f:
        json.dump(topics, f)


@app.get("/topics")
async def list_topics(request: Request):
    username = _session_user(request) or "anonymous"
    return {"topics": _load_topics(username)}


@app.delete("/topics/{topic_id}")
async def delete_topic(topic_id: str, request: Request):
    username = _session_user(request) or "anonymous"
    topics = _load_topics(username)
    _save_topics(username, [t for t in topics if t["id"] != topic_id])
    return {"deleted": topic_id}


@app.post("/suggest-topics")
async def suggest_topics(request: Request, data: dict):
    """Use the LLM to suggest podcast episode topics based on selected content."""
    username = _session_user(request) or "anonymous"

    urls     = data.get("urls", [])
    file_ids = data.get("file_ids", [])
    text     = data.get("text", "")
    llm_alias = data.get("llm_model", "gemini")

    # Extract content from sources
    from ..content_parser.content_extractor import ContentExtractor
    extractor = ContentExtractor()

    upload_dir = _user_upload_dir(username)
    content_parts = []

    for url in urls:
        try:
            content_parts.append(extractor.extract_content(url))
        except Exception as e:
            logger.warning("Failed to extract %s: %s", url, e)

    for fid in file_ids:
        safe = Path(fid).name
        fpath = os.path.join(upload_dir, safe)
        if not os.path.isfile(fpath):
            continue
        ext = Path(fpath).suffix.lower()
        if ext == ".pdf":
            try:
                content_parts.append(extractor.extract_content(fpath))
            except Exception as e:
                logger.warning("Failed to extract %s: %s", fid, e)
        elif ext == ".txt":
            content_parts.append(Path(fpath).read_text(errors="replace"))

    if text.strip():
        content_parts.append(text.strip())

    combined = "\n\n".join(content_parts)
    if not combined.strip():
        raise HTTPException(status_code=400, detail="No content could be extracted from the selected sources.")

    # Truncate for the topic suggestion call
    combined = combined[:50_000]

    # Use the LLM to generate topic suggestions
    llm_model_name, api_key_label = _resolve_llm(llm_alias)
    admin_keys = _load_admin_keys()
    gemini_key   = (admin_keys.get("gemini_key")   or GEMINI_API_KEY).strip()
    openai_key   = (admin_keys.get("openai_key")   or OPENAI_API_KEY).strip()
    deepseek_key = (admin_keys.get("deepseek_key") or DEEPSEEK_API_KEY).strip()

    if api_key_label == "GEMINI_API_KEY" and gemini_key:
        os.environ["GEMINI_API_KEY"] = gemini_key
    if api_key_label == "OPENAI_API_KEY" and openai_key:
        os.environ["OPENAI_API_KEY"] = openai_key
    if api_key_label == "DEEPSEEK_API_KEY" and deepseek_key:
        os.environ["DEEPSEEK_API_KEY"] = deepseek_key

    prompt = (
        "Based on the following content, suggest 8 specific and interesting podcast episode topics. "
        "Each topic should be a focused angle or theme that could make a compelling episode. "
        "Return ONLY a JSON array of objects, each with 'title' (short topic title, max 80 chars) "
        "and 'description' (1-2 sentence description of the episode angle). "
        "No markdown, no extra text — just the JSON array.\n\n"
        f"CONTENT:\n{combined}"
    )

    try:
        from ..content_generator import LLMBackend
        backend = LLMBackend(
            is_local=False,
            temperature=0.9,
            max_output_tokens=2048,
            model_name=llm_model_name,
            api_key_label=api_key_label,
        )
        response = backend.llm.invoke(prompt)
        raw = response.content if hasattr(response, "content") else str(response)

        # Parse JSON from response (handle markdown code fences)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        import re as _re
        # Find JSON array in the response
        match = _re.search(r'\[.*\]', raw, _re.DOTALL)
        if match:
            topics = json.loads(match.group())
        else:
            topics = json.loads(raw)

        # Validate and normalize
        result = []
        for t in topics[:10]:
            if isinstance(t, dict) and "title" in t:
                result.append({
                    "title": str(t["title"])[:80],
                    "description": str(t.get("description", ""))[:200],
                })
        return {"topics": result}

    except Exception as e:
        logger.exception("Topic suggestion failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to generate topic suggestions: {_friendly_error(e)}")


@app.post("/topics")
async def save_topic(request: Request, data: dict):
    """Save a topic for later use."""
    username = _session_user(request) or "anonymous"
    title = data.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Topic title required")
    topics = _load_topics(username)
    entry = {
        "id":          uuid.uuid4().hex[:12],
        "title":       title,
        "description": data.get("description", "").strip(),
        "added_at":    datetime.now(timezone.utc).isoformat(),
    }
    topics.append(entry)
    _save_topics(username, topics)
    return entry


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
        # Resolve actual file names from metadata
        upload_dir = _user_upload_dir(username)
        names = []
        for fid in file_ids:
            meta_path = os.path.join(upload_dir, Path(fid).name) + ".meta"
            try:
                with open(meta_path) as mf:
                    names.append(json.load(mf).get("name", fid))
            except (FileNotFoundError, json.JSONDecodeError):
                names.append(fid)
        title = ", ".join(names)[:80]
    elif text:
        title = text[:80].replace("\n", " ")
    else:
        title = "Podcast"

    # Snapshot of generation params for "next episode" feature
    gen_snapshot = {
        "urls":        urls,
        "file_ids":    file_ids,
        "text":        text,
        "tts_model":   data.get("tts_model"),
        "llm_model":   data.get("llm_model"),
        "voices":      data.get("voices"),
        "creativity":  data.get("creativity"),
        "episode_length": data.get("episode_length"),
        "is_long_form":   data.get("is_long_form"),
        "output_language": data.get("output_language"),
        "name":        data.get("name"),
        "tagline":     data.get("tagline"),
        "conversation_style":    data.get("conversation_style"),
        "engagement_techniques": data.get("engagement_techniques"),
        "topics":      data.get("topics"),
    }

    jobs = _load_jobs(username)
    jobs.insert(0, {
        "id":           job_id,
        "status":       "generating",
        "created_at":   datetime.now(timezone.utc).isoformat(),
        "title":        title,
        "description":  data.get("description", ""),
        "project_id":   data.get("project_id", ""),
        "audio_file":   None,
        "error":        None,
        "gen_snapshot": gen_snapshot,
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


@app.patch("/jobs/{job_id}")
async def patch_job(job_id: str, request: Request, data: dict):
    username = _session_user(request) or "anonymous"
    jobs = _load_jobs(username)
    job = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    for field in ("title", "description"):
        if field in data:
            job[field] = str(data[field])
    if "keep" in data:
        job["keep"] = bool(data["keep"])
    _save_jobs(username, jobs)
    return job


# ── Public share routes (no auth) ─────────────────────────────────────────────

@app.get("/public/audio/{token}")
async def public_audio(token: str):
    tokens = _load_share_tokens()
    entry  = tokens.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="Share link not found or expired")
    username = entry["username"]
    job_id   = entry["job_id"]
    job = next((j for j in _load_jobs(username) if j["id"] == job_id), None)
    if not job or job.get("status") != "done" or not job.get("audio_file"):
        raise HTTPException(status_code=404, detail="Audio not found")
    audio_path = os.path.join(_user_podcasts_dir(username), job["audio_file"])
    if not os.path.isfile(audio_path):
        raise HTTPException(status_code=404, detail="Audio file not found on disk")
    filename = (job.get("title") or job_id)[:60].replace("/", "-") + ".mp3"
    return FileResponse(
        audio_path,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ── Project routes ────────────────────────────────────────────────────────────

@app.get("/projects")
async def list_projects(request: Request):
    username = _session_user(request) or "anonymous"
    return {"projects": _load_projects(username)}


@app.post("/projects")
async def create_project(request: Request, data: dict):
    username = _session_user(request) or "anonymous"
    name = data.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name required")
    projects = _load_projects(username)
    project = {
        "id":          uuid.uuid4().hex[:12],
        "name":        name,
        "description": data.get("description", "").strip(),
        "created_at":  datetime.now(timezone.utc).isoformat(),
    }
    projects.append(project)
    _save_projects(username, projects)
    return project


@app.patch("/projects/{project_id}")
async def update_project(project_id: str, request: Request, data: dict):
    username = _session_user(request) or "anonymous"
    projects = _load_projects(username)
    project  = next((p for p in projects if p["id"] == project_id), None)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    for field in ("name", "description"):
        if field in data:
            project[field] = str(data[field]).strip()
    _save_projects(username, projects)
    return project


@app.delete("/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    username = _session_user(request) or "anonymous"
    projects = _load_projects(username)
    _save_projects(username, [p for p in projects if p["id"] != project_id])
    return {"deleted": project_id}


@app.get("/health")
def healthcheck():
    return {"status": "healthy"}


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host=host, port=port)
