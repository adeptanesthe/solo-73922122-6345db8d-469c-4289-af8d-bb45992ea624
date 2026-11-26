"""Tests for the 1-second chunk ZIP packager + budget allocation + HTTP API.

Run:  python -m unittest tests.test_package -v
      (or python -m unittest discover -s tests)
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.packaging import (  # noqa: E402
    CHUNK_SAMPLES,
    PackageDoesNotFit,
    build_package,
    chunk_bounds,
    chunks_intersecting,
    high8_decode,
    high8_encode,
    knapsack_min_error,
    parse_budget,
    restore_zip,
    zip_sse_against,
)
from app.wavutil import SAMPLE_RATE, parse_pcm16_wav, write_pcm16_wav  # noqa: E402


def tone(freq=440.0, seconds=2.0, amp=8000, sr=SAMPLE_RATE):
    n = int(seconds * sr)
    t = np.arange(n) / sr
    env = np.minimum(1.0, np.arange(n) / 800.0) * \
        np.minimum(1.0, (n - np.arange(n)) / 800.0)
    return np.rint(amp * env * np.sin(2 * np.pi * freq * t)).astype(np.int16)


class High8Tests(unittest.TestCase):
    def test_known_mapping_vectors(self):
        x = np.array([-32768, -257, -256, -1, 0, 1, 255, 256, 32767],
                     dtype=np.int16)
        u = high8_encode(x)
        # floor(x/256)+128
        expected = ((x.astype(np.int32) >> 8) + 128).astype(np.uint8)
        np.testing.assert_array_equal(u, expected)
        self.assertEqual(int(u[0]), 0)      # -32768 -> 0
        self.assertEqual(int(u[4]), 128)    # 0 -> 128
        self.assertEqual(int(u[-1]), 255)   # 32767 -> 255

    def test_decode_places_byte_in_high_bits(self):
        u = np.array([0, 127, 128, 255], dtype=np.uint8)
        y = high8_decode(u)
        np.testing.assert_array_equal(
            y.astype(np.int32), [-32768, -256, 0, 32512]
        )

    def test_pcm16_option_is_lossless(self):
        x = tone(seconds=1)
        self.assertEqual(int(((x.astype(np.int32) - x.astype(np.int32)) ** 2).sum()), 0)


class ChunkBoundsTests(unittest.TestCase):
    def test_exact_second_boundaries(self):
        bounds = chunk_bounds(3 * CHUNK_SAMPLES)
        self.assertEqual(
            bounds,
            [(0, 16000), (16000, 32000), (32000, 48000)],
        )

    def test_tail_chunk_keeps_actual_length(self):
        n = 2 * CHUNK_SAMPLES + 1234
        bounds = chunk_bounds(n)
        self.assertEqual(len(bounds), 3)
        self.assertEqual(bounds[0], (0, 16000))
        self.assertEqual(bounds[1], (16000, 32000))
        self.assertEqual(bounds[2], (32000, 33234))
        self.assertEqual(bounds[-1][1] - bounds[-1][0], 1234)

    def test_intersection_half_open_edge_rules(self):
        bounds = chunk_bounds(4 * CHUNK_SAMPLES)
        # Segment exactly [1s,2s) intersects only chunk 1.
        hit = chunks_intersecting(
            [{"start_sample": 16000, "end_sample": 32000}], bounds
        )
        self.assertEqual(hit, [1])
        # Segment touching chunk edges from the middle still hits chunks 1,2.
        hit2 = chunks_intersecting(
            [{"start_sample": 20000, "end_sample": 33000}], bounds
        )
        self.assertEqual(hit2, [1, 2])


class BudgetParseTests(unittest.TestCase):
    def test_accepts_numbers_and_suffixes(self):
        self.assertEqual(parse_budget(1234), 1234)
        self.assertEqual(parse_budget("1234"), 1234)
        self.assertEqual(parse_budget(200000.0), 200000)
        self.assertEqual(parse_budget("2kb"), 2048)
        self.assertEqual(parse_budget("1 MB"), 1024 * 1024)

    def test_rejects_bad(self):
        for v in (0, -5, None, "", "abc", True):
            with self.assertRaises(Exception):
                parse_budget(v)


class KnapsackTests(unittest.TestCase):
    def test_all_fit(self):
        items = [("a", 3, 10), ("b", 4, 20)]
        tags, val, w = knapsack_min_error(items, 10)
        self.assertEqual(set(tags), {"a", "b"})
        self.assertEqual(val, 30)
        self.assertEqual(w, 7)

    def test_picks_highest_value_under_capacity(self):
        # capacity 5: b alone (v=9) beats a alone (v=5); cannot take both.
        items = [("a", 2, 5), ("b", 4, 9), ("c", 3, 6)]
        tags, val, w = knapsack_min_error(items, 5)
        self.assertEqual(val, 11)  # a + c = weight 5, value 11
        self.assertEqual(set(tags), {"a", "c"})
        self.assertLessEqual(w, 5)

    def test_zero_capacity_picks_nothing(self):
        tags, val, w = knapsack_min_error([("a", 2, 5)], 0)
        self.assertEqual(tags, [])
        self.assertEqual((val, w), (0, 0))

    def test_large_item_count_mitm_exact(self):
        rng = np.random.RandomState(7)
        items = [(i, int(rng.randint(1, 1000)), int(rng.randint(0, 10 ** 9)))
                 for i in range(24)]
        cap = 12000
        tags, val, w = knapsack_min_error(items, cap)
        self.assertLessEqual(w, cap)
        # Brute-force DP over weight to cross-check the MITM result.
        dp = [-1] * (cap + 1)
        dp[0] = 0
        for _tag, wt, vl in items:
            for ww in range(cap, wt - 1, -1):
                if dp[ww - wt] >= 0:
                    dp[ww] = max(dp[ww], dp[ww - wt] + vl)
        self.assertEqual(val, max(dp))


class PackageBuildTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.RandomState(42)
        n = 3 * CHUNK_SAMPLES + 777
        self.a = rng.randint(-16000, 16000, size=n).astype(np.int16)
        self.b = (self.a.astype(np.float64) * 0.8 +
                  rng.randint(-500, 500, n)).astype(np.int16)
        self.b = np.clip(self.b, -32768, 32767).astype(np.int16)

    def test_zip_size_model_exact_for_many_budgets(self):
        for budget in (90000, 120000, 150000, 200000, 400000):
            try:
                res = build_package(self.a, self.b, budget, [])
            except PackageDoesNotFit:
                continue
            self.assertEqual(res["zip_bytes"], res["predicted_zip_bytes"])
            self.assertEqual(len(res["zip_blob"]), res["zip_bytes"])
            self.assertLessEqual(res["zip_bytes"], budget)

    def test_zip_layout_and_wav_depths(self):
        res = build_package(self.a, self.b, 200000,
                            [{"start_sample": CHUNK_SAMPLES,
                              "end_sample": 2 * CHUNK_SAMPLES}],
                            manual_chunks=[3])
        blob = res["zip_blob"]
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.json", names)
            for i in range(4):
                self.assertIn(f"A/chunk_{i:03d}.wav", names)
                self.assertIn(f"B/chunk_{i:03d}.wav", names)
            # Chunk 1 forced by segment intersection, chunk 3 manual.
            self.assertEqual(res["chunks"][1]["bit_depth"], 16)
            self.assertTrue(res["chunks"][1]["intersection_forced"])
            self.assertEqual(res["chunks"][3]["bit_depth"], 16)
            self.assertTrue(res["chunks"][3]["manual_fidelity"])
            # Paired depths equal + actual WAV bit widths match.
            import wave
            for i, row in enumerate(res["chunks"]):
                with zf.open(f"A/chunk_{i:03d}.wav") as fh:
                    with wave.open(io.BytesIO(fh.read())) as wav:
                        self.assertEqual(wav.getsampwidth(),
                                         2 if row["bit_depth"] == 16 else 1)
                        self.assertEqual(wav.getframerate(), 16000)
                        self.assertEqual(wav.getnframes(), row["samples"])

    def test_restore_and_sse_accounting(self):
        res = build_package(self.a, self.b, 150000, [])
        sse = zip_sse_against(res["zip_blob"], self.a, self.b)
        self.assertEqual(sse, res["total_sse"])
        restored = restore_zip(res["zip_blob"])
        self.assertEqual(len(restored["a"]), len(self.a))
        # PCM16 chunks restore bit-exact; check at least one full-16 package.
        full16 = build_package(self.a, self.b, 10_000_000, [])
        self.assertEqual(full16["total_sse"], 0)
        back = restore_zip(full16["zip_blob"])
        np.testing.assert_array_equal(back["a"], self.a)
        np.testing.assert_array_equal(back["b"], self.b)

    def test_allocation_is_globally_optimal_vs_bruteforce(self):
        # 4 chunks -> enumerate all 2^4 forced sets, solve each via the same
        # exact builder and compare the optimizer's SSE at several caps.
        floor = build_package(self.a, self.b, 10_000_000, [])["minimum_bytes"]
        for budget in (floor, floor + 8000, floor + 25000, floor + 60000):
            res = build_package(self.a, self.b, budget, [])
            best_sse = None
            for mask in range(1 << 4):
                manual = [i for i in range(4) if mask & (1 << i)]
                try:
                    cand = build_package(self.a, self.b, budget, [], manual)
                except PackageDoesNotFit:
                    continue
                if best_sse is None or cand["total_sse"] < best_sse:
                    best_sse = cand["total_sse"]
            self.assertEqual(res["total_sse"], best_sse,
                             f"budget {budget} not optimal")

    def test_infeasible_raises_with_minimum(self):
        with self.assertRaises(PackageDoesNotFit) as ctx:
            build_package(self.a, self.b, 1, [])
        self.assertGreater(ctx.exception.report["minimum_bytes"], 1)

    def test_invalid_manual_index(self):
        with self.assertRaises(Exception):
            build_package(self.a, self.b, 10 ** 7, [], manual_chunks=[99])


class PackageHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(data_dir=self.tmp.name)
        self.client = self.app.test_client()
        a = tone(amp=9000, seconds=4)
        b = tone(amp=9500, seconds=4, freq=520)
        data = {
            "wav_a": (io.BytesIO(write_pcm16_wav(a)), "a.wav"),
            "wav_b": (io.BytesIO(write_pcm16_wav(b)), "b.wav"),
        }
        up = self.client.post("/api/workspaces", data=data,
                              content_type="multipart/form-data",
                              headers={"Accept": "application/json"})
        self.wid = up.get_json()["workspace_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, path, body):
        return self.client.post(path, json=body,
                                headers={"Accept": "application/json"})

    def test_full_package_reallocate_and_retention_flow(self):
        base = f"/api/workspaces/{self.wid}"

        # No package yet -> 200 with package:null (quiet UI probe).
        none_resp = self.client.get(f"{base}/package")
        self.assertEqual(none_resp.status_code, 200)
        self.assertIsNone(none_resp.get_json()["package"])

        # Impossible initial budget -> 409 code.
        bad = self._post(f"{base}/package", {"budget_bytes": 10})
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(bad.get_json()["code"], "package_does_not_fit")

        # Huge cap -> all four chunks PCM16, creation 201.
        ok = self._post(f"{base}/package", {"budget_bytes": 10_000_000})
        self.assertEqual(ok.status_code, 201, ok.get_data(as_text=True))
        pkg = ok.get_json()["package"]
        self.assertEqual(pkg["version"], 1)
        self.assertEqual(len(pkg["chunks"]), 4)
        self.assertEqual(pkg["pcm8_chunk_indices"], [])
        floor = pkg["minimum_bytes"]
        self.assertGreater(floor, 0)

        # Invalid budget -> 400.
        self.assertEqual(
            self._post(f"{base}/package", {"budget_bytes": "nope"}).status_code,
            400,
        )

        # Force chunk 2 manual with an exact-fit cap: floor + its delta.
        delta2 = pkg["chunks"][2]["extra_bytes_to_16"]
        fit_budget = floor + delta2
        re = self._post(f"{base}/package/reallocate",
                        {"manual_chunks": [2], "budget_bytes": fit_budget})
        self.assertEqual(re.status_code, 200, re.get_data(as_text=True))
        pkg2 = re.get_json()["package"]
        self.assertEqual(pkg2["version"], 2)
        self.assertIn(2, pkg2["manual_chunks"])
        self.assertEqual(pkg2["chunks"][2]["bit_depth"], 16)
        self.assertLessEqual(pkg2["zip_bytes"], fit_budget)

        # Download the exact stored ZIP and validate it.
        dl = self.client.get(f"{base}/package/download")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.mimetype, "application/zip")
        blob = dl.get_data()
        self.assertEqual(len(blob), pkg2["zip_bytes"])
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            self.assertEqual(zf.testzip(), None)
            self.assertIn("manifest.json", zf.namelist())

        # Restored endpoint returns parseable full-length PCM16 WAV.
        ra = self.client.get(f"{base}/package/restored/A.wav")
        self.assertEqual(ra.status_code, 200)
        parsed_a = parse_pcm16_wav(ra.get_data())
        self.assertEqual(len(parsed_a), 4 * SAMPLE_RATE)

        # Impossible reallocation -> 409 retained, old package + marks kept.
        fail = self._post(f"{base}/package/reallocate",
                          {"manual_chunks": [0, 2], "budget_bytes": 10})
        self.assertEqual(fail.status_code, 409)
        fbody = fail.get_json()
        self.assertEqual(fbody["code"], "package_does_not_fit")
        self.assertTrue(fbody["retained"])
        self.assertEqual(fbody["package"]["version"], 2)
        self.assertIn(2, fbody["package"]["manual_chunks"])

        cur = self.client.get(f"{base}/package").get_json()["package"]
        self.assertEqual(cur["version"], 2)
        self.assertEqual(cur["manual_chunks"], [2])
        dl2 = self.client.get(f"{base}/package/download").get_data()
        self.assertEqual(dl2, blob)  # previous ZIP bytes untouched

    def test_segments_force_intersection_chunks(self):
        base = f"/api/workspaces/{self.wid}"
        resp = self._post(f"{base}/package", {
            "budget_bytes": 10_000_000,
            "segments": [{"start_sample": 16000, "end_sample": 32000}],
        })
        pkg = resp.get_json()["package"]
        self.assertEqual(pkg["intersection_chunks"], [1])
        self.assertIn(1, pkg["forced_chunks"])

    def test_reallocate_without_package_is_404(self):
        r = self._post(f"/api/workspaces/{self.wid}/package/reallocate",
                       {"manual_chunks": [0]})
        self.assertEqual(r.status_code, 404)

    def test_unknown_workspace_package_404(self):
        self.assertEqual(
            self.client.get("/api/workspaces/w_nope/package").status_code, 404
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
