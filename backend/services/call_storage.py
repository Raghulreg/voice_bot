"""Persist completed call transcripts and responses as JSON files."""

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CALL_DATA_DIR = Path(__file__).parent.parent / "call_data"


def ensure_call_data_dir() -> Path:
    CALL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return CALL_DATA_DIR


def _extract_qa_pairs(transcript: list[dict]) -> list[dict]:
    """Pair agent questions with the following customer answers."""
    pairs: list[dict] = []
    pending_question: str | None = None
    for turn in transcript:
        role = turn.get("role")
        text = turn.get("text", "")
        if role == "agent" and "?" in text:
            pending_question = text
        elif role == "customer" and pending_question:
            pairs.append({"question": pending_question, "answer": text})
            pending_question = None
    return pairs


def save_call_json(call_id: str, record: dict[str, Any]) -> Path:
    """Write call record to backend/call_data/{call_id}.json (atomic replace)."""
    ensure_call_data_dir()
    transcript = record.get("transcript", [])
    export = {
        **record,
        "saved_at": time.time(),
        "responses": [
            t for t in transcript
            if t.get("role") in ("customer", "agent")
        ],
        "qa_pairs": _extract_qa_pairs(transcript),
    }
    path = CALL_DATA_DIR / f"{call_id}.json"
    tmp_path = CALL_DATA_DIR / f"{call_id}.json.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(path)
    logger.info("Saved call data to %s", path)
    return path


def load_call_json(call_id: str) -> dict[str, Any] | None:
    """Load one call JSON. Corrupt / concatenated files return None (never raise)."""
    path = CALL_DATA_DIR / f"{call_id}.json"
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Recover first JSON object if file was appended to ("Extra data")
            decoder = json.JSONDecoder()
            data, _end = decoder.raw_decode(raw)
            logger.warning(
                "Call JSON had extra data; using first object only: %s",
                path.name,
            )
        if not isinstance(data, dict):
            logger.warning("Call JSON root is not an object: %s", path.name)
            return None
        return data
    except json.JSONDecodeError as e:
        logger.warning("Skipping corrupt call JSON %s: %s", path.name, e)
        return None
    except OSError as e:
        logger.warning("Could not read call JSON %s: %s", path.name, e)
        return None


def list_saved_call_files() -> list[Path]:
    ensure_call_data_dir()
    return sorted(CALL_DATA_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def list_all_call_records(in_memory: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge in-memory active calls with persisted JSON files (survives backend restart)."""
    merged: dict[str, dict[str, Any]] = {cid: dict(rec) for cid, rec in in_memory.items() if rec.get("call_id")}

    for path in list_saved_call_files():
        data = load_call_json(path.stem)
        if not data or not data.get("call_id"):
            continue
        cid = str(data["call_id"])
        existing = merged.get(cid)
        if not existing:
            merged[cid] = data
            continue
        existing_saved = float(existing.get("saved_at") or 0)
        disk_saved = float(data.get("saved_at") or 0)
        if disk_saved >= existing_saved:
            merged[cid] = data

    return sorted(merged.values(), key=lambda r: float(r.get("created_at") or 0), reverse=True)
