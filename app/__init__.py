"""ABX blind-listening platform package."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask


def samples_dir() -> Path:
    """Repository-level directory containing the bundled sample WAV pair."""
    return Path(__file__).resolve().parent.parent / "samples"


def create_app(data_dir: str | os.PathLike | None = None) -> Flask:
    if data_dir is not None:
        # Must be set before storage is imported/used.
        os.environ["ABX_DATA_DIR"] = str(data_dir)

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # two ~1 MB WAVs

    @app.template_filter("datetimeformat")
    def _datetimeformat(ts):
        import datetime as _dt

        try:
            return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return ""

    from .routes import bp

    app.register_blueprint(bp)

    @app.errorhandler(413)
    def too_large(_e):
        from flask import jsonify, request

        msg = "上传内容过大：每份 WAV 至多 30 秒 16 kHz 单声道 PCM16（约 0.96 MB）"
        if request.path.startswith("/api/") or "application/json" in request.headers.get(
            "Accept", ""
        ):
            return jsonify({"ok": False, "error": msg}), 413
        return msg, 413

    @app.errorhandler(404)
    def not_found(_e):
        from flask import jsonify, render_template, request

        wants_json = (
            request.path.startswith("/api/")
            or "/audio/" in request.path
            or "application/json" in request.headers.get("Accept", "")
        )
        if wants_json:
            return jsonify({"ok": False, "error": "资源不存在"}), 404
        return render_template("error.html", message="页面或题目不存在。"), 404

    @app.errorhandler(500)
    def server_error(_e):  # pragma: no cover - defensive
        from flask import jsonify, render_template, request

        if request.path.startswith("/api/") or "application/json" in request.headers.get("Accept", ""):
            return jsonify({"ok": False, "error": "服务器内部错误"}), 500
        return render_template("error.html", message="服务器内部错误，请查看日志。"), 500

    return app


# Module-level WSGI callable so ``flask --app app`` and ``app:app`` style
# servers discover an application instance directly.  ``wsgi:app`` remains
# the documented / Docker entry point.
app = create_app()
