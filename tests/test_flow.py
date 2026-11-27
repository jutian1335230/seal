import io
import re

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.pdfutil import detect_fields


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAL_DATA", str(tmp_path))
    monkeypatch.delenv("SEAL_ACCESS_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    db.init_db()
    with TestClient(app) as test_client:
        yield test_client


def _pdf_with_signature_block() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 96),
        "This agreement requires a signature from the client before any work begins on the file.",
    )
    page.insert_text((72, 640), "Signature: ________________________________")
    page.insert_text((72, 700), "Printed name: ____________________________")
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    return buffer.getvalue()


def _blank_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 96), "No signature line is printed on this fictional agreement.")
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    return buffer.getvalue()


def _upload(client, pdf: bytes, name: str = "Jordan Hale"):
    response = client.post(
        "/files",
        data={
            "title": "Northwind engagement",
            "client_name": name,
            "client_email": "jordan.hale@example.com",
            "note": "Fictional test",
        },
        files={"agreement": ("agreement.pdf", pdf, "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"].rstrip("/").rsplit("/", 1)[-1]


def _token(client, file_id: str) -> str:
    page = client.get(f"/files/{file_id}")
    assert page.status_code == 200
    match = re.search(r"/sign/([A-Za-z0-9_-]+)", page.text)
    assert match, page.text
    return match.group(1)


def test_detector_ignores_sentences_and_keeps_labels(tmp_path):
    path = tmp_path / "agreement.pdf"
    path.write_bytes(_pdf_with_signature_block())
    fields, _rotated = detect_fields(path)
    kinds = [field["kind"] for field in fields]
    assert kinds.count("signature") == 1
    assert "name" in kinds
    signature = next(field for field in fields if field["kind"] == "signature")
    assert signature["y"] > 0.5

    doc = fitz.open(path)
    page = doc[0]
    line = next(
        block_line
        for block in page.get_text("dict")["blocks"]
        for block_line in block.get("lines", [])
        if "Signature" in "".join(span["text"] for span in block_line["spans"])
    )
    line_bottom = line["bbox"][3] / page.rect.height
    box_bottom = signature["y"] + signature["h"]
    doc.close()
    # The box rests on the line rather than straddling it.
    assert abs(box_bottom - line_bottom) < 0.01
    assert signature["y"] < line_bottom - signature["h"] * 0.8


def test_sample_file_finds_a_signature(client):
    created = client.post("/sample", follow_redirects=False)
    assert created.status_code == 303
    file_id = created.headers["location"].rstrip("/").rsplit("/", 1)[-1]
    payload = client.get(f"/api/files/{file_id}").json()
    kinds = {field["kind"] for field in payload["fields"]}
    assert {"signature", "name", "date"} <= kinds


def test_sign_an_unseen_agreement(client):
    file_id = _upload(client, _pdf_with_signature_block())
    preview = client.get(f"/files/{file_id}/pages/0.png")
    assert preview.status_code == 200
    assert preview.content.startswith(b"\x89PNG")

    token = _token(client, file_id)
    submitted = client.post(
        f"/sign/{token}",
        data={"full_name": "Jordan Hale", "mode": "typed", "consent": "on"},
        follow_redirects=False,
    )
    assert submitted.status_code == 303

    done = client.get(f"/sign/{token}")
    assert "You signed" in done.text
    office = client.get(f"/files/{file_id}")
    assert "Ready to start" in office.text

    signed = client.get(f"/files/{file_id}/download")
    original = client.get(f"/files/{file_id}/original")
    assert signed.content != original.content
    doc = fitz.open(stream=signed.content, filetype="pdf")
    text = "\n".join(page.get_text() for page in doc).replace("\xa0", " ").replace("\u2010", "-")
    assert "Certificate of completion" in text
    assert "Jordan Hale" in text
    assert "not a government-issued" in text
    assert "SHA-256" in text
    original_doc = fitz.open(stream=original.content, filetype="pdf")
    original_text = "\n".join(page.get_text() for page in original_doc)
    assert "Certificate of completion" not in original_text
    assert "requires a signature from the client" in text


def test_manual_field_when_nothing_is_detected(client):
    file_id = _upload(client, _blank_pdf())
    payload = client.get(f"/api/files/{file_id}").json()
    assert payload["fields"] == []
    created = client.post(
        f"/api/files/{file_id}/fields",
        json={
            "signer_id": payload["signers"][0]["id"],
            "kind": "signature",
            "page": 0,
            "x": 0.2,
            "y": 0.7,
            "w": 0.34,
            "h": 0.08,
        },
    )
    assert created.status_code == 200
    token = _token(client, file_id)
    submitted = client.post(
        f"/sign/{token}",
        data={"full_name": "Jordan Hale", "mode": "typed", "consent": "on"},
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    assert "Ready to start" in client.get(f"/files/{file_id}").text


def test_recall_pauses_the_link(client):
    file_id = _upload(client, _pdf_with_signature_block())
    token = _token(client, file_id)
    recalled = client.post(f"/files/{file_id}/recall", follow_redirects=False)
    assert recalled.status_code == 303
    paused = client.get(f"/sign/{token}")
    assert "paused" in paused.text.lower()


def test_rejects_a_non_pdf(client):
    response = client.post(
        "/files",
        data={"title": "Nope", "client_name": "Jordan Hale", "client_email": "jordan.hale@example.com"},
        files={"agreement": ("notes.txt", b"not a pdf", "text/plain")},
    )
    assert response.status_code == 400
    assert "PDF" in response.text


def test_workspace_key_locks_files_but_not_signing(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAL_DATA", str(tmp_path))
    monkeypatch.setenv("SEAL_ACCESS_KEY", "review-key")
    db.init_db()
    with TestClient(app) as locked:
        blocked = locked.get("/", follow_redirects=False)
        assert blocked.status_code == 303
        assert blocked.headers["location"] == "/unlock"
        opened = locked.post("/unlock", data={"key": "review-key"}, follow_redirects=False)
        assert opened.status_code == 303
        assert locked.get("/").status_code == 200
        file_id = _upload(locked, _pdf_with_signature_block())
        token = _token(locked, file_id)

    with TestClient(app) as stranger:
        assert stranger.get("/", follow_redirects=False).status_code == 303
        signing = stranger.get(f"/sign/{token}")
        assert signing.status_code == 200
        assert "Sign as" in signing.text
