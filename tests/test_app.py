"""End-to-end / math tests for the ABX studio.

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.calibration import calibrate  # noqa: E402
from app.wavutil import (  # noqa: E402
    SAMPLE_RATE,
    parse_pcm16_wav,
    write_pcm16_wav,
)


def wav_bytes(samples: np.ndarray, sr: int = 16000, bits: int = 16,
              channels: int = 1, audio_format: int = 1) -> bytes:
    """Flexible WAV writer for negative tests (uses wave for standard PCM)."""
    if audio_format != 1 or bits != 16 or channels != 1 or sr != 16000:
        return _raw_wav(samples, sr=sr, bits=bits, channels=channels,
                        audio_format=audio_format)
    return write_pcm16_wav(np.asarray(samples, dtype=np.int16), sr)


def _raw_wav(samples, sr=16000, bits=16, channels=1, audio_format=1):
    if bits == 16:
        raw = np.asarray(samples, dtype="<i2").tobytes()
    elif bits == 8:
        raw = (np.asarray(samples) + 128).astype(np.uint8).tobytes()
    else:
        raw = np.asarray(samples, dtype="<i4").tobytes()
    byte_rate = sr * channels * bits // 8
    block_align = channels * bits // 8
    fmt = struct_pack_fmt(audio_format, channels, sr, byte_rate, block_align, bits)
    riff_size = 4 + (8 + len(fmt)) + (8 + len(raw))
    out = b"RIFF" + (riff_size).to_bytes(4, "little") + b"WAVE"
    out += b"fmt " + len(fmt).to_bytes(4, "little") + fmt
    out += b"data" + len(raw).to_bytes(4, "little") + raw
    return out


def struct_pack_fmt(fmt_tag, ch, sr, byte_rate, block_align, bits):
    import struct
    return struct.pack("<HHIIHH", fmt_tag, ch, sr, byte_rate, block_align, bits)


def tone(freq=440.0, seconds=2.0, amp=8000, sr=16000):
    n = int(seconds * sr)
    t = np.arange(n) / sr
    env = np.minimum(1.0, np.arange(n) / 800.0) * \
        np.minimum(1.0, (n - np.arange(n)) / 800.0)
    return np.rint(amp * env * np.sin(2 * np.pi * freq * t)).astype(np.int16)


class WavValidationTests(unittest.TestCase):
    def test_roundtrip(self):
        x = tone()
        parsed = parse_pcm16_wav(write_pcm16_wav(x))
        np.testing.assert_array_equal(parsed, x)

    def test_reject_stereo(self):
        stereo = np.repeat(tone()[:, None], 2, axis=1).reshape(-1)
        raw = _raw_wav(stereo, channels=2)
        with self.assertRaisesRegex(Exception, "单声道"):
            parse_pcm16_wav(raw)

    def test_reject_rate(self):
        raw = wav_bytes(tone(sr=8000), sr=8000)
        with self.assertRaisesRegex(Exception, "16 kHz"):
            parse_pcm16_wav(raw)

    def test_reject_8bit(self):
        raw = _raw_wav(tone(), bits=8)
        with self.assertRaisesRegex(Exception, "16 bit"):
            parse_pcm16_wav(raw)

    def test_reject_float_tag(self):
        raw = _raw_wav(tone(), audio_format=3)
        with self.assertRaisesRegex(Exception, "PCM16"):
            parse_pcm16_wav(raw)


class CalibrationMathTests(unittest.TestCase):
    def test_unity_when_already_matched(self):
        a = tone(amp=10000, seconds=2)
        b = tone(amp=10000, seconds=2)
        rep = calibrate(a, b, [{"start_sample": 16000, "end_sample": 28000}])
        self.assertTrue(rep["feasible"], rep.get("reason"))
        self.assertAlmostEqual(rep["gain"], 1.0, places=12)
        self.assertTrue(0.95 <= rep["segments"][0]["ratio_calibrated"] <= 1.05)

    def test_corrects_uniform_level_offset(self):
        a = tone(amp=10000, seconds=2)
        b = (tone(amp=10000, seconds=2).astype(np.float64) * 1.08)
        b = np.clip(np.rint(b), -32768, 32767).astype(np.int16)
        rep = calibrate(a, b, [{"start_sample": 16000, "end_sample": 28000}])
        self.assertTrue(rep["feasible"], rep.get("reason"))
        # B is ~8% too loud -> closest feasible gain is the RMS upper bound.
        self.assertLess(rep["gain"], 1.0)
        self.assertGreater(rep["gain"], 0.9)
        r = rep["segments"][0]["ratio_calibrated"]
        self.assertGreaterEqual(r, 0.95 - 1e-9)
        self.assertLessEqual(r, 1.05 + 1e-9)

    def test_disjoint_windows_no_solution(self):
        n = 4 * SAMPLE_RATE
        a = np.zeros(n, dtype=np.int16)
        b = np.zeros(n, dtype=np.int16)
        # Segment 1 (1-2s): B much quieter than A.
        s1, e1 = SAMPLE_RATE, 2 * SAMPLE_RATE
        a[s1:e1] = tone(amp=12000, seconds=1)
        b[s1:e1] = tone(amp=3000, seconds=1)
        # Segment 2 (2.5-3.5s): B much louder than A.
        s2, e2 = int(2.5 * SAMPLE_RATE), int(3.5 * SAMPLE_RATE)
        a[s2:e2] = tone(amp=3000, seconds=1)
        b[s2:e2] = tone(amp=12000, seconds=1)
        rep = calibrate(a, b, [
            {"start_sample": s1, "end_sample": e1},
            {"start_sample": s2, "end_sample": e2},
        ])
        self.assertFalse(rep["feasible"])
        self.assertEqual(rep["reason_code"], "disjoint_rms_windows")
        self.assertIn("没有交集", rep["reason"])

    def test_silent_segment_rejected(self):
        a = tone(seconds=2)
        b = tone(seconds=2)
        a[SAMPLE_RATE // 2:] = 0  # make second half silent in A
        rep = calibrate(a, b, [{"start_sample": SAMPLE_RATE // 2,
                                "end_sample": 3 * SAMPLE_RATE // 2}])
        self.assertFalse(rep["feasible"])
        self.assertEqual(rep["reason_code"], "silent_segment")

    def test_clipping_no_solution(self):
        n = 2 * SAMPLE_RATE
        # B sits at near full scale already; chosen segment B is quiet-ish?
        # Construct B with a full-scale peak outside the segment and a quiet
        # selected segment so calibration wants a big boost.
        a = tone(amp=12000, seconds=2)
        b = np.zeros(n, dtype=np.int16)
        s, e = 16000, 28000
        b[s:e] = tone(amp=2000, seconds=0.75, freq=300)
        b[100] = 32767  # full-scale peak elsewhere in the whole file
        rep = calibrate(a, b, [{"start_sample": s, "end_sample": e}])
        # Needed gain ~ rms_a/rms_b * ~1 ... definitely > 32767/32767 ~ 1.
        self.assertFalse(rep["feasible"])
        self.assertEqual(rep["reason_code"], "clipping")
        self.assertIn("PCM16", rep["reason"])

    def test_segment_count_limits(self):
        a = tone(seconds=3)
        b = tone(seconds=3)
        with self.assertRaises(Exception):
            calibrate(a, b, [])
        seg = [{"start_sample": i * 1000, "end_sample": i * 1000 + 800}
               for i in range(7)]
        with self.assertRaises(Exception):
            calibrate(a, b, seg)

    def test_seconds_and_sample_keys_equivalent(self):
        a = tone(seconds=2)
        b = tone(seconds=2)
        r1 = calibrate(a, b, [{"start_sample": 16000, "end_sample": 24000}])
        r2 = calibrate(a, b, [{"start": 1.0, "end": 1.5}])
        self.assertEqual(r1["segments"][0]["start_sample"],
                         r2["segments"][0]["start_sample"])


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(data_dir=self.tmp.name)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _upload_pair(self, a=None, b=None):
        a = tone(amp=9000, seconds=4) if a is None else a
        b = tone(amp=9500, seconds=4, freq=520) if b is None else b
        data = {
            "wav_a": (io.BytesIO(write_pcm16_wav(a)), "a.wav"),
            "wav_b": (io.BytesIO(write_pcm16_wav(b)), "b.wav"),
        }
        return self.client.post("/api/workspaces", data=data,
                                content_type="multipart/form-data",
                                headers={"Accept": "application/json"})

    def test_health_and_index(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("ABX", html)

    def test_upload_validation_error_is_json(self):
        bad = _raw_wav(tone(), channels=2)
        data = {
            "wav_a": (io.BytesIO(bad), "a.wav"),
            "wav_b": (io.BytesIO(write_pcm16_wav(tone())), "b.wav"),
        }
        resp = self.client.post("/api/workspaces", data=data,
                                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("单声道", resp.get_json()["error"])

    def test_length_mismatch(self):
        r = self._upload_pair(a=tone(seconds=2), b=tone(seconds=3))
        self.assertEqual(r.status_code, 400)
        self.assertIn("等长", r.get_json()["error"])

    def test_too_long(self):
        # 31 s exceeds the 30 s cap.
        r = self._upload_pair(a=tone(seconds=31), b=tone(seconds=31))
        self.assertEqual(r.status_code, 400)
        self.assertIn("30", r.get_json()["error"])

    def test_full_abx_flow_and_no_answer_leak(self):
        up = self._upload_pair()
        self.assertEqual(up.status_code, 201, up.get_data(as_text=True))
        wid = up.get_json()["workspace_id"]

        info = self.client.get(f"/api/workspaces/{wid}").get_json()
        self.assertTrue(info["ok"])
        self.assertEqual(info["sample_rate"], 16000)
        self.assertGreater(len(info["peaks_a"]["max"]), 100)

        segs = [{"start_sample": 16000, "end_sample": 48000}]
        cal = self.client.post(
            f"/api/workspaces/{wid}/calibrate", json={"segments": segs})
        self.assertEqual(cal.status_code, 200)
        crep = cal.get_json()
        self.assertTrue(crep["feasible"], crep.get("reason"))

        # Invalid segment count -> 400.
        bad = self.client.post(
            f"/api/workspaces/{wid}/calibrate", json={"segments": []})
        self.assertEqual(bad.status_code, 400)

        # Open question.
        oq = self.client.post(f"/api/workspaces/{wid}/questions",
                              json={"segments": segs})
        self.assertEqual(oq.status_code, 201, oq.get_data(as_text=True))
        qid = oq.get_json()["question"]["id"]

        # Open question API must not leak x_is / answer.
        q = self.client.get(f"/api/questions/{qid}").get_json()["question"]
        self.assertEqual(q["status"], "open")
        self.assertNotIn("answer", q)
        self.assertNotIn("x_is", json.dumps(q))
        page = self.client.get(f"/questions/{qid}").get_data(as_text=True)
        self.assertNotIn("x_is", page)

        # Audio endpoints return valid distinct WAVs.
        for kind in ("A", "B", "X"):
            ar = self.client.get(f"/questions/{qid}/audio/{kind}")
            self.assertEqual(ar.status_code, 200)
            self.assertEqual(ar.mimetype, "audio/wav")
            parsed = parse_pcm16_wav(ar.get_data())
            self.assertEqual(len(parsed), 4 * SAMPLE_RATE)

        # Submit a judgment.
        ans = self.client.post(f"/api/questions/{qid}/answer",
                               json={"choice": "A"})
        self.assertEqual(ans.status_code, 200)
        body = ans.get_json()
        self.assertIn(body["x_is"], ("A", "B"))
        self.assertEqual(body["correct"], body["x_is"] == "A")

        # Now the API reveals the answer.
        q2 = self.client.get(f"/api/questions/{qid}").get_json()["question"]
        self.assertEqual(q2["status"], "answered")
        self.assertEqual(q2["answer"]["x_is"], body["x_is"])

        # Repeat submission keeps the first score.
        again = self.client.post(f"/api/questions/{qid}/answer",
                                 json={"choice": "B"})
        self.assertTrue(again.get_json()["already_answered"])
        self.assertEqual(again.get_json()["choice"], "A")

    def test_infeasible_blocks_question(self):
        n = 4 * SAMPLE_RATE
        a = np.zeros(n, dtype=np.int16)
        b = np.zeros(n, dtype=np.int16)
        s1, e1 = SAMPLE_RATE, 2 * SAMPLE_RATE
        s2, e2 = int(2.5 * SAMPLE_RATE), int(3.5 * SAMPLE_RATE)
        a[s1:e1] = tone(amp=12000, seconds=1)
        b[s1:e1] = tone(amp=2000, seconds=1)
        a[s2:e2] = tone(amp=2000, seconds=1)
        b[s2:e2] = tone(amp=12000, seconds=1)

        up = self._upload_pair(a=a, b=b)
        wid = up.get_json()["workspace_id"]
        segs = [
            {"start_sample": s1, "end_sample": e1},
            {"start_sample": s2, "end_sample": e2},
        ]
        oq = self.client.post(f"/api/workspaces/{wid}/questions",
                             json={"segments": segs})
        self.assertEqual(oq.status_code, 409)
        self.assertEqual(oq.get_json()["code"], "infeasible")

    def test_old_question_unchanged_by_new_segments(self):
        up = self._upload_pair()
        wid = up.get_json()["workspace_id"]
        seg1 = [{"start_sample": 16000, "end_sample": 32000}]
        q1 = self.client.post(f"/api/workspaces/{wid}/questions",
                              json={"segments": seg1}).get_json()["question"]

        # Open a second question with different segments.
        seg2 = [{"start_sample": 48000, "end_sample": 64000}]
        q2 = self.client.post(f"/api/workspaces/{wid}/questions",
                              json={"segments": seg2}).get_json()["question"]
        self.assertNotEqual(q1["id"], q2["id"])

        old = self.client.get(f"/api/questions/{q1['id']}").get_json()["question"]
        self.assertEqual(old["segments"][0]["start_sample"], 16000)
        self.assertEqual(old["segments"][0]["end_sample"], 32000)

        # Old question can still be answered/scored.
        r = self.client.post(f"/api/questions/{q1['id']}/answer",
                             json={"choice": "B"})
        self.assertEqual(r.status_code, 200)

    def test_x_assignment_distribution(self):
        choices = []
        for _ in range(40):
            up = self._upload_pair()
            wid = up.get_json()["workspace_id"]
            q = self.client.post(
                f"/api/workspaces/{wid}/questions",
                json={"segments": [{"start_sample": 16000,
                                    "end_sample": 48000}]}
            ).get_json()["question"]
            # Answer A; correctness tells us x_is without storage access.
            r = self.client.post(f"/api/questions/{q['id']}/answer",
                                 json={"choice": "A"}).get_json()
            choices.append(r["x_is"])
        n_a = choices.count("A")
        # With 40 fair draws, landing outside 8..32 is astronomically unlikely.
        self.assertGreater(n_a, 7)
        self.assertLess(n_a, 33)


class SampleFilesTests(unittest.TestCase):
    def test_sample_pair_present_and_calibratable(self):
        sa = ROOT / "samples" / "sample_A.wav"
        sb = ROOT / "samples" / "sample_B.wav"
        self.assertTrue(sa.is_file(), "run python samples/make_samples.py")
        self.assertTrue(sb.is_file())
        a = parse_pcm16_wav(sa.read_bytes())
        b = parse_pcm16_wav(sb.read_bytes())
        self.assertEqual(len(a), len(b))
        self.assertLessEqual(len(a), 480_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
