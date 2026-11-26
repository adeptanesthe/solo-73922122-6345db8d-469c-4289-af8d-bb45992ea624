"""HTTP routes: HTML pages, JSON APIs, audio and sample files."""

from __future__ import annotations

from typing import Any, Optional

from flask import (
    Blueprint,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from . import samples_dir
from .calibration import (
    MAX_SEGMENTS,
    MIN_SEGMENTS,
    SegmentError,
    calibrate,
    suggest_non_silent_ranges,
    waveform_peaks,
)
from .storage import (
    InfeasibleCalibration,
    create_question,
    create_workspace,
    list_questions,
    load_workspace_audio,
    public_question,
    question_dir,
    submit_answer,
)
from .wavutil import WavError

bp = Blueprint("web", __name__)

_FILE_A_KEYS = (
    "wav_a", "a", "file_a", "wavA", "A", "audio_a", "fileA",
    "a_wav", "audioA", "first", "file1",
)
_FILE_B_KEYS = (
    "wav_b", "b", "file_b", "wavB", "B", "audio_b", "fileB",
    "b_wav", "audioB", "second", "file2",
)


def wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept.split(",")[0]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@bp.get("/healthz")
@bp.get("/health")
def healthz():
    return jsonify({"ok": True, "service": "abx-studio"})


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/workspaces/<wid>/segments")
def segments_page(wid: str):
    try:
        load_workspace_audio(wid)
    except KeyError:
        abort(404)
    except WavError as e:
        return render_template("error.html", message=str(e)), 400
    return render_template("segments.html", wid=wid, max_segments=MAX_SEGMENTS)


@bp.get("/questions/<qid>")
def question_page(qid: str):
    try:
        question_dir(qid)
    except KeyError:
        abort(404)
    return render_template("question.html", qid=qid)


@bp.get("/questions")
def questions_page():
    return render_template("questions.html", questions=list_questions())


# ---------------------------------------------------------------------------
# Sample audio
# ---------------------------------------------------------------------------


@bp.get("/sample-audio/<path:filename>")
def sample_audio(filename: str):
    sdir = samples_dir()
    if filename not in ("sample_A.wav", "sample_B.wav"):
        abort(404)
    return send_from_directory(str(sdir), filename, mimetype="audio/wav")


@bp.route("/samples/use", methods=["GET", "POST"])
def use_samples():
    sdir = samples_dir()
    pa, pb = sdir / "sample_A.wav", sdir / "sample_B.wav"
    if not pa.is_file() or not pb.is_file():
        msg = "样音文件尚未生成：请先运行 python samples/make_samples.py"
        if wants_json():
            return jsonify({"ok": False, "error": msg}), 503
        return render_template("error.html", message=msg), 503
    meta = create_workspace(
        pa.read_bytes(), pb.read_bytes(), "sample_A.wav", "sample_B.wav"
    )
    target = url_for("web.segments_page", wid=meta["id"])
    if wants_json():
        return jsonify({"ok": True, "workspace_id": meta["id"], "redirect": target})
    return _redirect_see_other(target)


# ---------------------------------------------------------------------------
# Workspace upload + info
# ---------------------------------------------------------------------------


def _extract_pair():
    files = request.files
    fa = None
    fb = None
    for k in _FILE_A_KEYS:
        if k in files:
            fa = files[k]
            break
    for k in _FILE_B_KEYS:
        if k in files:
            fb = files[k]
            break

    # Fallback: take the first two distinct file parts in submission order.
    if fa is None or fb is None:
        all_files = [f for f in files.values() if f is not None and f.filename]
        if fa is None and all_files:
            fa = all_files[0]
        if fb is None:
            for cand in all_files:
                if cand is not fa:
                    fb = cand
                    break

    if fa is None or fb is None or not fa.filename or not fb.filename:
        raise WavError(
            "请同时选择 A、B 两份 WAV（表单字段 wav_a / wav_b，"
            "或 multipart 中按顺序给出两个文件）"
        )
    return fa.read(), fb.read(), fa.filename, fb.filename


@bp.post("/api/workspaces")
@bp.post("/upload")
def upload_workspace():
    try:
        raw_a, raw_b, name_a, name_b = _extract_pair()
        meta = create_workspace(raw_a, raw_b, name_a, name_b)
    except WavError as e:
        if wants_json():
            return jsonify({"ok": False, "error": str(e), "code": "invalid_wav"}), 400
        return render_template("index.html", upload_error=str(e)), 400

    target = url_for("web.segments_page", wid=meta["id"])
    if wants_json():
        return (
            jsonify(
                {
                    "ok": True,
                    "workspace_id": meta["id"],
                    "redirect": target,
                    "info": meta,
                }
            ),
            201,
        )
    return _redirect_see_other(target)


@bp.get("/api/workspaces/<wid>")
def workspace_info(wid: str):
    try:
        meta, _ra, _rb, a, b = load_workspace_audio(wid)
    except KeyError:
        return jsonify({"ok": False, "error": "工作区不存在或已过期"}), 404
    except WavError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify(
        {
            "ok": True,
            "id": wid,
            "filename_a": meta.get("filename_a"),
            "filename_b": meta.get("filename_b"),
            "sample_rate": 16000,
            "samples": int(len(a)),
            "duration": len(a) / 16000.0,
            "min_segments": MIN_SEGMENTS,
            "max_segments": MAX_SEGMENTS,
            "peaks_a": waveform_peaks(a),
            "peaks_b": waveform_peaks(b),
            "suggestions": suggest_non_silent_ranges(a, b),
        }
    )


# ---------------------------------------------------------------------------
# Calibration + question opening
# ---------------------------------------------------------------------------


def _segments_from_request() -> Any:
    body = request.get_json(silent=True)
    if body is not None:
        if isinstance(body, dict) and "segments" in body:
            return body["segments"]
        return body
    # Form fallback: segments as JSON text or repeated start/end fields.
    raw = request.form.get("segments")
    if raw:
        import json

        try:
            return json.loads(raw)
        except ValueError:
            raise SegmentError("segments 表单字段不是合法 JSON")
    starts = request.form.getlist("start")
    ends = request.form.getlist("end")
    if starts and ends:
        return [{"start": s, "end": e} for s, e in zip(starts, ends)]
    raise SegmentError("请求中没有 segments 区间数组")


@bp.post("/api/workspaces/<wid>/calibrate")
def api_calibrate(wid: str):
    try:
        _meta, _ra, _rb, a, b = load_workspace_audio(wid)
        segments = _segments_from_request()
        report = calibrate(a, b, segments)
    except KeyError:
        return jsonify({"ok": False, "error": "工作区不存在或已过期"}), 404
    except SegmentError as e:
        return jsonify({"ok": False, "error": str(e), "code": "invalid_segments"}), 400
    except WavError as e:
        return jsonify({"ok": False, "error": str(e), "code": "invalid_wav"}), 400
    return jsonify({"ok": True, **report})


@bp.post("/api/workspaces/<wid>/questions")
@bp.post("/workspaces/<wid>/questions")
def open_question(wid: str):
    try:
        segments = _segments_from_request()
        rec = create_question(wid, segments)
    except KeyError:
        msg = "工作区不存在或已过期，请重新上传音频"
        if wants_json():
            return jsonify({"ok": False, "error": msg}), 404
        abort(404)
    except SegmentError as e:
        if wants_json():
            return jsonify({"ok": False, "error": str(e), "code": "invalid_segments"}), 400
        return render_template("error.html", message=str(e)), 400
    except InfeasibleCalibration as e:
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "code": "infeasible",
                        "error": e.report.get("reason"),
                        "calibration": e.report,
                    }
                ),
                409,
            )
        return (
            render_template(
                "error.html",
                message="电平校准无解，不能开题：" + (e.report.get("reason") or ""),
            ),
            409,
        )

    target = url_for("web.question_page", qid=rec["id"])
    if wants_json():
        return jsonify({"ok": True, "redirect": target, "question": rec}), 201
    return _redirect_see_other(target)


