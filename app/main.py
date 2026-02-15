print("RUNNING FILE:", __file__)

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from pathlib import Path
import os
import json
import time
from datetime import datetime

from dotenv import load_dotenv
from typing import Any, Dict, Optional
# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# OpenAI
from openai import OpenAI

# Runway
from runwayml import RunwayML, TaskFailedError

MAX_WAIT_SEC = 300
POLL_INTERVAL_SEC = 7
MAX_RETRIES = 2

# -----------------------------------------------------------------------------
# App + Env
# -----------------------------------------------------------------------------
app = FastAPI(title="AI Video Factory")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

project_root = Path(__file__).resolve().parents[1]  # repo root (parent of app/)
env_path = project_root / ".env"
if env_path.exists():
    load_dotenv(str(env_path))


@app.get("/")
async def health():
    return {"status": "ok"}


# -----------------------------------------------------------------------------
# Env helpers
# -----------------------------------------------------------------------------
def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise HTTPException(status_code=500, detail=f"Missing environment variable: {name}")
    return v


def get_openai_client() -> OpenAI:
    api_key = require_env("OPENAI_API_KEY")
    return OpenAI(api_key=api_key)


def get_runway_client() -> RunwayML:
    api_key = os.getenv("RUNWAYML_API_SECRET") or os.getenv("RUNWAY_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Missing RUNWAYML_API_SECRET (or RUNWAY_API_KEY) in .env")
    return RunwayML(api_key=api_key)


# -----------------------------------------------------------------------------
# Google Sheets helpers
# -----------------------------------------------------------------------------
def get_gspread_client() -> Optional[gspread.Client]:
    cred_path = project_root / "google_credentials.json"
    if not cred_path.exists():
        # If you prefer hard-fail, change this to raise HTTPException.
        return None
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = Credentials.from_service_account_file(str(cred_path), scopes=scopes)
        return gspread.authorize(creds)
    except Exception:
        return None


def _get_val_ci(row: Dict[str, Any], key: str) -> Any:
    key_lower = key.lower()
    for k in row.keys():
        if str(k).lower() == key_lower:
            return row.get(k)
    return None


def fetch_last_20_for_brand(brand_name: str) -> List[Dict[str, Any]]:
    """
    Reads spreadsheet & sheet from env or defaults:
      SPREADSHEET_NAME=concept_history
      SHEET_NAME=concept_history  (or Sheet1)
    Returns last 20 rows matching brand_name (case-insensitive).
    """
    brand_lower = (brand_name or "").strip().lower()
    if not brand_lower:
        return []

    client = get_gspread_client()
    if not client:
        return []

    spreadsheet_name = os.getenv("SPREADSHEET_NAME", "concept_history")
    sheet_name = os.getenv("SHEET_NAME", "")  # optional

    try:
        sh = client.open(spreadsheet_name)
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1
        records = ws.get_all_records()
        if not records:
            return []

        matches: List[Dict[str, Any]] = []

        # Prefer exact match on brand columns if present
        preferred_keys = {"brand", "brand_name", "Brand", "Brand Name", "BRAND"}

        for r in records:
            found_exact = False
            for k in r.keys():
                if k in preferred_keys:
                    v = r.get(k)
                    if isinstance(v, str) and v.strip().lower() == brand_lower:
                        matches.append(r)
                        found_exact = True
                        break
            if found_exact:
                continue

            # fallback: substring scan
            for v in r.values():
                if isinstance(v, str) and brand_lower in v.lower():
                    matches.append(r)
                    break

        return matches[-20:]
    except Exception:
        return []


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class AvoidItem(BaseModel):
    concept_title: str = ""
    hook_type: str = ""
    format: str = ""
    setting: str = ""
    camera_style: str = ""
    runway_prompt: str = ""
    notes: str = ""


class IngestResponse(BaseModel):
    user_id: str = "default"
    brand_name: str
    brand_description: str
    product_type: str
    target_audience: str
    extra_comments: Optional[str] = None
    avoid_list: List[Dict[str, Any]] = Field(default_factory=list)
    saved_images_local: List[str] = Field(default_factory=list)


