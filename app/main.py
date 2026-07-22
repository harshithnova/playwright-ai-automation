from fastapi import Query
from fastapi import (
    FastAPI,
    Request,
    UploadFile,
    File,
    Header,
    HTTPException,
    Depends,
    Form,
)
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from agents.pipeline import run_agent_pipeline
from agents.doc_sanitiser import sanitise_document
import asyncio
from db.database import (
    init_db,
    get_runs,
    get_run
)


import os

from app.llm_generator import generate_test_cases
from app.yaml_validator import validate_yaml
from app.yaml_to_playwright import convert_yaml_to_playwright

import secrets

_configured_key = os.getenv("APP_API_KEY", "")

# Fail-closed: if no key is configured, generate one and log it.
# The developer must use this key in X-API-Key header.
# Set ALLOW_UNAUTHENTICATED=true in .env to disable auth in dev.
if not _configured_key and os.getenv("ALLOW_UNAUTHENTICATED", "").lower() != "true":
    _configured_key = secrets.token_urlsafe(32)
    print(
        f"\n{'='*60}\n"
        f"[AUTH] No APP_API_KEY set — generated a temporary key:\n"
        f"[AUTH] APP_API_KEY={_configured_key}\n"
        f"[AUTH] Add this to your .env or set ALLOW_UNAUTHENTICATED=true\n"
        f"{'='*60}\n"
    )

APP_API_KEY = _configured_key


def verify_api_key(x_api_key: str = Header(default="")):
    """
    Require X-API-Key header on protected endpoints.
    Fails closed: if APP_API_KEY is set (or auto-generated),
    the key MUST match. Set ALLOW_UNAUTHENTICATED=true to disable.
    """
    if os.getenv("ALLOW_UNAUTHENTICATED", "").lower() == "true":
        return  # explicit opt-out for local dev
    if not APP_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Application API key is not configured."
        )
    if x_api_key != APP_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing X-API-Key header."
        )
    

app = FastAPI(title="Test Case Generator")


@app.on_event("startup")
async def startup():
    init_db()
class PipelineRequest(BaseModel):
    design_doc: str
    target_url: str | None = None

# -------------------------------------
# Create folders if missing
# -------------------------------------

os.makedirs("uploads", exist_ok=True)
os.makedirs("output", exist_ok=True)

# -------------------------------------
# Templates
# -------------------------------------

templates = Jinja2Templates(
    directory="app/templates"
)

# -------------------------------------
# Static Files
# -------------------------------------

app.mount(
    "/static",
    StaticFiles(directory="app/static"),
    name="static"
)


# -------------------------------------
# Home Page
# -------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request
        }
    )


# -------------------------------------
# Upload Route
# -------------------------------------
@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context={
            "request": request
        }
    )
