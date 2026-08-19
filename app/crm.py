"""FPI CRM helpers (SQLite)."""
from __future__ import annotations

import json
import uuid
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def get_lead(db_path: str, lead_id: str) -> dict[str, Any] | None:
    with connect(db_path) as con:
        row = con.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None


def list_leads(db_path: str, limit: int = 50) -> list[dict[str, Any]]:
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT id, first_name, last_name, full_name, status, qualified, phone_primary, "
            "property_address, retell_call_id, updated_at FROM leads "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_status(
    db_path: str,
    lead_id: str,
    status: str,
    *,
    actor: str = "retell",
    note: str = "",
) -> None:
    import uuid

    with connect(db_path) as con:
        prev = con.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
        from_s = prev["status"] if prev else None
        con.execute(
            "UPDATE leads SET status = ?, updated_at = ?, owner_agent = COALESCE(owner_agent, ?) WHERE id = ?",
            (status, utcnow(), actor, lead_id),
        )
        con.execute(
            "INSERT INTO status_history (id, lead_id, from_status, to_status, actor, note, at) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), lead_id, from_s, status, actor, note, utcnow()),
        )


def add_activity(
    db_path: str,
    lead_id: str,
    kind: str,
    summary: str,
    *,
    actor: str = "retell",
    payload: dict | None = None,
) -> None:
    """Write activities row. Schema: id, lead_id, actor, type, payload_json, at."""
    import uuid

    body = dict(payload or {})
    if summary:
        body.setdefault("summary", summary)
    with connect(db_path) as con:
        con.execute(
            "INSERT INTO activities (id, lead_id, actor, type, payload_json, at) VALUES (?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                lead_id,
                actor,
                kind,
                json.dumps(body),
                utcnow(),
            ),
        )


def mark_call(
    db_path: str,
    lead_id: str,
    *,
    call_id: str | None = None,
    status: str | None = None,
    qualified: str | None = None,
    alex_notes: str | None = None,
    extra: dict | None = None,
) -> None:
    fields = ["updated_at = ?"]
    vals: list[Any] = [utcnow()]
    if call_id is not None:
        fields.append("retell_call_id = ?")
        vals.append(call_id)
    if status is not None:
        fields.append("status = ?")
        vals.append(status)
    if qualified is not None:
        fields.append("qualified = ?")
        vals.append(qualified)
        if qualified.upper() in ("Y", "YES"):
            fields.append("qualified_at = ?")
            vals.append(utcnow())
    if alex_notes is not None:
        fields.append("alex_notes = ?")
        vals.append(alex_notes)
    if extra:
        for k, v in extra.items():
            fields.append(f"{k} = ?")
            vals.append(v)
    vals.append(lead_id)
    with connect(db_path) as con:
        if status is not None:
            prev = con.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
            from_s = prev["status"] if prev else None
            con.execute(
                "INSERT INTO status_history (id, lead_id, from_status, to_status, actor, note, at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), lead_id, from_s, status, "retell", f"call {call_id or ''}", utcnow()),
            )
        con.execute(f"UPDATE leads SET {', '.join(fields)} WHERE id = ?", vals)


def ensure_retell_columns(db_path: str) -> None:
    """Add retell_call_id if missing (safe migrate)."""
    with connect(db_path) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(leads)").fetchall()}
        if "retell_call_id" not in cols:
            con.execute("ALTER TABLE leads ADD COLUMN retell_call_id TEXT")
        if "last_retell_event" not in cols:
            con.execute("ALTER TABLE leads ADD COLUMN last_retell_event TEXT")
        if "lisa_notes" not in cols:
            con.execute("ALTER TABLE leads ADD COLUMN lisa_notes TEXT")
        if "lisa_checklist_json" not in cols:
            con.execute("ALTER TABLE leads ADD COLUMN lisa_checklist_json TEXT")
        if "lisa_checklist_at" not in cols:
            con.execute("ALTER TABLE leads ADD COLUMN lisa_checklist_at TEXT")


def ensure_elevenlabs_columns(db_path: str) -> None:
    """Add ElevenLabs conversation tracking columns (safe migrate)."""
    with connect(db_path) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(leads)").fetchall()}
        wanted = {
            "elevenlabs_conversation_id": "TEXT",
            "elevenlabs_agent_id": "TEXT",
            "last_elevenlabs_event": "TEXT",
            "elevenlabs_audio_path": "TEXT",
            "elevenlabs_failure_reason": "TEXT",
        }
        for name, typ in wanted.items():
            if name not in cols:
                con.execute(f"ALTER TABLE leads ADD COLUMN {name} {typ}")


_LEAD_WRITE_COLS = {
    "first_name",
    "last_name",
    "full_name",
    "phone_primary",
    "phones_json",
    "email_primary",
    "emails_json",
    "property_address",
    "property_city",
    "property_state",
    "property_zip",
    "status",
    "owner_agent",
    "ai_call_consent",
    "website_opt_in",
    "consent_text",
    "preferred_call_window",
    "best_time_to_call",
    "lisa_notes",
    "alex_notes",
    "appointment_at",
    "dnc_flag",
    "stop_reason",
    "motivation",
    "timeline",
    "beds",
    "baths",
    "sqft",
    "year_built",
    "garage_type",
    "lot_size_acres",
    "house_info_summary",
    "retell_call_id",
    "last_retell_event",
    "lisa_checklist_json",
    "lisa_checklist_at",
    "source_platform",
    "source_url",
    "elevenlabs_conversation_id",
    "elevenlabs_agent_id",
    "last_elevenlabs_event",
    "elevenlabs_audio_path",
    "elevenlabs_failure_reason",
}


def upsert_lead_fields(db_path: str, lead_id: str, fields: dict[str, Any], *, actor: str = "lisa") -> dict[str, Any]:
    """Update existing lead or insert minimal row."""
    clean = {k: v for k, v in fields.items() if k in _LEAD_WRITE_COLS and k != "id"}
    now = utcnow()
    with connect(db_path) as con:
        exists = con.execute("SELECT id, status FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not exists:
            cols = ["id", "created_at", "updated_at"] + list(clean.keys())
            placeholders = ",".join("?" * len(cols))
            vals = [lead_id, now, now] + [clean[k] for k in clean]
            con.execute(f"INSERT INTO leads ({','.join(cols)}) VALUES ({placeholders})", vals)
            if clean.get("status"):
                con.execute(
                    "INSERT INTO status_history (id, lead_id, from_status, to_status, actor, note, at) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), lead_id, None, clean.get("status"), actor, "lisa upsert create", now),
                )
        else:
            if not clean:
                return dict(con.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())
            sets = ", ".join(f"{k} = ?" for k in clean)
            vals = list(clean.values()) + [now, lead_id]
            prev_status = exists["status"]
            con.execute(f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?", vals)
            new_status = clean.get("status")
            if new_status and new_status != prev_status:
                con.execute(
                    "INSERT INTO status_history (id, lead_id, from_status, to_status, actor, note, at) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), lead_id, prev_status, new_status, actor, "lisa checklist / handoff", now),
                )
        row = con.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else {"id": lead_id}