class ConceptResponse(BaseModel):
    concept_title: str
    hook_type: str
    format: str
    setting: str
    camera_style: str
    runway_prompt: str
    notes: str = ""


class ConceptRequest(IngestResponse):
    """
    Same shape as ingest output.
    """


class ChatSessionRequest(BaseModel):
    user_id: str = "default"


class RunwayGenerateRequest(BaseModel):
    brand_name: str
    runway_prompt: str
    # Use an existing local path returned by ingest (or provide your own local file path)
    image_path_local: str
    # Optional overrides
    model: str = "gen3a_turbo"
    ratio: str = "1280:768"
    duration_seconds: int = 5


# -----------------------------------------------------------------------------
# Utility: Save uploads
# -----------------------------------------------------------------------------
async def save_uploads_locally(brand_name: str, images: Optional[List[UploadFile]]) -> List[str]:
    if not images:
        return []

    safe_brand = "".join([c if c.isalnum() else "_" for c in (brand_name or "brand")]).strip("_") or "brand"
    upload_dir = project_root / "uploads" / safe_brand
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: List[str] = []
    for i, up in enumerate(images[:6]):
        try:
            safe_name = Path(up.filename).name
            dest = upload_dir / f"{i}_{safe_name}"
            contents = await up.read()
            with open(dest, "wb") as f:
                f.write(contents)
            saved_paths.append(str(dest))
        except Exception:
            continue

    return saved_paths

# -----------------------------------------------------------------------------
# Internal pipeline functions (no FastAPI decorators)
# -----------------------------------------------------------------------------
async def do_ingest(
    brand_name: str,
    brand_description: str,
    product_type: str,
    target_audience: str,
    extra_comments: Optional[str],
    images: Optional[List[UploadFile]],
) -> IngestResponse:
    rows = fetch_last_20_for_brand(brand_name)
    saved_paths = await save_uploads_locally(brand_name, images)

    fields = ["concept_title", "hook_type", "format", "setting", "camera_style", "runway_prompt", "notes"]
    avoid_list: List[Dict[str, Any]] = []
    for r in rows:
        data = {f: (_get_val_ci(r, f) or "") for f in fields}
        try:
            item = AvoidItem(**data)
            avoid_list.append(item.model_dump())
        except Exception:
            continue

    return IngestResponse(
        user_id="default",
        brand_name=brand_name,
        brand_description=brand_description,
        product_type=product_type,
        target_audience=target_audience,
        extra_comments=extra_comments,
        avoid_list=avoid_list,
        saved_images_local=saved_paths,
    )


async def do_concept_generate(payload: ConceptRequest) -> ConceptResponse:
    # reuse your existing concept_generate body by calling it directly for now
    # (next step we can inline it if you want)
    return await concept_generate(payload)


async def do_runway_generate(payload: RunwayGenerateRequest) -> Dict[str, Any]:
    return await runway_generate(payload)


# -----------------------------------------------------------------------------
# Stage 3: Ingestion endpoint
# -----------------------------------------------------------------------------
@app.post("/ingest", response_model=IngestResponse)
async def ingest(
    request: Request,
    brand_name: str = Form(...),
    brand_description: str = Form(...),
    product_type: str = Form(...),
    target_audience: str = Form(...),
    extra_comments: Optional[str] = Form(None),
    images: Optional[List[UploadFile]] = File(None),
):
    rows = fetch_last_20_for_brand(brand_name)
    saved_paths = await save_uploads_locally(brand_name, images)

    fields = ["concept_title", "hook_type", "format", "setting", "camera_style", "runway_prompt", "notes"]
    avoid_list: List[Dict[str, Any]] = []
    for r in rows:
        data = {f: (_get_val_ci(r, f) or "") for f in fields}
        try:
            item = AvoidItem(**data)
            avoid_list.append(item.model_dump())
        except Exception:
            continue

    return IngestResponse(
        user_id="default",
        brand_name=brand_name,
        brand_description=brand_description,
        product_type=product_type,
        target_audience=target_audience,
        extra_comments=extra_comments,
        avoid_list=avoid_list,
        saved_images_local=saved_paths,
    )


