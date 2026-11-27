"""Seal web app. A file stays blocked until its agreement is signed."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel, Field

from fastapi.concurrency import run_in_threadpool

from app import db
from app.ai import ai_enabled, model_name, read_agreement
from app.emailer import email_enabled, send_email
from app.pdfutil import (
    already_certified,
    append_certificate,
    build_sample_pdf,
    clamp_box,
    detect_fields,
    page_count,
    png_has_ink,
    render_page_png,
    sha256_bytes,
    stamp_fields,
)

log = logging.getLogger("seal")

ROOT = Path(__file__).resolve().parent.parent
ID_RE = re.compile(r"^[a-f0-9]{32}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KINDS = {"signature", "initials", "name", "date"}
MAX_PDF = 15 * 1024 * 1024

STATUS_LABELS = {
    "draft": "Preparing",
    "awaiting": "Out for signature",
    "partial": "Partly signed",
    "complete": "Ready to start",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    for name in ("originals", "working", "pages"):
        (db.data_dir() / name).mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Seal", lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
templates.env.filters["pretty"] = db.pretty_time
templates.env.filters["tojson"] = lambda value: Markup(json.dumps(value).replace("<", "\\u003c"))
templates.env.globals["asset_version"] = str(
    max(int(path.stat().st_mtime) for path in (ROOT / "app" / "static").glob("*.*"))
)
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")


def new_id() -> str:
    return uuid.uuid4().hex


def new_token() -> str:
    return secrets.token_urlsafe(24)


def clean_text(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def check_email(value: str) -> str:
    email = clean_text(value, 200).lower()
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a real-looking email address. Use a test address, not a real person's.")
    return email


def require_id(value: str) -> str:
    if not ID_RE.match(value or ""):
        raise HTTPException(status_code=404)
    return value


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:80]
    if request.client:
        return request.client.host
    return ""


def base_url(request: Request) -> str:
    configured = os.environ.get("SEAL_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def access_key() -> str:
    return os.environ.get("SEAL_ACCESS_KEY", "").strip()


def access_token(key: str) -> str:
    return hmac.new(key.encode(), b"seal-access-v1", hashlib.sha256).hexdigest()


def keys_match(given: str, expected: str) -> bool:
    if len(given) != len(expected) or not expected:
        return False
    return hmac.compare_digest(given, expected)


def redirect(url: str, message: str | None = None) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if message:
        response.set_cookie("seal_flash", quote(message), max_age=30, httponly=True, samesite="lax")
    return response


def render(request: Request, name: str, status_code: int = 200, **context):
    flashed = unquote(request.cookies.get("seal_flash", ""))
    context["request"] = request
    context.setdefault("company_name", db.setting("company_name", ""))
    context.setdefault("email_enabled", email_enabled())
    context.setdefault("ai_on", ai_enabled())
    context.setdefault("ai_model", model_name())
    context.setdefault("public_page", False)
    context["flash"] = flashed
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    if flashed:
        response.delete_cookie("seal_flash")
    return response


@app.middleware("http")
async def access_gate(request: Request, call_next):
    key = access_key()
    path = request.url.path
    open_path = (
        not key
        or path.startswith("/static")
        or path.startswith("/sign")
        or path in {"/health", "/unlock"}
    )
    if open_path:
        return await call_next(request)
    expected = access_token(key)
    cookie = request.cookies.get("seal_access", "")
    if len(cookie) == len(expected) and hmac.compare_digest(cookie, expected):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse({"detail": "Unlock the workspace first."}, status_code=401)
    return RedirectResponse("/unlock", status_code=303)


def original_path(file_id: str) -> Path:
    return db.data_dir() / "originals" / f"{file_id}.pdf"


def working_path(file_id: str) -> Path:
    return db.data_dir() / "working" / f"{file_id}.pdf"


def cached_page(file_id: str, page_index: int) -> bytes:
    pdf = working_path(file_id)
    if not pdf.exists():
        raise HTTPException(status_code=404)
    cache = db.data_dir() / "pages" / file_id
    cache.mkdir(parents=True, exist_ok=True)
    marker = cache / "mtime"
    mtime = str(pdf.stat().st_mtime_ns)
    if not marker.exists() or marker.read_text(encoding="utf-8") != mtime:
        for child in cache.glob("*.png"):
            child.unlink()
        marker.write_text(mtime, encoding="utf-8")
    target = cache / f"{page_index}.png"
    if not target.exists():
        try:
            target.write_bytes(render_page_png(pdf, page_index))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return target.read_bytes()


def load_file(file_id: str) -> dict:
    record = db.get_file(require_id(file_id))
    if not record:
        raise HTTPException(status_code=404)
    return record


def any_signed(file_id: str) -> bool:
    return any(signer["status"] == "signed" for signer in db.list_signers(file_id))


def editable(file_id: str) -> bool:
    return not any_signed(file_id)


def agreement_date(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    return moment.strftime("%d %b %Y")


def initials_for(name: str) -> str:
    parts = [part for part in name.split() if part]
    return "".join(part[0] for part in parts).upper()[:4]


def safe_filename(title: str, suffix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", title).strip() or "agreement"
    return f"{cleaned[:80]} {suffix}.pdf"


def signer_tag(field: dict, signers: list[dict]) -> str:
    signer = next((item for item in signers if item["id"] == field["signer_id"]), None)
    who = signer["name"].split()[0] if signer else "Signer"
    label = field["label"] or field["kind"]
    if field["party"] == "other" and signer and signer["role"] == "client":
        return f"{label} · {who} · check who"
    return f"{label} · {who}"


def field_class(field: dict, signers: list[dict]) -> str:
    signer = next((item for item in signers if item["id"] == field["signer_id"]), None)
    if signer and signer["role"] != "client":
        return "firm"
    if field["party"] == "other":
        return "firm"
    return "client"


def ingest(data: bytes, filename: str, title: str, client_name: str, client_email: str, note: str) -> str:
    if len(data) > MAX_PDF:
        raise ValueError("That PDF is over 15 MB.")
    if not data.startswith(b"%PDF"):
        raise ValueError("Upload the agreement as a PDF.")

    file_id = new_id()
    original = original_path(file_id)
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(data)
    try:
        detected, rotated, report = detect_fields(original, read_agreement if ai_enabled() else None)
        count = page_count(original)
    except Exception:
        original.unlink(missing_ok=True)
        raise

    shutil.copy(original, working_path(file_id))
    now = db.utcnow()
    db.create_file(
        {
            "id": file_id,
            "title": title,
            "client_name": client_name,
            "client_email": client_email,
            "status": "draft",
            "note": note,
            "original_name": clean_text(filename, 180),
            "doc_sha256": sha256_bytes(data),
            "rotated": 1 if rotated else 0,
            "accepting": 1,
            "created_at": now,
        }
    )
    signer_id = new_id()
    db.create_signer(
        {
            "id": signer_id,
            "file_id": file_id,
            "name": client_name,
            "email": client_email,
            "role": "client",
            "token": new_token(),
            "status": "pending",
            "created_at": now,
        }
    )
    for field in detected:
        db.create_field(
            {
                "id": new_id(),
                "file_id": file_id,
                "signer_id": signer_id,
                "kind": field["kind"],
                "page": field["page"],
                "x": field["x"],
                "y": field["y"],
                "w": field["w"],
                "h": field["h"],
                "label": field["label"],
                "party": field["party"],
                "source": field["source"],
            }
        )
    db.add_event(file_id, "created", f"{count} pages")
    if report["used"]:
        parties = ", ".join(f"{party['name']} ({party['role']})" for party in report["parties"]) or "none named"
        from_ai = sum(1 for field in detected if field["source"] == "ai")
        db.add_event(file_id, "ai_read", f"{model_name()} · parties: {parties} · {from_ai} fields")
    elif report["error"]:
        db.add_event(file_id, "ai_failed", f"{report['error']} · used the built-in rules instead")
    if detected:
        db.add_event(file_id, "fields_found", f"{len(detected)} placed from the document")
    else:
        db.add_event(file_id, "fields_found", "none found; place them by hand")
    return file_id


def certificate_lines(record: dict, signers: list[dict], events: list[dict]) -> list[str]:
    company = db.setting("company_name", "") or "This workspace"
    lines = [
        company,
        f"File: {record['title']}",
        f"Client: {record['client_name']} <{record['client_email']}>",
        f"Original document SHA-256: {record['doc_sha256']}",
        "The agreement text was not edited. Values were written only into the signature fields, and this page was added at the end.",
        "",
        "Signers",
    ]
    for signer in signers:
        lines.append(
            f"{signer['name']} <{signer['email']}> signed {db.pretty_time(signer['signed_at'])} "
            f"from {signer['ip'] or 'an unknown address'} ({signer['signature_mode'] or 'signed'})."
        )
    lines.append("")
    lines.append("Activity")
    for event in events:
        when = db.pretty_time(event["created_at"])
        detail = f" — {event['detail']}" if event["detail"] else ""
        lines.append(f"{when}  {event['kind'].replace('_', ' ')}{detail}")
    lines.append("")
    lines.append(
        "This page is a business record of the signing session. It is not a government-issued or qualified electronic certificate."
    )
    return lines


def finish_if_ready(file_id: str) -> None:
    record = db.get_file(file_id)
    signers = db.list_signers(file_id)
    if not record or not signers or any(signer["status"] != "signed" for signer in signers):
        return
    path = working_path(file_id)
    if not already_certified(path):
        append_certificate(path, certificate_lines(record, signers, db.list_events(file_id)))
    if record["status"] != "complete":
        db.update_file(file_id, status="complete", completed_at=db.utcnow())
        db.add_event(file_id, "ready", "Every signer has signed. Work can start.")


def refresh_status(file_id: str) -> None:
    record = db.get_file(file_id)
    if not record or record["status"] == "complete":
        return
    signers = db.list_signers(file_id)
    signed = [signer for signer in signers if signer["status"] == "signed"]
    if signers and len(signed) == len(signers):
        finish_if_ready(file_id)
    elif signed and record["status"] != "partial":
        db.update_file(file_id, status="partial")


class FieldIn(BaseModel):
    signer_id: str
    kind: str
    page: int = Field(ge=0)
    x: float
    y: float
    w: float
    h: float


class FieldPatch(BaseModel):
    signer_id: str | None = None
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None


@app.get("/health")
def health():
    return {"ok": True, "product": "seal"}


@app.get("/")
def dashboard(request: Request):
    files = []
    for record in db.list_files():
        signers = db.list_signers(record["id"])
        signed = sum(1 for signer in signers if signer["status"] == "signed")
        files.append({**record, "signed": signed, "signer_count": len(signers), "label": STATUS_LABELS.get(record["status"], record["status"])})
    return render(request, "dashboard.html", files=files)


@app.get("/settings")
def settings_page(request: Request):
    return render(
        request,
        "settings.html",
        company_name=db.setting("company_name", ""),
        company_email=db.setting("company_email", ""),
        locked=bool(access_key()),
    )


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    db.set_setting("company_name", clean_text(str(form.get("company_name") or ""), 80))
    email_raw = clean_text(str(form.get("company_email") or ""), 200).lower()
    if email_raw and not EMAIL_RE.match(email_raw):
        return render(
            request,
            "settings.html",
            status_code=400,
            company_name=db.setting("company_name", ""),
            company_email=email_raw,
            locked=bool(access_key()),
            error="That company email doesn't look right.",
        )
    db.set_setting("company_email", email_raw)
    return redirect("/settings", "Settings saved.")


@app.get("/unlock")
def unlock_page(request: Request):
    if not access_key():
        return redirect("/")
    return render(request, "unlock.html", public_page=True)


@app.post("/unlock")
async def unlock(request: Request):
    key = access_key()
    form = await request.form()
    given = str(form.get("key") or "").strip()
    if not key or not keys_match(given, key):
        return render(request, "unlock.html", public_page=True, status_code=401, error="That key doesn't match.")
    response = redirect("/")
    response.set_cookie(
        "seal_access",
        access_token(key),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.get("/files/new")
def new_file_page(request: Request):
    return render(request, "new.html", form={})


@app.post("/files")
async def create_file(request: Request):
    form = await request.form()
    upload = form.get("agreement")
    payload = {
        "title": clean_text(str(form.get("title") or ""), 160),
        "client_name": clean_text(str(form.get("client_name") or ""), 120),
        "client_email": clean_text(str(form.get("client_email") or ""), 200),
        "note": clean_text(str(form.get("note") or ""), 2000),
    }
    try:
        payload["client_email"] = check_email(payload["client_email"])
        if len(payload["client_name"]) < 2:
            raise ValueError("Enter the client name.")
        if not payload["title"]:
            payload["title"] = f"{payload['client_name']} agreement"
        if upload is None or not getattr(upload, "filename", ""):
            raise ValueError("Choose the agreement PDF.")
        data = await upload.read()
        file_id = await run_in_threadpool(
            ingest,
            data,
            upload.filename,
            payload["title"],
            payload["client_name"],
            payload["client_email"],
            payload["note"],
        )
    except ValueError as exc:
        return render(request, "new.html", status_code=400, error=str(exc), form=payload)
    return redirect(f"/files/{file_id}", "Agreement uploaded. Check the fields, then use the signing link.")


@app.post("/sample")
def create_sample():
    file_id = ingest(
        build_sample_pdf(),
        "sample-agreement.pdf",
        "Sample engagement",
        "Jordan Hale",
        "jordan.hale@example.com",
        "Fictional sample. Safe to delete.",
    )
    return redirect(f"/files/{file_id}", "Sample file loaded. Jordan Hale is not a real person.")


@app.get("/files/{file_id}")
def file_page(request: Request, file_id: str):
    record = load_file(file_id)
    try:
        refresh_status(file_id)
    except Exception:
        log.exception("status refresh failed for %s", file_id)
    record = load_file(file_id)
    signers = db.list_signers(file_id)
    fields = db.list_fields(file_id)
    try:
        pages = page_count(working_path(file_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    can_edit = editable(file_id)
    prepared = []
    for field in fields:
        prepared.append({**field, "tag": signer_tag(field, signers), "css": field_class(field, signers)})
    other_warning = False
    for field in fields:
        signer = next((item for item in signers if item["id"] == field["signer_id"]), None)
        if field["party"] == "other" and signer and signer["role"] == "client":
            other_warning = True
            break
    events = db.list_events(file_id)
    ai_event = next((event for event in events if event["kind"] in {"ai_read", "ai_failed"}), None)
    return render(
        request,
        "file.html",
        file=record,
        ai_event=ai_event,
        signers=signers,
        fields=prepared,
        pages=pages,
        editable=can_edit,
        other_warning=other_warning,
        status_label=STATUS_LABELS.get(record["status"], record["status"]),
        base=base_url(request),
        events=events,
        has_signature=any(field["kind"] == "signature" for field in fields),
        state={
            "fileId": record["id"],
            "editable": can_edit,
            "status": record["status"],
            "signers": [
                {"id": signer["id"], "name": signer["name"], "role": signer["role"], "status": signer["status"]}
                for signer in signers
            ],
        },
    )


@app.post("/files/{file_id}/details")
async def update_details(request: Request, file_id: str):
    record = load_file(file_id)
    if not editable(file_id):
        return redirect(f"/files/{file_id}", "Details stay put once someone has signed.")
    form = await request.form()
    try:
        title = clean_text(str(form.get("title") or ""), 160) or record["title"]
        client_name = clean_text(str(form.get("client_name") or ""), 120)
        client_email = check_email(str(form.get("client_email") or ""))
        note = clean_text(str(form.get("note") or ""), 2000)
        if len(client_name) < 2:
            raise ValueError("Enter the client name.")
    except ValueError as exc:
        return redirect(f"/files/{file_id}", str(exc))
    db.update_file(file_id, title=title, client_name=client_name, client_email=client_email, note=note)
    signers = db.list_signers(file_id)
    client = next((signer for signer in signers if signer["role"] == "client"), None)
    if client and client["status"] != "signed":
        db.update_signer(client["id"], name=client_name, email=client_email)
    db.add_event(file_id, "details_updated")
    return redirect(f"/files/{file_id}", "File details saved.")


@app.post("/files/{file_id}/signers")
async def add_signer(request: Request, file_id: str):
    load_file(file_id)
    if not editable(file_id):
        return redirect(f"/files/{file_id}", "Signers are locked once someone has signed.")
    form = await request.form()
    try:
        name = clean_text(str(form.get("name") or ""), 120)
        email = check_email(str(form.get("email") or ""))
        if len(name) < 2:
            raise ValueError("Enter the signer's name.")
    except ValueError as exc:
        return redirect(f"/files/{file_id}", str(exc))
    role = "firm" if str(form.get("role") or "") != "client" else "client"
    db.create_signer(
        {
            "id": new_id(),
            "file_id": file_id,
            "name": name,
            "email": email,
            "role": role,
            "token": new_token(),
            "status": "pending",
            "created_at": db.utcnow(),
        }
    )
    db.add_event(file_id, "signer_added", name)
    return redirect(f"/files/{file_id}", f"{name} added. Assign the fields they should sign.")


@app.post("/files/{file_id}/signers/{signer_id}/delete")
def remove_signer(file_id: str, signer_id: str):
    load_file(file_id)
    if not editable(file_id):
        return redirect(f"/files/{file_id}", "Signers are locked once someone has signed.")
    signer = db.get_signer(require_id(signer_id))
    if not signer or signer["file_id"] != file_id:
        raise HTTPException(status_code=404)
    db.delete_signer(signer_id)
    db.add_event(file_id, "signer_removed", signer["name"])
    return redirect(f"/files/{file_id}", f"Removed {signer['name']} and their fields.")


@app.post("/files/{file_id}/send")
async def send_for_signature(request: Request, file_id: str):
    record = load_file(file_id)
    form = await request.form()
    if form.get("confirm") != "on":
        return redirect(f"/files/{file_id}", "Check the fields on the agreement before sending it.")
    signers = db.list_signers(file_id)
    fields = db.list_fields(file_id)
    if not signers:
        return redirect(f"/files/{file_id}", "Add the person who needs to sign.")
    missing = []
    for signer in signers:
        if not any(field["signer_id"] == signer["id"] and field["kind"] == "signature" for field in fields):
            missing.append(signer["name"])
    if missing:
        return redirect(f"/files/{file_id}", "Every signer needs a signature field: " + ", ".join(missing) + ".")
    db.update_file(file_id, accepting=1, status="awaiting" if record["status"] == "draft" else record["status"], sent_at=record["sent_at"] or db.utcnow())
    company = db.setting("company_name", "") or "We"
    mailed = []
    failed = []
    for signer in signers:
        if signer["status"] == "signed":
            continue
        link = f"{base_url(request)}/sign/{signer['token']}"
        ok, message = send_email(
            signer["email"],
            f"Signature needed: {record['title']}",
            (
                f"{company} needs your signature before work can start.\n\n"
                f"File: {record['title']}\n"
                f"Sign here: {link}\n\n"
                "This link is yours. Don't forward it.\n"
            ),
        )
        if ok:
            mailed.append(signer["name"])
        else:
            failed.append(message)
    detail = "emailed " + ", ".join(mailed) if mailed else "link only; email was not sent"
    db.add_event(file_id, "sent", detail)
    if mailed and not failed:
        return redirect(f"/files/{file_id}", "Signing email sent.")
    if not email_enabled():
        return redirect(f"/files/{file_id}", "Marked out for signature. Email isn't set up, so send the link yourself.")
    return redirect(f"/files/{file_id}", failed[0] if failed else "Marked out for signature.")


@app.post("/files/{file_id}/remind/{signer_id}")
def remind(request: Request, file_id: str, signer_id: str):
    record = load_file(file_id)
    signer = db.get_signer(require_id(signer_id))
    if not signer or signer["file_id"] != file_id:
        raise HTTPException(status_code=404)
    if signer["status"] == "signed":
        return redirect(f"/files/{file_id}", f"{signer['name']} has already signed.")
    link = f"{base_url(request)}/sign/{signer['token']}"
    company = db.setting("company_name", "") or "We"
    ok, message = send_email(
        signer["email"],
        f"Reminder: {record['title']} still needs your signature",
        f"{company} is still waiting on your signature before work can start.\n\nSign here: {link}\n",
    )
    db.add_event(file_id, "reminded", signer["name"] + (" · emailed" if ok else " · link only"), signer_id)
    if ok:
        return redirect(f"/files/{file_id}", f"Reminder emailed to {signer['name']}.")
    return redirect(f"/files/{file_id}", f"Reminder recorded for {signer['name']}. {message}")


@app.post("/files/{file_id}/recall")
def recall(file_id: str):
    record = load_file(file_id)
    if any_signed(file_id):
        return redirect(f"/files/{file_id}", "Someone already signed, so this link stays as it is.")
    db.update_file(file_id, accepting=0, status="draft", sent_at=None)
    db.add_event(file_id, "recalled", "Signing links paused")
    return redirect(f"/files/{record['id']}", "Links are paused. Fix the fields, then mark it out for signature again.")


@app.post("/files/{file_id}/finish")
def finish(file_id: str):
    load_file(file_id)
    try:
        finish_if_ready(file_id)
    except Exception as exc:
        return redirect(f"/files/{file_id}", f"Couldn't finish the file: {exc}")
    record = load_file(file_id)
    if record["status"] != "complete":
        return redirect(f"/files/{file_id}", "It's not ready yet. Someone still has to sign.")
    return redirect(f"/files/{file_id}", "The file is signed and ready to start.")


@app.post("/files/{file_id}/delete")
def remove_file(file_id: str):
    load_file(file_id)
    db.delete_file(file_id)
    for path in (original_path(file_id), working_path(file_id)):
        path.unlink(missing_ok=True)
    cache = db.data_dir() / "pages" / file_id
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
    return redirect("/", "File deleted.")


@app.get("/files/{file_id}/pages/{page_index}.png")
def file_page_image(file_id: str, page_index: int):
    load_file(file_id)
    return Response(
        content=cached_page(file_id, page_index),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/files/{file_id}/download")
def download_current(file_id: str):
    record = load_file(file_id)
    path = working_path(file_id)
    suffix = "signed" if record["status"] == "complete" else "current"
    return FileResponse(path, media_type="application/pdf", filename=safe_filename(record["title"], suffix))


@app.get("/files/{file_id}/original")
def download_original(file_id: str):
    record = load_file(file_id)
    path = original_path(file_id)
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=safe_filename(record["title"], "original"))


def _field_payload(field: dict, signers: list[dict]) -> dict:
    return {
        "id": field["id"],
        "signer_id": field["signer_id"],
        "kind": field["kind"],
        "page": field["page"],
        "x": field["x"],
        "y": field["y"],
        "w": field["w"],
        "h": field["h"],
        "party": field["party"],
        "tag": signer_tag(field, signers),
        "css": field_class(field, signers),
    }


@app.get("/api/files/{file_id}")
def api_file(file_id: str):
    record = load_file(file_id)
    signers = db.list_signers(file_id)
    fields = [_field_payload(field, signers) for field in db.list_fields(file_id)]
    return {
        "file": {"id": record["id"], "status": record["status"], "title": record["title"]},
        "signers": [
            {"id": signer["id"], "name": signer["name"], "email": signer["email"], "role": signer["role"], "status": signer["status"]}
            for signer in signers
        ],
        "fields": fields,
    }


@app.get("/api/files/{file_id}/status")
def api_status(file_id: str):
    record = load_file(file_id)
    signers = db.list_signers(file_id)
    return {
        "status": record["status"],
        "label": STATUS_LABELS.get(record["status"], record["status"]),
        "signers": [
            {"id": signer["id"], "name": signer["name"], "status": signer["status"], "signed_at": signer["signed_at"]}
            for signer in signers
        ],
    }


@app.post("/api/files/{file_id}/fields")
def api_add_field(file_id: str, body: FieldIn):
    load_file(file_id)
    if not editable(file_id):
        raise HTTPException(status_code=409, detail="Fields are locked once someone has signed.")
    if body.kind not in KINDS:
        raise HTTPException(status_code=400, detail="Unknown field type.")
    signer = db.get_signer(body.signer_id)
    if not signer or signer["file_id"] != file_id:
        raise HTTPException(status_code=400, detail="Choose a signer on this file.")
    try:
        count = page_count(working_path(file_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.page >= count:
        raise HTTPException(status_code=400, detail="That page isn't in this agreement.")
    x, y, w, h = clamp_box(body.x, body.y, body.w, body.h)
    field_id = new_id()
    record = {
        "id": field_id,
        "file_id": file_id,
        "signer_id": signer["id"],
        "kind": body.kind,
        "page": body.page,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "label": {"signature": "Signature", "initials": "Initials", "name": "Name", "date": "Date"}[body.kind],
        "party": "client",
        "source": "manual",
    }
    db.create_field(record)
    db.add_event(file_id, "field_added", record["label"])
    return _field_payload(record, db.list_signers(file_id))


@app.patch("/api/files/{file_id}/fields/{field_id}")
def api_move_field(file_id: str, field_id: str, body: FieldPatch):
    load_file(file_id)
    if not editable(file_id):
        raise HTTPException(status_code=409, detail="Fields are locked once someone has signed.")
    field = db.get_field(require_id(field_id))
    if not field or field["file_id"] != file_id:
        raise HTTPException(status_code=404)
    changes: dict = {}
    if body.signer_id:
        signer = db.get_signer(body.signer_id)
        if not signer or signer["file_id"] != file_id:
            raise HTTPException(status_code=400, detail="Choose a signer on this file.")
        changes["signer_id"] = signer["id"]
    x = body.x if body.x is not None else field["x"]
    y = body.y if body.y is not None else field["y"]
    w = body.w if body.w is not None else field["w"]
    h = body.h if body.h is not None else field["h"]
    if any(value is not None for value in (body.x, body.y, body.w, body.h)):
        x, y, w, h = clamp_box(x, y, w, h)
        changes.update({"x": x, "y": y, "w": w, "h": h})
    db.update_field(field_id, **changes)
    updated = db.get_field(field_id)
    return _field_payload(updated, db.list_signers(file_id))


@app.delete("/api/files/{file_id}/fields/{field_id}")
def api_delete_field(file_id: str, field_id: str):
    load_file(file_id)
    if not editable(file_id):
        raise HTTPException(status_code=409, detail="Fields are locked once someone has signed.")
    field = db.get_field(require_id(field_id))
    if not field or field["file_id"] != file_id:
        raise HTTPException(status_code=404)
    db.delete_field(field_id)
    db.add_event(file_id, "field_removed", field["label"])
    return Response(status_code=204)


def _sign_context(token: str) -> tuple[dict, dict]:
    if not TOKEN_RE.match(token or ""):
        raise HTTPException(status_code=404)
    signer = db.get_signer_by_token(token)
    if not signer:
        raise HTTPException(status_code=404)
    record = db.get_file(signer["file_id"])
    if not record:
        raise HTTPException(status_code=404)
    return record, signer


@app.get("/sign/{token}/pages/{page_index}.png")
def sign_page_image(token: str, page_index: int):
    record, _signer = _sign_context(token)
    return Response(
        content=cached_page(record["id"], page_index),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/sign/{token}")
def sign_page(request: Request, token: str):
    record, signer = _sign_context(token)
    if signer["status"] != "signed" and record["accepting"] and signer["status"] == "pending":
        db.update_signer(signer["id"], status="viewed", viewed_at=db.utcnow())
        db.add_event(record["id"], "viewed", signer["name"], signer["id"], client_ip(request))
        signer = db.get_signer(signer["id"])
    pages = page_count(working_path(record["id"]))
    mine = db.fields_for_signer(signer["id"]) if signer["status"] != "signed" else []
    others = [item["name"].split()[0] for item in db.list_signers(record["id"]) if item["id"] != signer["id"] and item["status"] != "signed"]
    mode = "paused"
    if signer["status"] == "signed":
        mode = "done"
    elif record["accepting"]:
        mode = "sign"
    return render(
        request,
        "sign.html",
        public_page=True,
        file=record,
        signer=signer,
        pages=pages,
        fields=mine,
        mode=mode,
        others=others,
        today=agreement_date(),
        company_name=db.setting("company_name", ""),
    )


@app.post("/sign/{token}")
async def submit_signature(request: Request, token: str):
    record, signer = _sign_context(token)
    if signer["status"] == "signed":
        return redirect(f"/sign/{token}")
    if not record["accepting"]:
        return redirect(f"/sign/{token}", "This link is paused.")
    form = await request.form()
    full_name = clean_text(str(form.get("full_name") or ""), 120)
    mode = "drawn" if str(form.get("mode") or "") == "drawn" else "typed"
    if form.get("consent") != "on":
        return _sign_error(request, token, "Tick the box to agree this is your signature.")
    if len(full_name) < 2:
        return _sign_error(request, token, "Type the name you are signing with.")
    image = None
    if mode == "drawn":
        upload = form.get("signature_png")
        if upload is None or not getattr(upload, "filename", ""):
            return _sign_error(request, token, "Draw your signature, or switch to typing it.")
        image = await upload.read()
        if len(image) > 2_000_000 or not image.startswith(b"\x89PNG") or not png_has_ink(image):
            return _sign_error(request, token, "Draw your signature in the box, or switch to typing it.")
    fields = db.fields_for_signer(signer["id"])
    if not any(field["kind"] == "signature" for field in fields):
        return _sign_error(request, token, "There isn't a signature field assigned to you yet.")
    signed_at = datetime.now(timezone.utc)
    stamps = []
    for field in fields:
        stamp = {"page": field["page"], "x": field["x"], "y": field["y"], "w": field["w"], "h": field["h"], "kind": field["kind"]}
        if field["kind"] == "signature" and image:
            stamp["image"] = image
        elif field["kind"] == "signature":
            stamp["text"] = full_name
        elif field["kind"] == "initials":
            stamp["text"] = initials_for(full_name)
        elif field["kind"] == "name":
            stamp["text"] = full_name
        elif field["kind"] == "date":
            stamp["text"] = agreement_date(signed_at)
        else:
            continue
        stamps.append(stamp)
    try:
        stamp_fields(working_path(record["id"]), stamps)
    except Exception:
        log.exception("stamp failed")
        return _sign_error(request, token, "The signature couldn't be written onto the PDF. Try again.")
    ip = client_ip(request)
    agent = (request.headers.get("user-agent") or "")[:300]
    db.update_signer(
        signer["id"],
        status="signed",
        signature_mode=mode,
        ip=ip,
        user_agent=agent,
        signed_at=signed_at.replace(microsecond=0).isoformat(),
        name=full_name,
    )
    db.add_event(record["id"], "signed", f"{full_name} · {mode}", signer["id"], ip)
    try:
        refresh_status(record["id"])
    except Exception as exc:
        db.add_event(record["id"], "certificate_failed", str(exc)[:300])
    return redirect(f"/sign/{token}")


def _sign_error(request: Request, token: str, message: str):
    record, signer = _sign_context(token)
    pages = page_count(working_path(record["id"]))
    return render(
        request,
        "sign.html",
        public_page=True,
        status_code=400,
        file=record,
        signer=signer,
        pages=pages,
        fields=db.fields_for_signer(signer["id"]),
        mode="sign",
        others=[],
        today=agreement_date(),
        error=message,
    )


@app.get("/sign/{token}/download")
def signer_download(token: str):
    record, _signer = _sign_context(token)
    if record["status"] != "complete":
        raise HTTPException(status_code=404, detail="The finished agreement isn't ready yet.")
    return FileResponse(working_path(record["id"]), media_type="application/pdf", filename=safe_filename(record["title"], "signed"))


@app.exception_handler(404)
async def not_found(request: Request, _exc: HTTPException):
    if request.url.path.startswith("/api") or request.url.path.endswith(".png"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return render(
        request,
        "missing.html",
        status_code=404,
        public_page=request.url.path.startswith("/sign"),
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 404:
        return await not_found(request, exc)
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return render(request, "missing.html", status_code=exc.status_code, message=str(exc.detail))
