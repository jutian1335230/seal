"""Optional OpenAI pass over the agreement text.

The model points at lines by id. It never supplies coordinates, so a bad answer can
only pick the wrong line, and the person preparing the file still checks every box.
"""

from __future__ import annotations

import json
import os

import httpx

KINDS = {"signature", "initials", "name", "date"}
PARTIES = {"client", "other"}
CHAR_BUDGET = 45_000
HEAD_LINES = 80

PROMPT = """You read a contract that a business needs its client to sign.
Each line of the contract is given as: id | page | text.

Return JSON with two keys.
"parties": the parties to the agreement, as [{"name": str, "role": "client" | "other"}].
"client" is the customer being asked to sign. "other" is the business providing the work.
"lines": every line where a person must sign, initial, print their name, or write a date
as part of executing the agreement, as [{"id": int, "kind": "signature" | "initials" | "name" | "date", "party": "client" | "other"}].
Include blank lines made of underscores when the label sits next to or under them.
Do not include dates or names that are already filled in, headings, or sentences in the body.
Return only the JSON object."""


def ai_enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def model_name() -> str:
    return os.environ.get("OPENAI_MODEL", "").strip() or "gpt-4o-mini"


def _budget(lines: list[dict]) -> list[dict]:
    rows = [{**line, "text": line["text"][:160]} for line in lines]
    total = sum(len(row["text"]) + 12 for row in rows)
    if total <= CHAR_BUDGET:
        return rows
    # Parties are named near the start and signature blocks sit near the end.
    head = rows[:HEAD_LINES]
    used = sum(len(row["text"]) + 12 for row in head)
    tail: list[dict] = []
    for row in reversed(rows[HEAD_LINES:]):
        cost = len(row["text"]) + 12
        if used + cost > CHAR_BUDGET:
            break
        tail.append(row)
        used += cost
    return head + list(reversed(tail))


def ask_model(listing: str) -> str:
    response = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}"},
        json={
            "model": model_name(),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": listing},
            ],
        },
        timeout=40,
    )
    if response.status_code >= 400:
        try:
            message = response.json().get("error", {}).get("message", "")
        except ValueError:
            message = response.text
        raise RuntimeError(f"OpenAI returned {response.status_code}: {message[:200]}")
    return response.json()["choices"][0]["message"]["content"]


def read_agreement(lines: list[dict]) -> dict:
    """lines: [{"id", "page", "text"}]. Returns {"parties": [...], "lines": [...]}."""
    rows = _budget(lines)
    listing = "\n".join(f"{row['id']} | {row['page'] + 1} | {row['text']}" for row in rows)
    payload = json.loads(ask_model(listing))
    known = {row["id"] for row in rows}

    parties = []
    for party in payload.get("parties") or []:
        if not isinstance(party, dict):
            continue
        name = str(party.get("name") or "").strip()[:120]
        role = party.get("role") if party.get("role") in PARTIES else "other"
        if name:
            parties.append({"name": name, "role": role})

    picked = []
    seen = set()
    for item in payload.get("lines") or []:
        if not isinstance(item, dict):
            continue
        try:
            line_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        kind = item.get("kind")
        if line_id not in known or kind not in KINDS or (line_id, kind) in seen:
            continue
        seen.add((line_id, kind))
        party = item.get("party") if item.get("party") in PARTIES else "client"
        picked.append({"id": line_id, "kind": kind, "party": party})
    return {"parties": parties[:6], "lines": picked[:24]}
