"""File-system persistence for upload workspaces and ABX questions.

Each uploaded A/B pair gets a workspace directory.  Each opened ABX
question gets an immutable directory containing its own copy of A, the
rendered gain-calibrated B and the opaque X file, plus ``record.json``
(which also stores the fixed answer).  Re-selecting segments and opening
new questions therefore never changes the audio / scoring of old ones.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from .calibration import calibrate
from .wavutil import gain_limited_samples, validate_pair, write_pcm16_wav

_LOCK = threading.Lock()


def data_dir() -> Path:
    return Path(os.environ.get("ABX_DATA_DIR", "data")).resolve()


def _ws_root() -> Path:
    p = data_dir() / "workspaces"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _q_root() -> Path:
    p = data_dir() / "questions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Workspaces (one uploaded, validated A/B pair)
# ---------------------------------------------------------------------------


def create_workspace(
    raw_a: bytes,
    raw_b: bytes,
    filename_a: str = "",
    filename_b: str = "",
) -> Dict[str, Any]:
    # Full validation happens before anything is persisted.
    a, b = validate_pair(raw_a, raw_b)

    wid = "w_" + secrets.token_hex(6)
    wdir = _ws_root() / wid
    wdir.mkdir(parents=True, exist_ok=False)
    (wdir / "a.wav").write_bytes(raw_a)
    (wdir / "b.wav").write_bytes(raw_b)
    meta = {
        "id": wid,
        "created_at": time.time(),
        "filename_a": filename_a or "A.wav",
        "filename_b": filename_b or "B.wav",
        "samples": int(len(a)),
        "sample_rate": 16000,
    }
    _atomic_write_json(wdir / "meta.json", meta)
    return meta


def workspace_dir(wid: str) -> Path:
    # Guard against path traversal: ids are token hex but validate anyway.
    if not wid.replace("_", "").isalnum() or ".." in wid:
        raise KeyError(wid)
    p = _ws_root() / wid
    if not p.is_dir():
        raise KeyError(wid)
    return p


def load_workspace_audio(wid: str):
    wdir = workspace_dir(wid)
    raw_a = (wdir / "a.wav").read_bytes()
    raw_b = (wdir / "b.wav").read_bytes()
    a, b = validate_pair(raw_a, raw_b)
    meta = _read_json(wdir / "meta.json")
    return meta, raw_a, raw_b, a, b


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


def question_dir(qid: str) -> Path:
    if not qid.replace("_", "").isalnum() or ".." in qid:
        raise KeyError(qid)
    p = _q_root() / qid
    if not p.is_dir():
        raise KeyError(qid)
    return p


def get_question_record(qid: str) -> Dict[str, Any]:
    return _read_json(question_dir(qid) / "record.json")


def create_question(wid: str, segments_payload: Any) -> Dict[str, Any]:
    """Calibrate against *current* segment choice and freeze a question."""
    meta, raw_a, raw_b, a, b = load_workspace_audio(wid)
    report = calibrate(a, b, segments_payload)
    if not report["feasible"]:
        raise InfeasibleCalibration(report)

    gain = float(report["gain"])
    b_cal = gain_limited_samples(b, gain)
    b_cal_wav = write_pcm16_wav(b_cal, 16000)

    # Equal-probability X assignment, fixed at question opening.
    x_is = "A" if secrets.randbelow(2) == 0 else "B"

    qid = "q_" + secrets.token_hex(6)
    qdir = _q_root() / qid
    with _LOCK:
        qdir.mkdir(parents=True, exist_ok=False)
        try:
            (qdir / "a.wav").write_bytes(raw_a)
            (qdir / "b_calibrated.wav").write_bytes(b_cal_wav)
            # Opaque X file: its URL never reveals A vs B.
            x_bytes = raw_a if x_is == "A" else b_cal_wav
            (qdir / "x.wav").write_bytes(x_bytes)

            record = {
                "id": qid,
                "workspace_id": wid,
                "created_at": time.time(),
                "filename_a": meta.get("filename_a", "A.wav"),
                "filename_b": meta.get("filename_b", "B.wav"),
                "sample_rate": 16000,
                "samples": int(len(a)),
                "duration": len(a) / 16000.0,
                "segments": report["segments"],
                "gain": gain,
                "gain_db": report.get("gain_db"),
                "calibration": {
                    "ratio_target": report["ratio_target"],
                    "rms_gain_lower": report["rms_gain_lower"],
                    "rms_gain_upper": report["rms_gain_upper"],
                    "effective_gain_upper": report["effective_gain_upper"],
                    "clip_gain_limit": report["clip_gain_limit"],
                    "peak_abs_b": report["peak_abs_b"],
                    "max_scaled_abs": report["max_scaled_abs"],
                    "silence_rms_threshold": report["silence_rms_threshold"],
                },
                # Answer / scoring fields:
                "x_is": x_is,
                "answered": False,
                "choice": None,
                "correct": None,
                "answered_at": None,
            }
            _atomic_write_json(qdir / "record.json", record)
        except Exception:
            # Avoid leaving a half-written question directory behind.
            import shutil

            shutil.rmtree(qdir, ignore_errors=True)
            raise
    return public_question(record)


class InfeasibleCalibration(Exception):
    def __init__(self, report: Dict[str, Any]):
        super().__init__(report.get("reason") or "calibration infeasible")
        self.report = report


def submit_answer(qid: str, choice: str) -> Dict[str, Any]:
    choice = str(choice).strip().upper()
    if choice not in ("A", "B"):
        raise ValueError("choice 必须是 'A' 或 'B'")

    qdir = question_dir(qid)
    rec_path = qdir / "record.json"
    with _LOCK:
        rec = _read_json(rec_path)
        already = bool(rec.get("answered"))
        if not already:
            rec["answered"] = True
            rec["choice"] = choice
            rec["correct"] = choice == rec["x_is"]
            rec["answered_at"] = time.time()
            _atomic_write_json(rec_path, rec)
        result = {
            "ok": True,
            "question_id": qid,
            "already_answered": already,
            "choice": rec["choice"],
            "x_is": rec["x_is"],
            "correct": rec["correct"],
            "answered_at": rec["answered_at"],
            "gain": rec["gain"],
        }
        if already:
            result["note"] = "该题已提交过判断，以第一次提交的判分为准。"
        return result


def public_question(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize a question without leaking the answer while it is open."""
    answered = bool(rec.get("answered"))
    out: Dict[str, Any] = {
        "id": rec["id"],
        "workspace_id": rec.get("workspace_id"),
        "created_at": rec.get("created_at"),
        "sample_rate": rec.get("sample_rate", 16000),
        "samples": rec.get("samples"),
        "duration": rec.get("duration"),
        "segments": rec.get("segments", []),
        "gain": rec.get("gain"),
        "gain_db": rec.get("gain_db"),
        "calibration": rec.get("calibration", {}),
        "status": "answered" if answered else "open",
        "audio_urls": {
            "A": f"/questions/{rec['id']}/audio/A",
            "B": f"/questions/{rec['id']}/audio/B",
            "X": f"/questions/{rec['id']}/audio/X",
        },
    }
    if answered:
        out["answer"] = {
            "x_is": rec["x_is"],
            "choice": rec["choice"],
            "correct": rec["correct"],
            "answered_at": rec["answered_at"],
        }
    return out


def list_questions() -> List[Dict[str, Any]]:
    root = _q_root()
    items: List[Dict[str, Any]] = []
    for qdir in root.glob("q_*"):
        rec_path = qdir / "record.json"
        if not rec_path.is_file():
            continue
        try:
            rec = _read_json(rec_path)
        except (json.JSONDecodeError, OSError):
            continue
        answered = bool(rec.get("answered"))
        items.append(
            {
                "id": rec["id"],
                "created_at": rec.get("created_at"),
                "workspace_id": rec.get("workspace_id"),
                "segment_count": len(rec.get("segments", [])),
                "gain": rec.get("gain"),
                "status": "answered" if answered else "open",
                "choice": rec.get("choice") if answered else None,
                "x_is": rec.get("x_is") if answered else None,
                "correct": rec.get("correct") if answered else None,
            }
        )
    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return items
