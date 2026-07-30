from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any ,Union
from pathlib import Path
import asyncio
import os
import json
import time
import subprocess
from datetime import datetime

import requests
from dotenv import load_dotenv

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# OpenAI
from openai import OpenAI

# Runway
from runwayml import RunwayML, TaskFailedError

MAX_WAIT_SEC = 300
POLL_INTERVAL_SEC = 7


def trim_video_start(input_path: Path, seconds: float = 0.35) -> Path:
    """
    Trims the first `seconds` from a video using ffmpeg.
    Returns the new file path.
    """
    output_path = input_path.with_name(input_path.stem + f"_trim{int(seconds*1000)}ms" + input_path.suffix)

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(seconds),
        "-i", str(input_path),
        "-c", "copy",
        str(output_path),
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path
    except Exception:
        # fallback: re-encode if stream copy fails
        cmd = [
            "ffmpeg",
            "-y",
            "-ss", str(seconds),
            "-i", str(input_path),
            "-c:v", "libx264",
            "-crf", "20",
            "-preset", "veryfast",
            "-c:a", "aac",
            str(output_path),
        ]
        subprocess.run(cmd, check=True)
        return output_path


# -----------------------------------------------------------------------------
# App + Env
# -----------------------------------------------------------------------------
app = FastAPI(title="AI Video Factory")

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
def get_gspread_client() -> gspread.Client:
    cred_path = project_root / "app" / "google_credentials.json"
    if not cred_path.exists():
        raise RuntimeError(f"google_credentials.json not found at: {cred_path}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(str(cred_path), scopes=scopes)

    return gspread.authorize(creds)


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

    Returns [] ONLY when there is genuinely no matching history.
    Raises on any failure to reach or read the sheet, so the caller can
    tell "no history" apart from "history unavailable".
    """
    brand_lower = (brand_name or "").strip().lower()
    if not brand_lower:
        return []

    client = get_gspread_client()

    spreadsheet_name = os.getenv("SPREADSHEET_NAME", "concept_history")
    sheet_name = os.getenv("SHEET_NAME", "")  # optional

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


def append_concept_to_sheet(
    brand_name: str,
    concept: "ConceptResponse",
    runway: Optional[Dict[str, Any]],
    image_path_local: Optional[str] = None,
) -> bool:
    """
    Appends one concept row to the history sheet.
    Returns True on success, False on failure — the caller decides what to do.
    """
    

    spreadsheet_name = os.getenv("SPREADSHEET_NAME", "concept_history")
    sheet_name = os.getenv("SHEET_NAME", "")

    try:
        client = get_gspread_client()
        sh = client.open(spreadsheet_name)
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        runway_data = runway or {}
        row = [
            ts,
            brand_name,
            concept.concept_title,
            concept.hook_type,
            concept.format,
            concept.setting,
            concept.camera_style,
            concept.runway_prompt,
            concept.notes or "",
            image_path_local or "",
            runway_data.get("video_url", ""),
            runway_data.get("video_file_path", ""),
            runway_data.get("status", ""),
            runway_data.get("task_id", ""),
        ]

        ws.append_row(row, value_input_option="USER_ENTERED")
        return True

    except Exception as e:
        print("Google Sheets write failed:", repr(e))
        return False


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
    # False means the history sheet could not be read, so de-duplication
    # did NOT run — an empty avoid_list is not proof of no history.
    history_available: bool = True


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
    brand_name: str


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

    images = [f for f in (images or []) if isinstance(f, UploadFile) and f.filename]

    # An unreachable sheet must not silently look like "this brand has no history".
    try:
        rows = fetch_last_20_for_brand(brand_name)
        history_available = True
    except Exception as e:
        print("history unavailable:", repr(e))
        rows = []
        history_available = False

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
        history_available=history_available,
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

    # history_available is internal plumbing, not something the model should see.
    payload = req.model_dump(exclude={"history_available"})
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
    Also attaches brand_name + avoid_list to session metadata.
    """
    workflow_id = require_env("WORKFLOW_ID")
    client = get_openai_client()

    # Pull memory from Sheets
    try:
        avoid_list = fetch_last_20_for_brand(req.brand_name)
        history_available = True
    except Exception as e:
        print("history unavailable:", repr(e))
        avoid_list = []
        history_available = False

    try:
        session = client.beta.chatkit.sessions.create(
            user=req.user_id,
            workflow={"id": workflow_id},
            metadata={
                "brand_name": req.brand_name,
                "avoid_list": avoid_list,  # list[dict]
            },
        )
        return {
            "session_id": session.id,
            "client_secret": session.client_secret,
            "brand_name": req.brand_name,
            "avoid_count": len(avoid_list),
            "history_available": history_available,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Stage 5: Runway image-to-video (uses ephemeral upload)
# -----------------------------------------------------------------------------
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

    max_attempts = 3
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
                duration=req.duration_seconds,
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
                        "failure": "Max wait time exceeded",
                    }

                # await, not time.sleep: a blocking sleep here freezes the
                # entire event loop for every other request.
                await asyncio.sleep(POLL_INTERVAL_SEC)

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
                return {
                    "task_id": task_id,
                    "status": "SUCCEEDED",
                    "video_url": None,
                    "video_file_path": None,
                    "attempt": attempt,
                }

            video_url = output[0] if isinstance(output, list) else output

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            local_path = output_dir / f"video_{ts}.mp4"

            with requests.get(video_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)

            trim_seconds = float(os.getenv("TRIM_INTRO_SECONDS", "0.35"))
            local_path = trim_video_start(local_path, seconds=trim_seconds)

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
            await asyncio.sleep(5 * attempt)

    # If we got here, all attempts failed
    return {"status": "FAILED", "detail": last_error, "attempts": max_attempts}


# -----------------------------------------------------------------------------
# Full pipeline
# -----------------------------------------------------------------------------
@app.post("/pipeline/generate")
async def pipeline_generate(
    request: Request,
    brand_name: str = Form(...),
    brand_description: str = Form(...),
    product_type: str = Form(...),
    target_audience: str = Form(...),
    extra_comments: Optional[str] = Form(None),
    images: Optional[List[UploadFile]] = File(None),
):
    images = [f for f in (images or []) if isinstance(f, UploadFile) and f.filename]
    try:
        # 1) Ingest
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
            runway_payload = RunwayGenerateRequest(
                brand_name=ingest_result.brand_name,
                runway_prompt=concept_result.runway_prompt,
                image_path_local=ingest_result.saved_images_local[0],
                model="gen3a_turbo",
                ratio="1280:768",
                duration_seconds=5,
            )
            runway_result = await runway_generate(runway_payload)

        # 4) Append to Google Sheets (best-effort, but reported)
        history_written = append_concept_to_sheet(
            brand_name=ingest_result.brand_name,
            concept=concept_result,
            runway=runway_result,
            image_path_local=ingest_result.saved_images_local[0] if ingest_result.saved_images_local else None,
        )

        return {
            "ingest": ingest_result.model_dump(),
            "concept": concept_result.model_dump(),
            "runway": runway_result,
            # If either of these is False, de-duplication is compromised for
            # the next run — an unrecorded concept can be regenerated.
            "history_available": ingest_result.history_available,
            "history_written": history_written,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"pipeline_generate failed: {e}")