# ---------------------------------------------------------------------------
# Question API + audio + answering
# ---------------------------------------------------------------------------


@bp.get("/api/questions")
def api_list_questions():
    return jsonify({"ok": True, "questions": list_questions()})


@bp.get("/api/questions/<qid>")
def api_question(qid: str):
    from .storage import get_question_record

    try:
        rec = get_question_record(qid)
    except KeyError:
        return jsonify({"ok": False, "error": "题目不存在"}), 404
    return jsonify({"ok": True, "question": public_question(rec)})


@bp.get("/questions/<qid>/audio/<kind>")
def question_audio(qid: str, kind: str):
    try:
        qdir = question_dir(qid)
    except KeyError:
        abort(404)

    k = kind.upper()
    if k == "A":
        fname = "a.wav"
    elif k == "B":
        fname = "b_calibrated.wav"
    elif k == "X":
        fname = "x.wav"
    else:
        abort(404)

    fpath = qdir / fname
    if not fpath.is_file():
        abort(404)
    data = fpath.read_bytes()
    resp = Response(data, mimetype="audio/wav")
    resp.headers["Accept-Ranges"] = "bytes"
    # The URL itself is opaque for X; caching per-URL is safe.
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["Content-Length"] = str(len(data))
    return resp


@bp.post("/api/questions/<qid>/answer")
def answer_question(qid: str):
    try:
        question_dir(qid)
    except KeyError:
        return jsonify({"ok": False, "error": "题目不存在"}), 404

    body = request.get_json(silent=True) or {}
    choice: Optional[str] = None
    if isinstance(body, dict):
        for key in ("choice", "answer", "judgment", "guess", "selection", "x_is"):
            if key in body and body[key] is not None:
                choice = str(body[key])
                break
    if choice is None:
        choice = request.form.get("choice") or request.form.get("answer")

    # Normalize common variants: "a"/"B"/"X_IS_A" etc.
    normalized = _normalize_choice(choice)
    if normalized is None:
        return (
            jsonify(
                {"ok": False, "error": "请提交你的判断：choice 必须是 'A' 或 'B'"}
            ),
            400,
        )
    try:
        result = submit_answer(qid, normalized)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify(result)


def _normalize_choice(choice: Any) -> Optional[str]:
    if choice is None:
        return None
    c = str(choice).strip().upper()
    if c in ("A", "B"):
        return c
    if c in ("X=A", "X_IS_A", "XISA", "甲"):
        return "A"
    if c in ("X=B", "X_IS_B", "XISB", "乙"):
        return "B"
    if c in ("1",):
        return "A"
    if c in ("2",):
        return "B"
    return None


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def _redirect_see_other(target: str):
    return Response(status=303, headers={"Location": target})
