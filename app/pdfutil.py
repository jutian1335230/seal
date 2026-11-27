"""Read an unknown agreement, place fields, and write signatures back into it.

The words of the agreement are left alone. Ink goes into field boxes only.
A completion page is added at the end once everyone has signed.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "app" / "static" / "fonts"
SIGNATURE_FONT = FONT_DIR / "GreatVibes-Regular.ttf"
BODY_FONT = FONT_DIR / "Lato-Regular.ttf"
MAX_PAGES = 80

LABELS = [
    (re.compile(r"^(client\s+|authorized\s+)?signature\b", re.I), "signature"),
    (re.compile(r"^sign\s+here\b", re.I), "signature"),
    (re.compile(r"^signed\s+by\b", re.I), "signature"),
    (re.compile(r"^by\s*:?\s*$", re.I), "signature"),
    (re.compile(r"^initials?\b", re.I), "initials"),
    (re.compile(r"^(printed|print)\s+name\b", re.I), "name"),
    (re.compile(r"^full\s+name\b", re.I), "name"),
    (re.compile(r"^name\s*:?\s*$", re.I), "name"),
    (re.compile(r"^date\s*:?\s*$", re.I), "date"),
]

SIZES = {
    "signature": (0.32, 0.05),
    "initials": (0.1, 0.04),
    "name": (0.28, 0.032),
    "date": (0.2, 0.032),
}

KIND_LABELS = {
    "signature": "Signature",
    "initials": "Initials",
    "name": "Name",
    "date": "Date",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clamp_box(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    w = min(max(float(w), 0.04), 0.75)
    h = min(max(float(h), 0.025), 0.25)
    x = min(max(float(x), 0.0), 1 - w)
    y = min(max(float(y), 0.0), 1 - h)
    return round(x, 4), round(y, 4), round(w, 4), round(h, 4)


def open_doc(path: Path) -> fitz.Document:
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise ValueError("We couldn't open that PDF.") from exc
    if doc.needs_pass:
        doc.close()
        raise ValueError("This PDF is password protected. Upload an unlocked copy.")
    if doc.page_count == 0:
        doc.close()
        raise ValueError("This PDF has no pages.")
    if doc.page_count > MAX_PAGES:
        count = doc.page_count
        doc.close()
        raise ValueError(f"This PDF has {count} pages. The limit is {MAX_PAGES}.")
    return doc


def page_count(path: Path) -> int:
    doc = open_doc(path)
    try:
        return doc.page_count
    finally:
        doc.close()


def render_page_png(path: Path, page_index: int) -> bytes:
    doc = open_doc(path)
    try:
        if page_index < 0 or page_index >= doc.page_count:
            raise ValueError("That page does not exist.")
        pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _classify(text: str) -> str | None:
    # Long lines are sentences. A signature label is a short line, often ending in a colon.
    cleaned = re.sub(r"[_\.]{2,}", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    words = cleaned.replace(":", "").split()
    if not cleaned or len(words) > 5:
        return None
    for pattern, kind in LABELS:
        if pattern.search(cleaned):
            return kind
    return None


def _party(text: str) -> str:
    if re.search(r"\b(provider|company|contractor|consultant|vendor|supplier|agency)\b", text, re.I):
        return "other"
    return "client"


def _field(page: int, x: float, y: float, w: float, h: float, kind: str, party: str, source: str) -> dict:
    x, y, w, h = clamp_box(x, y, w, h)
    return {
        "page": page,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "kind": kind,
        "label": KIND_LABELS[kind],
        "party": party,
        "source": source,
    }


def _from_rect(page_index: int, rect: fitz.Rect, page_rect: fitz.Rect, kind: str, party: str, source: str) -> dict:
    pw, ph = page_rect.width, page_rect.height
    x = (rect.x0 - page_rect.x0) / pw
    y = (rect.y0 - page_rect.y0) / ph
    w = rect.width / pw
    h = rect.height / ph
    dw, dh = SIZES[kind]
    if w < 0.08:
        w = dw
    if h < 0.03:
        h = dh
    return _field(page_index, x, y, w, h, kind, party, source)


def _from_label(page_index: int, bbox: tuple, page_rect: fitz.Rect, kind: str, party: str) -> dict:
    pw, ph = page_rect.width, page_rect.height
    x0 = (bbox[0] - page_rect.x0) / pw
    x1 = (bbox[2] - page_rect.x0) / pw
    line_w = max(0.0, x1 - x0)
    baseline = (bbox[3] - page_rect.y0) / ph
    dw, dh = SIZES[kind]
    # A line with a blank after the label should be filled, not followed by a box in the margin.
    # Text is drawn from the top of the box, so the box has to start just above the line.
    drop = 0.62 if kind == "signature" else 0.78
    if line_w >= 0.2:
        w = min(dw, max(0.16, line_w * 0.5))
        x = max(x0 + line_w - w, x0 + min(0.16, line_w * 0.34))
        y = baseline - dh * drop
        h = dh
    else:
        x = x1 + 0.015
        y = baseline - dh * drop
        w, h = dw, dh
        if x + w > 0.97:
            x = x0
            y = baseline + 0.01
    return _field(page_index, x, y, w, h, kind, party, "detected")


def _near(left: dict, right: dict) -> bool:
    if left["page"] != right["page"] or left["kind"] != right["kind"]:
        return False
    ax = left["x"] + left["w"] / 2
    ay = left["y"] + left["h"] / 2
    bx = right["x"] + right["w"] / 2
    by = right["y"] + right["h"] / 2
    return abs(ax - bx) < 0.08 and abs(ay - by) < 0.04


def _widget_kind(widget: fitz.Widget) -> str | None:
    type_name = str(getattr(widget, "field_type_string", "") or "")
    field_name = str(getattr(widget, "field_name", "") or "")
    blob = f"{type_name} {field_name}".lower()
    if "sig" in blob:
        return "signature"
    if "init" in blob:
        return "initials"
    if "date" in blob:
        return "date"
    if "name" in blob:
        return "name"
    return None


def detect_fields(path: Path) -> tuple[list[dict], bool]:
    doc = open_doc(path)
    found: list[dict] = []
    rotated = False
    try:
        for page_index, page in enumerate(doc):
            if page.rotation:
                rotated = True
            page_rect = page.rect
            if page_rect.width <= 0 or page_rect.height <= 0:
                continue
            try:
                widgets = list(page.widgets() or [])
            except Exception:
                widgets = []
            for widget in widgets:
                kind = _widget_kind(widget)
                if not kind:
                    continue
                found.append(_from_rect(page_index, widget.rect, page_rect, kind, "client", "detected"))

            lines = []
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                    bbox = line.get("bbox")
                    if not text or not bbox:
                        continue
                    y_pct = (bbox[1] - page_rect.y0) / page_rect.height
                    lines.append({"text": text, "bbox": bbox, "y": y_pct})

            for index, line in enumerate(lines):
                kind = _classify(line["text"])
                if not kind:
                    continue
                above = " ".join(
                    prev["text"]
                    for prev in lines[max(0, index - 4) : index]
                    if 0 <= line["y"] - prev["y"] < 0.18 and len(prev["text"]) <= 40
                )
                party = _party(f"{above} {line['text']}")
                found.append(_from_label(page_index, line["bbox"], page_rect, kind, party))
    finally:
        doc.close()

    kept: list[dict] = []
    for field in found:
        if any(_near(field, previous) for previous in kept):
            continue
        kept.append(field)
    order = {"signature": 0, "name": 1, "date": 2, "initials": 3}
    kept.sort(key=lambda field: (field["page"], field["y"], order.get(field["kind"], 9)))
    return kept[:12], rotated


def png_has_ink(data: bytes) -> bool:
    image = None
    try:
        image = fitz.open(stream=data, filetype="png")
        pix = image[0].get_pixmap(alpha=True)
        samples = pix.samples
        channels = pix.n
        if channels < 4:
            return False
        ink = 0
        for index in range(3, len(samples), channels * 5):
            if samples[index] > 20:
                ink += 1
                if ink > 8:
                    return True
        return False
    except Exception:
        return False
    finally:
        if image is not None:
            image.close()


def _box(page: fitz.Page, stamp: dict) -> fitz.Rect:
    rect = page.rect
    return fitz.Rect(
        rect.x0 + stamp["x"] * rect.width,
        rect.y0 + stamp["y"] * rect.height,
        rect.x0 + (stamp["x"] + stamp["w"]) * rect.width,
        rect.y0 + (stamp["y"] + stamp["h"]) * rect.height,
    )


def _font_for(kind: str) -> tuple[str, str, tuple[float, float, float]]:
    if kind in {"signature", "initials"} and SIGNATURE_FONT.exists():
        return "seal-sig", str(SIGNATURE_FONT), (0.42, 0.08, 0.09)
    return "seal-body", str(BODY_FONT), (0.12, 0.1, 0.08)


def _write_text(page: fitz.Page, box: fitz.Rect, text: str, kind: str) -> None:
    fontname, fontfile, color = _font_for(kind)
    size = max(11, min(box.height * 0.72, 46)) if fontname == "seal-sig" else max(9, min(box.height * 0.62, 16))
    for _ in range(7):
        try:
            spare = page.insert_textbox(
                box,
                text,
                fontname=fontname,
                fontfile=fontfile,
                fontsize=size,
                color=color,
                align=fitz.TEXT_ALIGN_LEFT,
            )
        except Exception:
            if fontname != "seal-body":
                fontname, fontfile, color = "seal-body", str(BODY_FONT), (0.12, 0.1, 0.08)
                continue
            raise
        if spare >= 0:
            return
        size *= 0.82
    page.insert_textbox(
        box,
        text,
        fontname="seal-body",
        fontfile=str(BODY_FONT),
        fontsize=8,
        color=color,
        align=fitz.TEXT_ALIGN_LEFT,
    )


def stamp_fields(path: Path, stamps: list[dict]) -> None:
    doc = fitz.open(path)
    try:
        for stamp in stamps:
            page = doc[stamp["page"]]
            box = _box(page, stamp)
            image = stamp.get("image")
            if image:
                page.insert_image(box, stream=image, keep_proportion=True)
                continue
            text = (stamp.get("text") or "").strip()
            if text:
                _write_text(page, box, text, stamp["kind"])
        _replace(doc, path)
    except Exception:
        doc.close()
        raise


def already_certified(path: Path) -> bool:
    doc = fitz.open(path)
    try:
        if doc.page_count == 0:
            return False
        return "Certificate of completion" in doc[-1].get_text()
    finally:
        doc.close()


def _wrap(text: str, width: int) -> list[str]:
    if not text:
        return [""]
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def append_certificate(path: Path, lines: list[str]) -> None:
    if already_certified(path):
        return
    doc = fitz.open(path)
    try:
        page = doc.new_page(width=612, height=792)
        y = 62
        page.insert_text(
            (58, y),
            "Certificate of completion",
            fontsize=18,
            fontname="seal-body",
            fontfile=str(BODY_FONT),
            color=(0.35, 0.08, 0.08),
        )
        y += 14
        page.draw_line(fitz.Point(58, y), fitz.Point(554, y), color=(0.55, 0.2, 0.18), width=1)
        y += 26
        for line in lines:
            for part in _wrap(line, 92):
                if y > 740:
                    page = doc.new_page(width=612, height=792)
                    y = 62
                page.insert_text(
                    (58, y),
                    part,
                    fontsize=10,
                    fontname="seal-body",
                    fontfile=str(BODY_FONT),
                    color=(0.15, 0.12, 0.1),
                )
                y += 14
        _replace(doc, path)
    except Exception:
        doc.close()
        raise


def _replace(doc: fitz.Document, path: Path) -> None:
    temporary = path.with_suffix(".writing.pdf")
    doc.save(temporary, garbage=4, deflate=True)
    doc.close()
    os.replace(temporary, path)


def build_sample_pdf() -> bytes:
    doc = fitz.open()
    first = doc.new_page(width=612, height=792)
    body = dict(fontname="seal-body", fontfile=str(BODY_FONT), fontsize=12, color=(0.12, 0.1, 0.08))
    first.insert_textbox(
        fitz.Rect(64, 64, 548, 740),
        (
            "SERVICES AGREEMENT\n\n"
            "This is a fictional sample for trying Seal. It is not a real client's agreement, "
            "and the people named in it are not real.\n\n"
            "Northwind Studio (\"Provider\") and Jordan Hale (\"Client\") agree that work on "
            "this file does not start until this agreement is signed.\n\n"
            "1. The work is the job described on the file this agreement belongs to.\n"
            "2. Either party may stop the work with written notice.\n"
            "3. Each party keeps what it already owned.\n"
            "4. Nothing in this sample is legal advice.\n\n"
            "The signature block is on the next page."
        ),
        **body,
    )
    second = doc.new_page(width=612, height=792)
    second.insert_text((64, 78), "CLIENT", fontsize=11, fontname="seal-body", fontfile=str(BODY_FONT), color=(0.4, 0.16, 0.14))
    second.insert_text((64, 130), "Signature: ________________________________", **body)
    second.insert_text((64, 190), "Printed name: ____________________________", **body)
    second.insert_text((64, 250), "Date: ____________________________________", **body)
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    return buffer.getvalue()
