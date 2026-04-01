Always commit directly to the main branch, never create feature branches.

# Podcast – Project Overview

A self-hosted, AI-powered podcast generator. Users provide content (URLs, PDFs, images, text) and the app produces a two-speaker audio podcast using LLMs for transcript generation and TTS providers for audio synthesis.

## Tech Stack

- **Backend**: Python 3.11+, FastAPI, file-based JSON storage (no database)
- **Frontend**: Vanilla HTML/CSS/JavaScript (no framework), dark theme
- **LLMs**: Google Gemini (default: `gemini-2.5-flash`), OpenAI (`gpt-4o-mini`)
- **TTS**: OpenAI, ElevenLabs, Microsoft Edge, Google Gemini, Gemini Multi-speaker
- **Auth**: OAuth2/OIDC via Authentik (falls back to anonymous if unconfigured)

## Key Files

| Path | Purpose |
|------|---------|
| `podcastfy/api/fast_app.py` | FastAPI app — all routes, auth, job queue |
| `podcastfy/content_generator.py` | LLM transcript generation (standard + long-form strategies) |
| `podcastfy/text_to_speech.py` | TTS orchestration |
| `podcastfy/tts/providers/` | One file per TTS provider |
| `podcastfy/conversation_config.yaml` | Default podcast name, voices, styles |
| `frontend/index.html` | Main UI — sources, options, projects, episode list |
| `frontend/profile.html` | User settings — API keys |

## Data Storage

All data lives under `user_data/{username}/`:
- `settings.json` — API keys and profile
- `resources.json` — saved URLs and text snippets
- `jobs.json` — generated episodes (status, audio file path, gen_snapshot)
- `projects.json` — project groupings
- `podcasts/` — MP3 files
- `files/` — uploaded files

Global: `user_data/share_tokens.json` — public share tokens

## API Endpoints (summary)

- `POST /generate` — start a generation job
- `GET /jobs` — list episodes; `PATCH /jobs/{id}` — edit title/description
- `GET /audio/{id}` — stream/download (authenticated)
- `GET /public/audio/{token}` — stream/download (public share link)
- `GET|POST|PATCH|DELETE /projects` — project CRUD
- `GET|POST /resources/url|text` — saved sources
- `POST /upload` — file upload

## Running Locally

```bash
pip install -e .
# Set env vars: OPENAI_API_KEY, GEMINI_API_KEY, etc.
uvicorn podcastfy.api.fast_app:app --reload --port 8080
```

Visit `http://localhost:8080`. Without Authentik configured, it logs in as `anonymous`.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | OpenAI LLM + TTS |
| `GEMINI_API_KEY` | Gemini LLM + TTS |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS |
| `AUTHENTIK_URL` | OIDC provider base URL |
| `AUTHENTIK_CLIENT_ID` | OAuth client ID |
| `AUTHENTIK_CLIENT_SECRET` | OAuth client secret |
| `AUTHENTIK_SLUG` | Authentik app slug |
| `APP_URL` | Public URL of this app (for OAuth callback) |
| `SESSION_SECRET` | Cookie signing secret |
| `USER_DATA_DIR` | Override for user data directory |
| `GEMINI_LLM_MODEL` | Override Gemini model (default: `gemini-2.5-flash`) |
| `OPENAI_LLM_MODEL` | Override OpenAI model (default: `gpt-4o-mini`) |

## Episode Generation Flow

1. User selects sources (URLs / files / text) and options
2. `POST /generate` creates a job record and starts a background thread
3. Content extractor fetches/parses sources
4. `ContentGenerator` calls LLM to produce a `<Person1>`/`<Person2>` transcript
5. `TextToSpeech` splits transcript by speaker, synthesises audio, merges to MP3
6. Job updated to `done`; share token created automatically
7. Frontend polls `/jobs/{id}` every 3 s until status changes

## Frontend Architecture

Single-page app (`index.html`) with sections:
- **User bar** — username, profile link, logout
- **Projects** — create/filter/delete project groups
- **Sources** — tabs for URLs, uploaded files, text snippets
- **Options** — TTS model, LLM, language, episode length, voices, creativity
- **Generate button**
- **My Podcasts** — episode list with audio player, share, edit, next-episode, delete

Each completed episode stores a `gen_snapshot` with all generation parameters so the "Generate Next Episode" button can reload them into the form.
