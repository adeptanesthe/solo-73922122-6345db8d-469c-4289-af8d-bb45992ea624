#!/usr/bin/env python3
"""Black-box smoke test against a running ABX Studio server.

Uses only the Python standard library so it can run on the host against
``docker compose up``::

    python scripts/smoke.py                 # http://127.0.0.1:5000
    BASE=http://localhost:8080 python scripts/smoke.py

It loads the bundled samples, calibrates the first two suggested ranges,
opens a question, checks the answer does not leak, fetches A/B/X audio and
submits a judgment.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:5000").rstrip("/")
failures = 0


def check(cond, msg):
    global failures
    print(("  PASS: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures += 1


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, json.loads(e.read().decode())


def raw(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, r.read()


def main() -> int:
    st, health = call("GET", "/healthz")
    check(st == 200 and health.get("ok"), f"healthz 200 (got {st})")

    st, use = call("POST", "/samples/use")
    check(st in (200, 201) and use.get("workspace_id"), f"sample workspace created ({st})")
    wid = use["workspace_id"]

    st, info = call("GET", f"/api/workspaces/{wid}")
    check(st == 200 and len(info.get("suggestions", [])) >= 1,
          f"workspace info with suggestions ({len(info.get('suggestions', []))})")

    segs = [
        {"start_sample": s["start_sample"], "end_sample": s["end_sample"]}
        for s in info["suggestions"][:2]
    ]
    st, cal = call("POST", f"/api/workspaces/{wid}/calibrate", {"segments": segs})
    check(st == 200 and cal.get("feasible"),
          f"sample calibration feasible g={cal.get('gain')}")

    st, oq = call("POST", f"/api/workspaces/{wid}/questions", {"segments": segs})
    check(st == 201 and oq.get("question", {}).get("id"), f"question opened ({st})")
    qid = oq["question"]["id"]

    st, q = call("GET", f"/api/questions/{qid}")
    raw_text = json.dumps(q)
    check(st == 200 and q["question"]["status"] == "open", "question is open")
    check("x_is" not in raw_text and "answer" not in q["question"],
          "open question payload leaks no answer")

    for kind in ("A", "B", "X"):
        st, audio = raw(f"/questions/{qid}/audio/{kind}")
        check(st == 200 and audio[:4] == b"RIFF", f"audio {kind} is RIFF/WAV ({len(audio)} B)")

    st, ans = call("POST", f"/api/questions/{qid}/answer", {"choice": "A"})
    check(st == 200 and ans.get("x_is") in ("A", "B") and "correct" in ans,
          f"answer scored: X={ans.get('x_is')} correct={ans.get('correct')}")

    st, q2 = call("GET", f"/api/questions/{qid}")
    check(q2["question"].get("answer", {}).get("x_is") == ans["x_is"],
          "answer visible on refetch after submission")

    print()
    if failures:
        print(f"SMOKE FAILED: {failures} check(s)")
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
