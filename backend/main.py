"""FastAPI app: serves the UI and the /api/search + /api/download endpoints."""
from __future__ import annotations

import base64
import csv
import io
import os
import secrets
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

import pipeline  # noqa: E402
from agent import DEFAULT_MODEL, run_agent  # noqa: E402  (after load_dotenv)
from hubspot import HubSpot  # noqa: E402

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Single-user HTTP Basic auth. Credentials come from env; defaults match the one
# provisioned user. Override AUTH_USERNAME / AUTH_PASSWORD in production.
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "navaneetha.krishnan@lyzr.ai")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "navaneetha.krishnan@lyzr.ai")
# Paths reachable without auth (so deploy platforms can health-check).
_PUBLIC_PATHS = {"/api/health"}

app = FastAPI(title="HubSpot Lead Agent")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Gate every route behind one set of Basic-auth credentials."""
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    ok = False
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            ok = secrets.compare_digest(user, AUTH_USERNAME) and secrets.compare_digest(
                pw, AUTH_PASSWORD
            )
        except Exception:
            ok = False
    if not ok:
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="HubSpot Lead Agent"'},
        )
    return await call_next(request)

# In-memory CSV store (fine for a local test tool). id -> csv text.
_CSV_STORE: dict[str, str] = {}

# Columns we surface first when present; everything else follows alphabetically.
_PREFERRED_ORDER = [
    "firstname", "lastname", "email", "jobtitle", "company", "country",
    "lifecyclestage", "hs_lead_status", "phone", "owner_name", "hubspot_owner_id",
    "hs_contact_id", "hs_deal_id", "deal_name", "deal_stage", "deal_owner",
    "deal_amount", "deal_closedate", "deal_state", "loss_class",
    "deal_closelost_reason", "notes_last_contacted", "createdate", "hubspot_link",
]


class SearchRequest(BaseModel):
    prompt: str
    model: Optional[str] = None


def _order_columns(rows: list[dict]) -> list[str]:
    keys: set[str] = set()
    for r in rows:
        keys.update(r.keys())
    ordered = [c for c in _PREFERRED_ORDER if c in keys]
    ordered += sorted(k for k in keys if k not in ordered)
    return ordered


def _to_csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r.get(c, "") for c in columns})
    return buf.getvalue()


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "model": DEFAULT_MODEL,
        "hubspot_token_set": bool(os.environ.get("HUBSPOT_TOKEN")),
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }


@app.post("/api/search")
def search(req: SearchRequest) -> dict:
    if not req.prompt.strip():
        raise HTTPException(400, "Prompt is required.")
    if not os.environ.get("HUBSPOT_TOKEN") or not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "Set HUBSPOT_TOKEN and ANTHROPIC_API_KEY in your .env file.")

    try:
        result = run_agent(req.prompt, model=req.model or DEFAULT_MODEL)
    except Exception as e:  # surface a clean message to the UI
        raise HTTPException(500, f"Agent error: {e}")

    last = result["last_search"]
    if not last or not last["records"]:
        return {
            "summary": result["summary"],
            "query": last["query"] if last else None,
            "total": last["total"] if last else 0,
            "count": 0,
            "columns": [],
            "rows": [],
            "csv_id": None,
        }

    hs: HubSpot = result["hubspot"]
    object_type = last["object_type"]
    include_deals = bool(last.get("include_associated_deals"))
    deal_filter = last.get("deal_filter") or {}

    # Deterministic post-processing over the FULL set (not the model's sample):
    rows = pipeline.build_rows(last["records"], hs, object_type, include_deals)
    rows, junk = pipeline.drop_junk_leads(rows)        # standing rule: never show junk
    rows = pipeline.dedupe(rows)                        # unique contact x deal (F5)
    rows, off_target = pipeline.apply_deal_filter(rows, deal_filter)  # deal state/loss class
    rows, no_email = pipeline.split_missing_email(rows)  # honest blanks (F4)
    stats = pipeline.breakdown(rows)                    # reconcilable counts (E2)

    # CSV carries qualifying rows, then no-email and off-target rows clearly separated.
    csv_rows = rows + no_email + off_target
    columns = _order_columns(csv_rows)
    csv_text = _to_csv(csv_rows, columns)
    csv_id = uuid.uuid4().hex
    _CSV_STORE[csv_id] = csv_text

    return {
        "summary": result["summary"],
        "query": last["query"],
        "total": last["total"],
        "count": len(rows),
        "columns": columns,
        "rows": rows[:200],  # preview cap; full set is in the CSV
        "deals_joined": include_deals,
        "deal_filter": deal_filter or None,
        "off_target_count": len(off_target),
        "off_target_rows": off_target[:200],
        "no_email_count": len(no_email),
        "no_email_rows": no_email[:200],
        "junk_dropped_count": len(junk),
        "breakdown": stats,
        "csv_id": csv_id,
    }


@app.get("/api/download/{csv_id}")
def download(csv_id: str) -> Response:
    csv_text = _CSV_STORE.get(csv_id)
    if csv_text is None:
        raise HTTPException(404, "CSV not found or expired.")
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


# ----- serve the frontend ------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