# -----------------------------------------------------------------------------
# Stage 4: OpenAI concept generation (Responses API)
# -----------------------------------------------------------------------------
def _safe_json_loads(text: str) -> Optional[dict]:
    if not text or not isinstance(text, str):
        return None
    t = text.strip()
    try:
        return json.loads(t)
    except Exception:
        pass

    first = t.find("{")
    last = t.rfind("}")
    if first != -1 and last != -1 and last > first:
        chunk = t[first : last + 1]
        try:
            return json.loads(chunk)
        except Exception:
            return None
    return None


@app.post("/concept/generate", response_model=ConceptResponse)
async def concept_generate(req: ConceptRequest):
    client = get_openai_client()
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    system = (
        "You generate ONE short-form social video concept for a brand.\n"
        "Return ONLY valid JSON (no markdown, no commentary) with keys:\n"
        "concept_title, hook_type, format, setting, camera_style, runway_prompt, notes.\n"
        "Do NOT repeat or closely resemble anything in avoid_list.\n"
        "If images are provided, assume they are product hero shots and write prompts accordingly.\n"
    )

    payload = req.model_dump()
    user = json.dumps(payload, ensure_ascii=False)

    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )

    text = resp.output_text
    data = _safe_json_loads(text)

    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail=f"Model did not return valid JSON. Raw: {text[:500]}")

    required = ["concept_title", "hook_type", "format", "setting", "camera_style", "runway_prompt"]
    for k in required:
        if k not in data or not isinstance(data.get(k), str) or not data.get(k).strip():
            raise HTTPException(status_code=500, detail=f"Model JSON missing/invalid field: {k}")

    if "notes" not in data or not isinstance(data.get("notes"), str):
        data["notes"] = ""

    return ConceptResponse(**data)


# -----------------------------------------------------------------------------
# ChatKit: create session for Workflow (frontend uses client_secret)
# -----------------------------------------------------------------------------
@app.post("/chatkit/session")
async def chatkit_session(req: ChatSessionRequest):
    """
    Returns {session_id, client_secret} for your Workflow ID.
    Your frontend can use client_secret to chat with the workflow.
    """
    workflow_id = require_env("WORKFLOW_ID")
    client = get_openai_client()

    try:
        session = client.beta.chatkit.sessions.create(
            user=req.user_id,
            workflow={"id": workflow_id},
        )
        return {"session_id": session.id, "client_secret": session.client_secret}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Stage 5: Runway image-to-video (NO ngrok, uses ephemeral upload)
# -----------------------------------------------------------------------------

def _is_moderation_timeout(failure: Any) -> bool:
    try:
        s = (failure or "")
        if not isinstance(s, str):
            s = str(s)
        return "Timeout during moderation" in s or "moderation" in s.lower() and "timeout" in s.lower()
    except Exception:
        return False

