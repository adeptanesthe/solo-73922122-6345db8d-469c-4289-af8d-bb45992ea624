"""WSGI / direct-run entry point: ``python wsgi.py`` or gunicorn wsgi:app."""

import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Default to all interfaces so the page is reachable from port-forwarded
    # / container setups; override with HOST / PORT if desired.
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        threaded=True,
    )