@app.post("/upload", response_class=HTMLResponse)
async def upload_document(
    request: Request,
    target_url: str = Form(...),
    file: UploadFile = File(...),
):

    try:

        # ----------------------------
        # Validate File
        # ----------------------------

        if not file.filename:

            return templates.TemplateResponse(
                request=request,
                name="results.html",
                context={
                    "request": request,
                    "error": "No file selected."
                }
            )

        allowed_extensions = [
            ".txt",
            ".md",
            ".pdf"
        ]

        extension = os.path.splitext(
            file.filename
        )[1].lower()

        if extension not in allowed_extensions:

            return templates.TemplateResponse(
                request=request,
                name="results.html",
                context={
                    "request": request,
                    "error": f"Unsupported file type: {extension}"
                }
            )

        # ----------------------------
        # Read & Validate Uploaded File
        # ----------------------------

        content = await file.read()

        # Validate file size (max 2 MB)
        MAX_BYTES = 2 * 1024 * 1024

        if len(content) > MAX_BYTES:
            return templates.TemplateResponse(
                request=request,
                name="results.html",
                context={
                    "request": request,
                    "error": "File too large (max 2 MB)."
                }
            )

        # Validate UTF-8 for text files
        if extension in (".txt", ".md"):
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                return templates.TemplateResponse(
                    request=request,
                    name="results.html",
                    context={
                        "request": request,
                        "error": "File is not valid UTF-8 text."
                    }
                )

        # Safe filename
        from pathlib import Path

        safe_name = Path(file.filename).name
        file_path = os.path.join("uploads", safe_name)

        # Save uploaded file
        with open(file_path, "wb") as f:
            f.write(content)

        print("File saved:", file_path)

        # ----------------------------
        # Read Document Content
        # ----------------------------

        document_text = ""

        if extension == ".pdf":

            from pypdf import PdfReader

            def _extract_pdf(path: str) -> str:
                with open(path, "rb") as pdf_file:
                    reader = PdfReader(pdf_file)
                    return "".join(
                        page.extract_text() or ""
                        for page in reader.pages
                    )

            # Run PDF parsing in a thread — avoids blocking the event loop
            document_text = await asyncio.to_thread(_extract_pdf, file_path)

        else:

            document_text = content.decode(
                "utf-8",
                errors="ignore"
            )

        document_text, warnings = sanitise_document(document_text)
        
        if not document_text.strip():
            raise ValueError("Uploaded document contains no readable text.")

        if warnings:
            print(f"[Security] Document sanitised. Warnings: {warnings}")

        print("\n========== DOCUMENT ==========")
        print(document_text[:500])
        print("==============================\n")




        # ----------------------------
        # Authoritative path: LangGraph Agent Pipeline
        # ----------------------------
        agent_result = await asyncio.to_thread(
            run_agent_pipeline,
            design_doc=document_text,
            target_url=target_url,
        )

        # BUG FIX: `yaml_output` was referenced here before it was ever
        # assigned (NameError), and the code below it unconditionally
        # re-ran generate_test_cases() regardless of what the agent
        # pipeline produced -- so the "authoritative" LangGraph path
        # was silently discarded every single time. Pull the YAML the
        # agent pipeline actually produced (if any), and only fall
        # back to the legacy single-shot generator when it's missing.
        # NOTE: agents/pipeline.py's run_agent_pipeline() returns the
        # YAML under the key "generated_yaml", not "yaml_output".
        yaml_output = (
            agent_result.get("generated_yaml", "") if isinstance(agent_result, dict) else ""
        )

        if not yaml_output.strip():
            print("[Upload] Agent pipeline returned no YAML — falling back to llm_generator.")
            yaml_output = await asyncio.to_thread(
                generate_test_cases, document_text
            )

        print("\n========== YAML OUTPUT ==========")
        print(yaml_output)
        print("=================================\n")

        # ----------------------------
        # Validate YAML
        # ----------------------------

        validation = validate_yaml(
            yaml_output
        )

        print("Validation Result:")
        print(validation)

        # ----------------------------
        # Convert to Playwright
        # ----------------------------

        playwright_output = convert_yaml_to_playwright(
            yaml_output
        )

        print("Playwright Conversion Complete")

        # ----------------------------
        # Render Results Page
        # ----------------------------

        return templates.TemplateResponse(
            request=request,
            name="results.html",
            context={
                "request": request,
                "filename": file.filename,
                "preview": document_text[:1500],

                # Existing outputs
                "yaml_output": yaml_output,
                "validation": validation,
                "playwright_output": playwright_output.get(
                    "code",
                    "No Playwright code generated"
                ),
                "test_cases": playwright_output.get(
                    "test_cases",
                    []
                ),

                # LangGraph outputs
                "agent_task_plan": agent_result.get("task_plan", []),
                "agent_code": agent_result.get("generated_code", ""),
                "agent_review": agent_result.get("review_notes", ""),
                "agent_edge_cases": agent_result.get("edge_cases", []),
                "agent_architecture": agent_result.get("architecture_notes", "")
            }
        )

    except Exception as e:

        print("\n========== ERROR ==========")
        print(str(e))
        print("===========================\n")

        return templates.TemplateResponse(
            request=request,
            name="results.html",
            context={
                "request": request,
                "error": str(e)
            }
        )
# -------------------------------------
# LangGraph Agent Pipeline API
# -------------------------------------

@app.post("/agents/run")
async def run_agents(req: PipelineRequest, _: None = Depends(verify_api_key)):
    """
    Runs the complete LangGraph Agent Pipeline.
    """

    try:
        print(f"\n[Upload] Target URL: {req.target_url}\n")
        result = await asyncio.to_thread(
            run_agent_pipeline,
            design_doc=req.design_doc,
            target_url=req.target_url
            )

        return {
            "status": "success",
            "task_plan": result.get("task_plan", []),
            "architecture_notes": result.get("architecture_notes", ""),
            "generated_code": result.get("generated_code", ""),
            "review_notes": result.get("review_notes", ""),
            "edge_cases": result.get("edge_cases", []),
            "selectors": result.get("selectors", [])
        }

    except Exception as e:

        print("\n========== AGENT PIPELINE ERROR ==========")
        print(str(e))
        print("==========================================\n")

        return {
            "status": "error",
            "message": str(e)
        }
        
# --------------------------------------------------------
# Run History APIs
# --------------------------------------------------------

@app.get("/runs")
async def list_runs(
    limit: int = Query(
        default=20,
        ge=1,
        le=100
    ),
    _: None = Depends(verify_api_key),
):
    """
    Return the latest pipeline runs.
    """

    try:
        return {
            "runs": get_runs(limit=limit)
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.get("/runs/{run_id}")
async def get_run_detail(
    run_id: str,
    _: None = Depends(verify_api_key),
):
    """
    Return one pipeline execution.
    """

    try:
        run = get_run(run_id)

        if not run:
            raise HTTPException(
                status_code=404,
                detail="Run not found"
            )

        return run

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )