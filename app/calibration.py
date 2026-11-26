"""Segment parsing, RMS level calibration and waveform analysis.

Calibration rule (see project README):

For each selected segment ``i`` let

    r_i = RMS(B_i) / RMS(A_i)          (computed in float64, unquantized)

A single whole-program positive gain ``g`` applied to B must satisfy

    0.95 <= g * r_i <= 1.05

so each segment contributes an admissible gain interval
``[0.95 / r_i, 1.05 / r_i]``.  Intersect them all:

    G_rms_lo = max_i(0.95 / r_i)
    G_rms_hi = min_i(1.05 / r_i)

PCM16 clipping adds an upper bound computed against the exact int16
boundaries (-32768, 32767) on the *unquantized* scaled samples:

    g * max_positive(B) <= 32767
    g * max_negative_abs(B) <= 32768

The feasible set is ``[G_rms_lo, min(G_rms_hi, G_clip)]``.  The chosen gain
is the feasible point closest to 1 (clamp 1 into that interval).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

import numpy as np

from .wavutil import (
    PCM16_MAX,
    PCM16_MIN,
    SAMPLE_RATE,
    SILENCE_RMS_THRESHOLD,
    is_silent_rms,
)

RATIO_MIN = 0.95
RATIO_MAX = 1.05
MIN_SEGMENTS = 1
MAX_SEGMENTS = 6

# Relative tolerance for boundary comparisons so exact mathematical
# boundaries are not rejected by a single floating-point rounding step.
_EPS = 1e-12


class SegmentError(ValueError):
    """The submitted segment list / boundaries are invalid."""


def parse_segments(payload: Any, n_samples: int) -> List[Dict[str, Any]]:
    """Normalize the JSON segment payload into sample-exact half-open ranges.

    Accepted segment objects (all keys optional aliases):
      * samples:  ``start_sample`` / ``end_sample`` (also startSample/endSample)
      * seconds:  ``start`` / ``end`` / ``start_sec`` ...

    Returns a list of ``{"start_sample", "end_sample"}`` dicts.
    """
    if isinstance(payload, dict):
        # Allow {"segments": [...]} wrappers if a caller passes the body.
        payload = payload.get("segments", payload)
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise SegmentError("segments 必须是包含 1～6 个区间的数组")

    raw_list = list(payload)
    if not (MIN_SEGMENTS <= len(raw_list) <= MAX_SEGMENTS):
        raise SegmentError(
            f"对比段数量必须为 {MIN_SEGMENTS}～{MAX_SEGMENTS} 段，"
            f"当前提交了 {len(raw_list)} 段"
        )

    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_list, start=1):
        if not isinstance(item, dict):
            raise SegmentError(f"第 {idx} 段格式不正确，应为包含起止时间的对象")

        s = _find_number(
            item,
            (
                "start_sample",
                "startSample",
                "start_index",
                "startFrame",
                "start_frame",
            ),
        )
        e = _find_number(
            item,
            (
                "end_sample",
                "endSample",
                "end_index",
                "endFrame",
                "end_frame",
            ),
        )

        if s is not None or e is not None:
            if s is None or e is None:
                raise SegmentError(f"第 {idx} 段缺少 start_sample / end_sample")
            try:
                s_i = int(s)
                e_i = int(e)
            except (TypeError, ValueError):
                raise SegmentError(f"第 {idx} 段采样下标必须是整数")
        else:
            st = _find_number(
                item, ("start", "start_sec", "startSec", "start_time", "from")
            )
            en = _find_number(
                item, ("end", "end_sec", "endSec", "end_time", "to")
            )
            if st is None or en is None:
                raise SegmentError(
                    f"第 {idx} 段缺少 start/end（秒）或 start_sample/end_sample"
                )
            if not (math.isfinite(float(st)) and math.isfinite(float(en))):
                raise SegmentError(f"第 {idx} 段起止时间必须是有限数字")
            s_i = int(round(float(st) * SAMPLE_RATE))
            e_i = int(round(float(en) * SAMPLE_RATE))

        if not (0 <= s_i < e_i <= n_samples):
            raise SegmentError(
                f"第 {idx} 段区间无效：需要 0 ≤ start < end ≤ {n_samples}"
                f"（实际 start={s_i}, end={e_i}）"
            )
        out.append({"start_sample": s_i, "end_sample": e_i})
    return out


def _find_number(d: Dict[str, Any], keys: Sequence[str]):
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return None


def rms_float(x: np.ndarray) -> float:
    xf = x.astype(np.float64, copy=False)
    if xf.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(xf * xf)))


def clipping_gain_limit(b: np.ndarray) -> float:
    """Largest positive g keeping unquantized ``g*b`` inside PCM16 range."""
    limit = math.inf
    if b.size == 0:
        return limit
    pos = b[b > 0]
    if pos.size:
        limit = min(limit, PCM16_MAX / float(pos.max()))
    neg = b[b < 0]
    if neg.size:
        # b is negative: g*b >= PCM16_MIN  <=>  g <= 32768 / |b_min|
        limit = min(limit, abs(PCM16_MIN) / float((-neg).max()))
    return limit


def calibrate(a: np.ndarray, b: np.ndarray, segments: Sequence[Dict[str, Any]]):
    """Compute the common gain calibration.

    Returns a JSON-serializable report dict.  Never raises for "expected"
    no-solution conditions; instead returns ``feasible=False`` with
    ``reason_code`` and a Chinese ``reason``.  Raises
    :class:`SegmentError` only for malformed segment boundaries.
    """
    n = len(a)
    segs = parse_segments(segments, n)

    clip_limit = clipping_gain_limit(b)
    peak_abs = int(max(abs(int(b.min())) if b.size else 0,
                       abs(int(b.max())) if b.size else 0))

    rows: List[Dict[str, Any]] = []
    lo_bounds: List[float] = []
    hi_bounds: List[float] = []
    silent_indices: List[int] = []

    for idx, seg in enumerate(segs, start=1):
        s, e = seg["start_sample"], seg["end_sample"]
        ra = rms_float(a[s:e])
        rb = rms_float(b[s:e])
        silent_a = is_silent_rms(ra)
        silent_b = is_silent_rms(rb)
        if silent_a or silent_b:
            silent_indices.append(idx)

        if ra > 0 and rb > 0:
            ratio = rb / ra
            g_lo = RATIO_MIN / ratio
            g_hi = RATIO_MAX / ratio
        else:
            ratio = None
            g_lo = math.inf
            g_hi = -math.inf

        rows.append(
            {
                "index": idx,
                "start_sample": s,
                "end_sample": e,
                "start": s / SAMPLE_RATE,
                "end": e / SAMPLE_RATE,
                "duration": (e - s) / SAMPLE_RATE,
                "rms_a": ra,
                "rms_b": rb,
                "silent_a": silent_a,
                "silent_b": silent_b,
                "ratio_native": ratio,
                "gain_lower": g_lo if math.isfinite(g_lo) else None,
                "gain_upper": g_hi if math.isfinite(g_hi) else None,
                "ratio_calibrated": None,  # filled once gain is chosen
            }
        )
        if ratio is not None:
            lo_bounds.append(g_lo)
            hi_bounds.append(g_hi)

    base = {
        "sample_rate": SAMPLE_RATE,
        "samples": n,
        "ratio_target": [RATIO_MIN, RATIO_MAX],
        "silence_rms_threshold": SILENCE_RMS_THRESHOLD,
        "peak_abs_b": peak_abs,
        "clip_gain_limit": clip_limit if math.isfinite(clip_limit) else None,
        "segments": rows,
    }

    # ---- Reason 1: a selected segment is silent -------------------------
    if silent_indices:
        labels = "、".join(f"第 {i} 段" for i in silent_indices)
        which = []
        for i in silent_indices:
            row = rows[i - 1]
            parts = []
            if row["silent_a"]:
                parts.append("A")
            if row["silent_b"]:
                parts.append("B")
            which.append(f"第 {i} 段{'/'.join(parts)} 版")
        report = {
            **base,
            "feasible": False,
            "gain": None,
            "rms_gain_lower": None,
            "rms_gain_upper": None,
            "effective_gain_upper": None,
            "reason_code": "silent_segment",
            "reason": (
                f"{labels}存在低于静音阈值（RMS < "
                f"{SILENCE_RMS_THRESHOLD:.2f} LSB，约 -75 dBFS）的情况："
                + "、".join(which)
                + "。请重新圈出两版均有声音的区间。"
            ),
        }
        return _finalize_ratios(report, None, a, b)

    g_lo_max = max(lo_bounds)
    g_hi_min = min(hi_bounds)

    # Binding segments (for human-readable explanations).
    i_lo = max(range(len(segs)), key=lambda k: lo_bounds[k]) + 1
    i_hi = min(range(len(segs)), key=lambda k: hi_bounds[k]) + 1

    # ---- Reason 2: per-segment gain windows do not intersect ------------
    if g_lo_max > g_hi_min * (1.0 + _EPS):
        r_lo = rows[i_lo - 1]["ratio_native"]
        r_hi = rows[i_hi - 1]["ratio_native"]
        report = {
            **base,
            "feasible": False,
            "gain": None,
            "rms_gain_lower": g_lo_max,
            "rms_gain_upper": g_hi_min,
            "effective_gain_upper": min(g_hi_min, clip_limit),
            "reason_code": "disjoint_rms_windows",
            "binding_lower_segment": i_lo,
            "binding_upper_segment": i_hi,
            "reason": (
                "各对比段要求的增益区间没有交集，单个全片增益无法同时把"
                f"所有段的 RMS(B×g)/RMS(A) 校准到 {RATIO_MIN}～{RATIO_MAX}。"
                f"增益下界由第 {i_lo} 段决定（该段 B 相对 A 最轻，原生 RMS 比 "
                f"B/A={r_lo:.4f}），要求 g ≥ {g_lo_max:.5f}；"
                f"增益上界由第 {i_hi} 段决定（该段 B 相对 A 最响，B/A={r_hi:.4f}），"
                f"要求 g ≤ {g_hi_min:.5f}。请改选电平差异更一致的区间。"
            ),
        }
        return _finalize_ratios(report, None, a, b)

    effective_hi = min(g_hi_min, clip_limit)

    # ---- Reason 3: clipping prevents the required boost -----------------
    if g_lo_max > clip_limit * (1.0 + _EPS):
        report = {
            **base,
            "feasible": False,
            "gain": None,
            "rms_gain_lower": g_lo_max,
            "rms_gain_upper": g_hi_min,
            "effective_gain_upper": effective_hi,
            "binding_lower_segment": i_lo,
            "reason_code": "clipping",
            "reason": (
                f"校准响度至少需要 g ≥ {g_lo_max:.5f}"
                f"（第 {i_lo} 段 B 相对 A 偏轻），但 B 全片峰值绝对值为 "
                f"{peak_abs}，为保证乘增益后所有样点仍处于 PCM16 范围"
                f"（{PCM16_MIN}～{PCM16_MAX}），增益最多只能到 "
                f"{clip_limit:.5f}；再大就会削波。请改选不需要大幅提升 B "
                "的区间，或重新提供电平更匹配的音频。"
            ),
        }
        return _finalize_ratios(report, None, a, b)

    # ---- Feasible: pick the point of the interval closest to 1 ----------
    if 1.0 < g_lo_max:
        gain = g_lo_max
    elif 1.0 > effective_hi:
        gain = effective_hi
    else:
        gain = 1.0

    report = {
        **base,
        "feasible": True,
        "gain": gain,
        "gain_db": 20.0 * math.log10(gain),
        "distance_to_unity": abs(gain - 1.0),
        "rms_gain_lower": g_lo_max,
        "rms_gain_upper": g_hi_min,
        "effective_gain_upper": effective_hi,
        "reason_code": None,
        "reason": None,
    }
    return _finalize_ratios(report, gain, a, b)


def _finalize_ratios(
    report: Dict[str, Any], gain: float | None, a: np.ndarray, b: np.ndarray
) -> Dict[str, Any]:
    """Fill post-gain ratios / bounds for transparent UI display."""
    max_scaled = 0.0
    if gain is not None:
        for row in report["segments"]:
            s, e = row["start_sample"], row["end_sample"]
            ra = row["rms_a"]
            rb_g = rms_float(b[s:e].astype(np.float64) * gain)
            row["rms_b_calibrated"] = rb_g
            row["ratio_calibrated"] = (rb_g / ra) if ra > 0 else None
        if b.size:
            max_scaled = float(np.max(np.abs(b.astype(np.float64) * gain)))
    report["max_scaled_abs"] = max_scaled
    report["pcm16_range"] = [PCM16_MIN, PCM16_MAX]
    return report


# ---------------------------------------------------------------------------
# Waveform peaks + automatic non-silent range suggestions (UI helpers)
# ---------------------------------------------------------------------------


def waveform_peaks(x: np.ndarray, buckets: int = 1600) -> Dict[str, List[float]]:
    """Return min/max peaks per bucket for canvas waveform rendering."""
    n = int(x.shape[0])
    buckets = max(1, min(int(buckets), n if n else 1))
    mins: List[float] = []
    maxs: List[float] = []
    if n == 0:
        return {"min": [], "max": [], "buckets": 0}
    edges = np.linspace(0, n, buckets + 1, dtype=np.int64)
    xf = x.astype(np.float64, copy=False)
    for k in range(buckets):
        s, e = int(edges[k]), int(edges[k + 1])
        if e <= s:
            continue
        mins.append(float(xf[s:e].min()))
        maxs.append(float(xf[s:e].max()))
    return {"min": mins, "max": maxs, "buckets": len(mins)}


def suggest_non_silent_ranges(
    a: np.ndarray,
    b: np.ndarray,
    frame_ms: float = 20.0,
    min_duration: float = 0.45,
    merge_gap: float = 0.25,
    max_suggestions: int = 12,
) -> List[Dict[str, Any]]:
    """Find aligned ranges where *both* A and B are non-silent.

    Purely a convenience for the selection page; the server re-validates
    every chosen segment with the same RMS predicate.
    """
    n = len(a)
    win = max(1, int(SAMPLE_RATE * frame_ms / 1000.0))
    n_frames = n // win
    if n_frames == 0:
        return []

    af = a[: n_frames * win].astype(np.float64).reshape(n_frames, win)
    bf = b[: n_frames * win].astype(np.float64).reshape(n_frames, win)
    rms_a = np.sqrt(np.mean(af * af, axis=1))
    rms_b = np.sqrt(np.mean(bf * bf, axis=1))
    active = (~is_silent_rms_array(rms_a)) & (~is_silent_rms_array(rms_b))

    # Runs of active frames.
    runs: List[List[int]] = []
    start = None
    for i, v in enumerate(active):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append([start, i])  # half-open frame indices
            start = None
    if start is not None:
        runs.append([start, n_frames])

    # Merge runs separated by short gaps.
    merged: List[List[int]] = []
    gap_frames = int(round(merge_gap * SAMPLE_RATE / win))
    for run in runs:
        if merged and run[0] - merged[-1][1] <= gap_frames:
            merged[-1][1] = run[1]
        else:
            merged.append(run)

    min_frames = int(round(min_duration * SAMPLE_RATE / win))
    out: List[Dict[str, Any]] = []
    for f0, f1 in merged:
        if f1 - f0 < min_frames:
            continue
        s = f0 * win
        e = min(n, f1 * win)
        out.append(
            {
                "start_sample": int(s),
                "end_sample": int(e),
                "start": s / SAMPLE_RATE,
                "end": e / SAMPLE_RATE,
                "duration": (e - s) / SAMPLE_RATE,
                "rms_a": rms_float(a[s:e]),
                "rms_b": rms_float(b[s:e]),
            }
        )
        if len(out) >= max_suggestions:
            break
    return out


def is_silent_rms_array(v: np.ndarray) -> np.ndarray:
    return v < SILENCE_RMS_THRESHOLD
