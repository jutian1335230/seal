import io
import json

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app import ai, db
from app.main import app
from app.pdfutil import build_tricky_sample_pdf, detect_fields


def _unusual_pdf() -> bytes:
    # No "Signature:" label the rules know. The blank line sits above its caption.
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 96), "This agreement is between Northwind Studio and Jordan Hale.")
    page.insert_text((72, 600), "Executed by the client")
    page.insert_text((72, 640), "______________________________")
    page.insert_text((72, 656), "(authorised signatory)")
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    return buffer.getvalue()


def _line_id(lines, text):
    return next(line["id"] for line in lines if line["text"].startswith(text))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAL_DATA", str(tmp_path))
    monkeypatch.delenv("SEAL_ACCESS_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    db.init_db()
    with TestClient(app) as test_client:
        yield test_client


def test_rules_alone_miss_the_unusual_block(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(_unusual_pdf())
    fields, _rotated, report = detect_fields(path)
    assert fields == []
    assert report["used"] is False


def test_model_pick_becomes_a_box_on_the_blank_line(tmp_path, monkeypatch):
    path = tmp_path / "a.pdf"
    path.write_bytes(_unusual_pdf())
    seen = {}

    def fake_model(listing):
        seen["listing"] = listing
        line_id = next(int(row.split(" | ")[0]) for row in listing.splitlines() if row.endswith("___"))
        return json.dumps(
            {
                "parties": [{"name": "Northwind Studio", "role": "other"}, {"name": "Jordan Hale", "role": "client"}],
                "lines": [
                    {"id": line_id, "kind": "signature", "party": "client"},
                    {"id": 9999, "kind": "signature", "party": "client"},
                    {"id": line_id, "kind": "nonsense", "party": "client"},
                ],
            }
        )

    monkeypatch.setattr(ai, "ask_model", fake_model)
    fields, _rotated, report = detect_fields(path, ai.read_agreement)
    assert "Executed by the client" in seen["listing"]
    assert report["used"] is True
    assert {"name": "Jordan Hale", "role": "client"} in report["parties"]
    assert len(fields) == 1
    box = fields[0]
    assert box["kind"] == "signature" and box["source"] == "ai" and box["party"] == "client"
    # The box starts at the blank line, and its bottom rests on it.
    assert box["x"] < 0.15
    page_height = fitz.paper_size("a4")[1]
    assert 640 / page_height - 0.005 < box["y"] + box["h"] < 640 / page_height + 0.02


def test_a_failed_call_falls_back_to_the_rules(tmp_path, monkeypatch):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 640), "Signature: ________________________________")
    path = tmp_path / "b.pdf"
    doc.save(path)
    doc.close()

    def broken(_listing):
        raise RuntimeError("OpenAI returned 401: bad key")

    monkeypatch.setattr(ai, "ask_model", broken)
    fields, _rotated, report = detect_fields(path, ai.read_agreement)
    assert [field["kind"] for field in fields] == ["signature"]
    assert report["used"] is False
    assert "401" in report["error"]


def test_upload_records_what_the_model_found(client, monkeypatch):
    def fake_model(listing):
        line_id = next(int(row.split(" | ")[0]) for row in listing.splitlines() if row.endswith("___"))
        return json.dumps(
            {
                "parties": [{"name": "Jordan Hale", "role": "client"}],
                "lines": [{"id": line_id, "kind": "signature", "party": "client"}],
            }
        )

    monkeypatch.setattr(ai, "ask_model", fake_model)
    response = client.post(
        "/files",
        data={"title": "Unusual", "client_name": "Jordan Hale", "client_email": "jordan.hale@example.com"},
        files={"agreement": ("a.pdf", _unusual_pdf(), "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    file_id = response.headers["location"].rsplit("/", 1)[-1]
    payload = client.get(f"/api/files/{file_id}").json()
    assert [field["kind"] for field in payload["fields"]] == ["signature"]
    page = client.get(f"/files/{file_id}").text
    assert "OpenAI read the agreement" in page
    assert "Jordan Hale (client)" in page


def test_harder_sample_needs_the_model(tmp_path, monkeypatch):
    path = tmp_path / "tricky.pdf"
    path.write_bytes(build_tricky_sample_pdf())
    fields, _rotated, _report = detect_fields(path)
    assert fields == []

    def fake_model(listing):
        rows = [row.split(" | ", 2) for row in listing.splitlines()]
        page_two = [(int(line_id), text) for line_id, page, text in rows if page == "2"]
        blanks = [line_id for line_id, text in page_two if set(text) <= set("_ ")]
        names = [line_id for line_id, text in page_two if text.startswith("Name in full")]
        dates = [line_id for line_id, text in page_two if text.startswith("On (date)")]
        lines = []
        for index, party in ((0, "other"), (1, "client")):
            lines += [
                {"id": blanks[index], "kind": "signature", "party": party},
                {"id": names[index], "kind": "name", "party": party},
                {"id": dates[index], "kind": "date", "party": party},
            ]
        return json.dumps({"parties": [], "lines": lines})

    monkeypatch.setattr(ai, "ask_model", fake_model)
    fields, _rotated, report = detect_fields(path, ai.read_agreement)
    assert report["used"] is True
    assert [(field["kind"], field["party"]) for field in fields] == [
        ("signature", "other"),
        ("name", "other"),
        ("date", "other"),
        ("signature", "client"),
        ("name", "client"),
        ("date", "client"),
    ]


def test_harder_sample_route(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = client.post("/sample/tricky", follow_redirects=False)
    assert response.status_code == 303
    file_id = response.headers["location"].rsplit("/", 1)[-1]
    assert client.get(f"/api/files/{file_id}").json()["fields"] == []
    assert "No signature lines were found" in client.get(f"/files/{file_id}").text


def test_upload_still_works_when_the_model_fails(client, monkeypatch):
    def broken(_listing):
        raise RuntimeError("OpenAI returned 429: quota")

    monkeypatch.setattr(ai, "ask_model", broken)
    response = client.post(
        "/files",
        data={"title": "Unusual", "client_name": "Jordan Hale", "client_email": "jordan.hale@example.com"},
        files={"agreement": ("a.pdf", _unusual_pdf(), "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    file_id = response.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/files/{file_id}").text
    assert "didn&#39;t run" in page or "didn't run" in page
    assert "429" in page