@app.post("/runway/generate")
async def runway_generate(req: RunwayGenerateRequest):
    """
    Uses local image path -> Runway ephemeral upload -> image_to_video task.
    Retries on transient failures (ex: moderation timeout).
    Polls until SUCCEEDED/FAILED (with max wait).
    Downloads mp4 into outputs/<brand>/video_YYYYMMDD_HHMMSS.mp4
    """
    runway = get_runway_client()

    img_path = Path(req.image_path_local)
    if not img_path.exists():
        raise HTTPException(status_code=400, detail=f"image_path_local not found: {req.image_path_local}")

    safe_brand = "".join([c if c.isalnum() else "_" for c in (req.brand_name or "brand")]).strip("_") or "brand"
    output_dir = project_root / "outputs" / safe_brand
    output_dir.mkdir(parents=True, exist_ok=True)

    # Tuning knobs
    max_attempts = 3
    poll_interval_sec = 5
    max_wait_sec = 180  # total polling time per attempt (3 minutes)

    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            # 1) Ephemeral upload (do it per-attempt; URI can expire)
            upload_res = runway.uploads.create_ephemeral(file=img_path)
            prompt_image_uri = str(getattr(upload_res, "uri", upload_res))

            # 2) Create task
            task = runway.image_to_video.create(
                model=req.model,
                prompt_image=prompt_image_uri,
                prompt_text=req.runway_prompt,
                ratio=req.ratio,
                duration=req.duration_seconds,  # Runway expects "duration", your model field is duration_seconds
            )
            task_id = task.id

            # 3) Poll with timeout
            start_time = time.time()
            while True:
                task = runway.tasks.retrieve(task_id)
                status = getattr(task, "status", None)

                if status in ("SUCCEEDED", "FAILED"):
                    break

                if time.time() - start_time > MAX_WAIT_SEC:
                    return {
                       "task_id": task_id,
                        "status": "FAILED",
                        "failure": "Max wait time exceeded"
                    }
                
                time.sleep(POLL_INTERVAL_SEC)

            if task.status == "FAILED":
                failure = getattr(task, "failure", None) or "Unknown failure"
                # Treat moderation timeout as retryable
                msg = str(failure)
                if "Timeout during moderation" in msg or "moderation" in msg.lower():
                    raise Exception(f"Retryable failure: {failure}")
                # Non-retryable: return immediately
                return {"task_id": task_id, "status": "FAILED", "failure": failure, "attempt": attempt}

            # 4) Download output
            output = getattr(task, "output", None)
            if not output:
                return {"task_id": task_id, "status": "SUCCEEDED", "video_url": None, "video_file_path": None, "attempt": attempt}

            video_url = output[0] if isinstance(output, list) else output

            import requests
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            local_path = output_dir / f"video_{ts}.mp4"

            with requests.get(video_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)

            return {
                "task_id": task_id,
                "status": "SUCCEEDED",
                "video_url": video_url,
                "video_file_path": str(local_path),
                "attempt": attempt,
            }

        except TaskFailedError as e:
            # SDK-level failure wrapper
            last_error = getattr(e, "task_details", None) or str(e)

        except Exception as e:
            # Any other error (including our "retryable failure" exceptions)
            last_error = str(e)

        # Backoff before next attempt
        if attempt < max_attempts:
            time.sleep(5 * attempt)

    # If we got here, all attempts failed
    return {"status": "FAILED", "detail": last_error, "attempts": max_attempts}


@app.get("/outputs/file")
async def get_output_file(path: str):
    """
    Convenience endpoint to download a generated video by local path.
    Use with caution in production (path traversal risks).
    For local dev only.
    """
    p = Path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(p), filename=p.name)



@app.post("/pipeline/generate")
async def pipeline_generate(
    request: Request,
    brand_name: str = Form(...),
    brand_description: str = Form(...),
    product_type: str = Form(...),
    target_audience: str = Form(...),
    extra_comments: Optional[str] = Form(None),
    images: Optional[List[UploadFile]] = File(None),
) -> Dict[str, Any]:
    """
    One-shot pipeline:
    1) ingest (save images + build avoid_list from Sheets)
    2) concept (OpenAI)
    3) runway (optional, if image exists)
    """
    # 1) Ingest (call function directly)
    ingest_result: IngestResponse = await ingest(
        request=request,
        brand_name=brand_name,
        brand_description=brand_description,
        product_type=product_type,
        target_audience=target_audience,
        extra_comments=extra_comments,
        images=images,
    )

    # 2) Concept
    concept_result: ConceptResponse = await concept_generate(
        ConceptRequest(**ingest_result.model_dump())
    )

    # 3) Runway (optional)
    runway_result: Optional[Dict[str, Any]] = None
    if ingest_result.saved_images_local:
        try:
            runway_payload = RunwayGenerateRequest(
                brand_name=ingest_result.brand_name,
                runway_prompt=concept_result.runway_prompt,
                image_path_local=ingest_result.saved_images_local[0],
                model="gen3a_turbo",
                ratio="1280:768",
                duration_seconds=5,
            )
            runway_result = await runway_generate(runway_payload)
        except Exception as e:
            runway_result = {"status": "FAILED", "detail": str(e)}
    download_url = None
    if runway_result and runway_result.get("video_file_path"):
        download_url = (
        f"http://127.0.0.1:8000/outputs/file?path="
        f"{runway_result['video_file_path']}"
    )

    return {
        "ingest": ingest_result.model_dump(),
        "concept": concept_result.model_dump(),
        "runway": runway_result,
    }