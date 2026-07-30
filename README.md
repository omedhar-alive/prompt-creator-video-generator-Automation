# AI Video Factory

An automation for marketing agencies that produces social video end to end: an AI generates the concept and prompt, an AI video model turns it into a reel, and every idea is logged to Google Sheets — which is fed back into the next generation so the system never repeats itself.

---

## What it does

- Takes a brand brief (name, description, product type, audience) plus optional product images
- Reads that brand's past concepts from Google Sheets and passes them in as an avoid-list
- Generates a new video concept and a video prompt with OpenAI, constrained to structured JSON
- Sends the prompt and product image to Runway to render a video, then trims the intro frame with ffmpeg
- Writes the concept, prompt, and video location back to Sheets so the next run can't repeat it

The Sheets round-trip is the point: the history is the memory, so concept #20 for a brand is aware of the previous 19.

## Pipeline

```
brand brief + images
      |
      +--> Google Sheets --> last 20 concepts for this brand (avoid-list)
      |
      v
   OpenAI --> concept + runway_prompt (validated JSON)
      |
      v
   Runway image-to-video --> mp4 --> ffmpeg trim
      |
      v
   Google Sheets <-- concept + prompt + video path
```

One call to `POST /pipeline/generate` runs the whole chain.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Health check |
| POST | `/pipeline/generate` | Full chain: ingest -> concept -> video -> log |
| POST | `/ingest` | Brand brief + images, returns the avoid-list |
| POST | `/concept/generate` | Concept + prompt only |
| POST | `/runway/generate` | Image -> video only |
| POST | `/chatkit/session` | Opens an OpenAI ChatKit session seeded with brand history |

## Failure handling

Silent failure is the main risk in a pipeline like this — an unreachable spreadsheet looks
identical to a brand with no history, which would quietly disable de-duplication. So:

- `history_available` reports whether the avoid-list was actually read
- `history_written` reports whether the new concept was recorded
- Runway calls retry up to 3 times with backoff; moderation timeouts are treated as retryable, other failures are not
- Polling is capped by `MAX_WAIT_SEC` and uses `await asyncio.sleep` so a long render doesn't block the event loop
- OpenAI output is parsed defensively and every required field is validated before use

## Stack

- **Backend:** Python, FastAPI, Pydantic
- **AI:** OpenAI (concept generation + ChatKit), Runway (image-to-video)
- **Data:** Google Sheets via gspread + service account
- **Media:** ffmpeg
- **Frontend:** React (Vite)

## Setup

**1. Install**

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

ffmpeg must be on your PATH (`brew install ffmpeg` on macOS).

**2. Environment** — create `.env` in the repo root:

```
OPENAI_API_KEY=...
RUNWAYML_API_SECRET=...
WORKFLOW_ID=...              # OpenAI ChatKit workflow, only needed for /chatkit/session
SPREADSHEET_NAME=concept_history
SHEET_NAME=                  # blank = first tab
OPENAI_MODEL=gpt-4.1-mini
TRIM_INTRO_SECONDS=0.35
```

**3. Google Sheets**

- Create a Google Cloud service account, enable the Sheets and Drive APIs, download the JSON key
- Save it as `app/google_credentials.json` (gitignored)
- Create a spreadsheet named to match `SPREADSHEET_NAME`
- Share that spreadsheet with the service account's `client_email` as an Editor — otherwise every read fails with "spreadsheet not found"

**4. Run**

```bash
uvicorn app.main:app --reload
```

API docs at `http://127.0.0.1:8000/docs`.

**5. Frontend** (optional)

```bash
cd ai-video-frontend
npm install
echo "VITE_API_BASE=http://127.0.0.1:8000" > .env
npm run dev
```

## Structure

```
app/main.py               all endpoints, pipeline orchestration, Sheets + OpenAI + Runway clients
requirements.txt
ai-video-frontend/src/    App.jsx (brand form), lib/chatkit.js (session helper)
uploads/<brand>/          saved product images (gitignored)
outputs/<brand>/          rendered mp4s (gitignored)
```

## Known limits

- Video generation requires a product image; text-only generation is not wired up yet
- Concepts are matched to a brand by name, so two brands with similar names can share history
- Renders are synchronous — a request can stay open for the length of a Runway job
- Output videos are stored on local disk, not object storage