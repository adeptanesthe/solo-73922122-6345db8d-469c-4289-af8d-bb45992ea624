"""1-second chunk ZIP packaging with budget-optimal bit-depth allocation.

Requirement handled here
------------------------
The listening/preview attachment has a size cap.  From the original A/B
tracks (16 kHz mono PCM16, equal length):

* split **each** track into 1-second chunks; the final chunk keeps its real
  (shorter) length;
* chunk *i* of A and chunk *i* of B always share one bit depth: either
  untouched PCM16 or 8-bit WAV carrying the **top 8 bits** of each sample
  (standard unsigned PCM8: ``u8 = (s16 >> 8) + 128``);
* every chunk intersecting a circled "fidelity segment" is forced PCM16,
  as are manually marked fidelity chunks;
* all chunk WAVs are packed into one ZIP with DEFLATE;
* under a user-supplied whole-ZIP byte budget, choose depths minimizing the
  total PCM16 restoration squared error over **both** tracks.

Optimization
------------
A PCM16 chunk pair has zero error; downgrading it to PCM8 incurs a known
integer SSE cost and saves a known exact number of ZIP bytes (DEFLATE is a
per-entry raw stream and ZIP framing is additive).  After the mandatory
PCM16 chunks and the globally-cheapest baseline are fixed, allocation is a
small 0/1 knapsack (at most 30 chunks for the 30 s upload cap), solved
exactly with meet-in-the-middle.

The ZIP size model is exact for :func:`render_zip` (fixed DOS timestamps,
empty extra/comment fields, fixed-size padded stored manifest); unit tests
assert predicted == produced byte length for randomized allocations.
"""

from __future__ import annotations

import io
import json
import math
import operator
import struct
import wave
import zipfile
import zlib
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from .calibration import SegmentError, parse_segments
from .wavutil import SAMPLE_RATE, WavError, parse_pcm16_wav, write_pcm16_wav

CHUNK_SECONDS = 1.0
CHUNK_SAMPLES = SAMPLE_RATE  # 16000 samples == 1 second
DEPTH_PCM16 = 16
DEPTH_PCM8 = 8

# DEFLATE level used for every WAV entry; cost measurement must use the same.
DEFLATE_LEVEL = 9

# The in-ZIP manifest is padded with JSON-insignificant spaces to this exact
# length so its byte contribution is constant across allocations.
MANIFEST_SLOT = 4096
MANIFEST_NAME = "manifest.json"
PACKAGE_FORMAT = "abx-chunk-zip/1"

# ZIP framing constants (APPNOTE): local/central fixed fields + EOCD.
_LOCAL_FIXED = 30
_CENTRAL_FIXED = 46
_EOCD_FIXED = 22

_FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)

# Payload sanity bound for the byte cap (200 MB); real audio is <= ~2 MB.
MAX_BUDGET_BYTES = 200 * 1024 * 1024
MAX_FIDELITY_RANGES = 100


class PackageError(ValueError):
    """Bad package request (invalid budget / chunk index / segment)."""


class PackageDoesNotFit(Exception):
    """Even the minimum-depth ZIP exceeds the supplied byte budget."""

    def __init__(self, report: Dict[str, Any]):
        self.report = report
        super().__init__(report.get("error") or "package does not fit budget")


# ---------------------------------------------------------------------------
# Chunking + PCM8 high-8-bit conversion
# ---------------------------------------------------------------------------


def chunk_bounds(
    n_samples: int, chunk_samples: int = CHUNK_SAMPLES
) -> List[Tuple[int, int]]:
    """Half-open ``[start, end)`` bounds of the 1 s chunks; last is short."""
    if n_samples <= 0:
        return []
    out: List[Tuple[int, int]] = []
    start = 0
    while start < n_samples:
        end = min(n_samples, start + chunk_samples)
        out.append((start, end))
        start = end
    return out


def high8_encode(samples: np.ndarray) -> np.ndarray:
    """Take the top 8 bits of signed PCM16 and return standard unsigned PCM8.

    ``u8 = floor(s16 / 256) + 128`` which is identical to an arithmetic
    right shift by 8 followed by the unsigned-8 center offset.  Mapping:
    ``-32768 -> 0``, ``0 -> 128``, ``32767 -> 255``.
    """
    x = np.asarray(samples, dtype=np.int32)
    q = (x >> 8) + 128  # numpy signed shift is arithmetic
    return np.clip(q, 0, 255).astype(np.uint8)


def high8_decode(pcm8: np.ndarray) -> np.ndarray:
    """Restore unsigned PCM8 high bytes to PCM16 LSB-aligned samples.

    ``s16 = (u8 - 128) * 256`` (equivalent to sign-extended high byte << 8).
    """
    u = np.asarray(pcm8, dtype=np.int32)
    y = (u - 128) << 8
    return np.clip(y, -32768, 32767).astype("<i2")


def write_pcm8_wav(pcm8: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Encode a 1-D uint8 array as mono 8-bit PCM WAV (unsigned by spec)."""
    arr = np.ascontiguousarray(np.asarray(pcm8, dtype=np.uint8))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(sample_rate)
        wf.writeframes(arr.tobytes())
    return buf.getvalue()


def encode_chunk_wav(samples: np.ndarray, bit_depth: int) -> bytes:
    if bit_depth == DEPTH_PCM16:
        return write_pcm16_wav(samples)
    if bit_depth == DEPTH_PCM8:
        return write_pcm8_wav(high8_encode(samples))
    raise PackageError(f"不支持的位深：{bit_depth}（只允许 16 或 8）")


def quantization_sse(original: np.ndarray, bit_depth: int) -> int:
    """Squared error after encode -> restore-to-PCM16 for one track slice."""
    if bit_depth == DEPTH_PCM16:
        return 0
    if bit_depth != DEPTH_PCM8:
        raise PackageError(f"不支持的位深：{bit_depth}")
    orig = np.asarray(original, dtype=np.int32)
    restored = high8_decode(high8_encode(original)).astype(np.int32)
    diff = orig - restored
    return int((diff * diff).sum(dtype=np.int64))


# ---------------------------------------------------------------------------
# Request normalization
# ---------------------------------------------------------------------------


def parse_budget(value: Any) -> int:
    """Parse the user byte cap; accepts ints, integer floats and strings."""
    if isinstance(value, bool) or value is None:
        raise PackageError("请填写 ZIP 字节上限（正整数，单位：字节）")
    b: Any
    if isinstance(value, int):
        b = value
    elif isinstance(value, float):
        if not math.isfinite(value) or value <= 0:
            raise PackageError("字节上限必须是正整数")
        b = int(round(value))
    elif isinstance(value, str):
        text = value.strip().lower().replace("字节", "").replace("bytes", "").strip()
        mult = 1
        for suffix, factor in (("mb", 1024 * 1024), ("m", 1024 * 1024),
                               ("kb", 1024), ("k", 1024)):
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                mult = factor
                break
        try:
            b = int(round(float(text) * mult)) if mult != 1 else int(text)
        except ValueError:
            raise PackageError(f"无法解析字节上限：{value!r}，请填写正整数字节数")
    else:
        raise PackageError("字节上限必须是正整数（单位：字节）")

    if isinstance(b, bool) or not isinstance(b, int):
        raise PackageError("字节上限必须是正整数")
    if b <= 0:
        raise PackageError("字节上限必须大于 0")
    if b > MAX_BUDGET_BYTES:
        raise PackageError(f"字节上限过大：{b}（最多支持 {MAX_BUDGET_BYTES} 字节）")
    return b


def normalize_fidelity_ranges(payload: Any, n_samples: int) -> List[Dict[str, int]]:
    """Lenient segment parser for packaging: zero segments are allowed."""
    if payload is None:
        return []
    try:
        return parse_segments(
            payload,
            n_samples,
            min_count=0,
            max_count=MAX_FIDELITY_RANGES,
            count_label=f"0～{MAX_FIDELITY_RANGES} 段",
        )
    except SegmentError:
        raise
    except Exception as e:  # defensive: never let weird payloads escape 500
        raise PackageError(f"保真段数据无法解析：{e}") from e


def normalize_manual_chunks(value: Any, n_chunks: int) -> List[int]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)) or isinstance(value, (str, bytes)):
        raise PackageError("manual_chunks 必须是块编号数组")
    out = set()
    for item in value:
        if isinstance(item, bool):
            raise PackageError("保真块编号必须是整数")
        try:
            idx = operator.index(item)  # strict for numpy-ish / ints
        except TypeError:
            try:
                idx = int(item)
            except (TypeError, ValueError):
                raise PackageError(f"保真块编号无效：{item!r}")
        if not 0 <= idx < n_chunks:
            raise PackageError(
                f"保真块编号 {idx} 越界：共有 {n_chunks} 块（0～{n_chunks - 1}）"
            )
        out.add(idx)
    return sorted(out)


def chunks_intersecting(
    ranges: Sequence[Dict[str, int]],
    bounds: Sequence[Tuple[int, int]],
) -> List[int]:
    """Indices of chunks whose half-open range overlaps any fidelity range."""
    hit = set()
    for i, (cs, ce) in enumerate(bounds):
        for r in ranges:
            ss, se = int(r["start_sample"]), int(r["end_sample"])
            if ss < ce and se > cs:  # strict: touching at an edge does not overlap
                hit.add(i)
                break
    return sorted(hit)


# ---------------------------------------------------------------------------
# Exact ZIP byte accounting
# ---------------------------------------------------------------------------


def deflate_payload_len(data: bytes, level: int = DEFLATE_LEVEL) -> int:
    """Length of the raw DEFLATE stream zipfile will write for *data*."""
    co = zlib.compressobj(level, zlib.DEFLATED, -15)
    return len(co.compress(data) + co.flush())


def zip_entry_byte_len(arcname: str, data: bytes, compressed: bool = True) -> int:
    """Exact bytes one writestr entry contributes (local+data+central)."""
    name_len = len(arcname.encode("utf-8"))
    payload = deflate_payload_len(data) if compressed else len(data)
    return _LOCAL_FIXED + name_len + payload + _CENTRAL_FIXED + name_len


def manifest_entry_constant(slot: int = MANIFEST_SLOT) -> int:
    name_len = len(MANIFEST_NAME.encode("utf-8"))
    # Stored (no compression); payload length is exactly the padded slot.
    return _LOCAL_FIXED + name_len + slot + _CENTRAL_FIXED + name_len


def zip_constant_bytes() -> int:
    return _EOCD_FIXED + manifest_entry_constant()


def chunk_entry_names(index: int) -> Tuple[str, str]:
    stem = f"chunk_{index:03d}.wav"
    return f"A/{stem}", f"B/{stem}"


# ---------------------------------------------------------------------------
# Meet-in-the-middle 0/1 knapsack
# ---------------------------------------------------------------------------


def knapsack_min_error(
    items: Sequence[Tuple[Any, int, int]], capacity: int
) -> Tuple[List[Any], int, int]:
    """Exact 0/1 knapsack.

    ``items`` are ``(tag, weight>0, value>=0)``.  Maximizes total value under
    total weight <= capacity; ties prefer the smaller total weight.  At most
    30 items in this app, so enumerate halves (<= 2^15 each).

    Returns ``(chosen_tags, chosen_value, chosen_weight)``.
    """
    if capacity < 0:
        raise ValueError("capacity must be non-negative")
    if not items:
        return [], 0, 0

    total_weight = sum(w for _, w, _ in items)
    total_value = sum(v for _, _, v in items)
    if total_weight <= capacity:
        return [tag for tag, _, _ in items], total_value, total_weight

    m = len(items)
    half = m // 2

    def enumerate_subsets(sub: Sequence[Tuple[Any, int, int]]):
        # (weight, value, mask)
        combos: List[Tuple[int, int, int]] = [(0, 0, 0)]
        for bit, (_tag, w, v) in enumerate(sub):
            old = combos
            combos = old + [
                (tw + w, tv + v, mask | (1 << bit)) for tw, tv, mask in old
            ]
        return combos

    left = enumerate_subsets(items[:half])
    right = enumerate_subsets(items[half:])
    # Lightest-weight first; at equal weight highest value first.
    right.sort(key=lambda t: (t[0], -t[1]))

    weights: List[int] = []
    prefix_value: List[int] = []
    prefix_mask: List[int] = []
    prefix_weight: List[int] = []
    best_v = -1
    best_mask = 0
    best_w = 0
    for w, v, mask in right:
        if v > best_v:  # strict: keep the earlier (lighter) mask on value ties
            best_v, best_mask, best_w = v, mask, w
        weights.append(w)
        prefix_value.append(best_v)
        prefix_mask.append(best_mask)
        prefix_weight.append(best_w)

    import bisect

    best_total_v = -1
    best_total_w = 0
    best_left_mask = 0
    best_right_mask = 0

    for wl, vl, lm in left:
        room = capacity - wl
        if room < 0:
            continue
        j = bisect.bisect_right(weights, room) - 1
        if j < 0:
            continue
        vv = vl + prefix_value[j]
        ww = wl + prefix_weight[j]
        if vv > best_total_v or (vv == best_total_v and ww < best_total_w):
            best_total_v = vv
            best_total_w = ww
            best_left_mask = lm
            best_right_mask = prefix_mask[j]

    chosen: List[Any] = []
    for bit in range(half):
        if best_left_mask & (1 << bit):
            chosen.append(items[bit][0])
    for bit in range(m - half):
        if best_right_mask & (1 << bit):
            chosen.append(items[half + bit][0])
    return chosen, best_total_v, best_total_w


# ---------------------------------------------------------------------------
# Metrics + allocation
# ---------------------------------------------------------------------------


def build_chunk_metrics(
    a: np.ndarray,
    b: np.ndarray,
    ranges: Sequence[Dict[str, int]],
    manual: Sequence[int],
    chunk_samples: int = CHUNK_SAMPLES,
) -> List[Dict[str, Any]]:
    n = len(a)
    bounds = chunk_bounds(n, chunk_samples)
    intersection = set(chunks_intersecting(ranges, bounds))
    manual_set = set(manual)
    metrics: List[Dict[str, Any]] = []

    for i, (s, e) in enumerate(bounds):
        forced_intersection = i in intersection
        manual_fidelity = i in manual_set
        forced = forced_intersection or manual_fidelity

        wa16 = write_pcm16_wav(a[s:e])
        wb16 = write_pcm16_wav(b[s:e])
        wa8 = write_pcm8_wav(high8_encode(a[s:e]))
        wb8 = write_pcm8_wav(high8_encode(b[s:e]))
        name_a, name_b = chunk_entry_names(i)
        bytes16 = zip_entry_byte_len(name_a, wa16) + zip_entry_byte_len(name_b, wb16)
        bytes8 = zip_entry_byte_len(name_a, wa8) + zip_entry_byte_len(name_b, wb8)
        sse8 = quantization_sse(a[s:e], DEPTH_PCM8) + quantization_sse(
            b[s:e], DEPTH_PCM8
        )

        metrics.append(
            {
                "index": i,
                "start_sample": s,
                "end_sample": e,
                "samples": e - s,
                "intersection_forced": forced_intersection,
                "manual_fidelity": manual_fidelity,
                "forced": forced,
                "bytes16_pair": int(bytes16),
                "bytes8_pair": int(bytes8),
                "extra_bytes_to_16": int(bytes16 - bytes8),
                "sse_if_pcm8": sse8,
            }
        )
    return metrics


def solve_allocation(
    metrics: List[Dict[str, Any]], budget_bytes: int
) -> Dict[str, Any]:
    """Pick an exact optimal depth vector; raise PackageDoesNotFit if floor > cap."""
    n_chunks = len(metrics)
    bits = [DEPTH_PCM8] * n_chunks
    base = zip_constant_bytes()
    items: List[Tuple[int, int, int]] = []

    for m in metrics:
        i = m["index"]
        c8 = m["bytes8_pair"]
        c16 = m["bytes16_pair"]
        if m["forced"]:
            bits[i] = DEPTH_PCM16
            base += c16
        elif c16 <= c8:
            # PCM16 both sounds better and (DEFLATE quirk / edge content) is
            # no larger: it dominates PCM8, take it for free.
            bits[i] = DEPTH_PCM16
            base += c16
        else:
            bits[i] = DEPTH_PCM8
            base += c8
            items.append((i, c16 - c8, m["sse_if_pcm8"]))

    minimum_bytes = base
    if budget_bytes < minimum_bytes:
        raise PackageDoesNotFit(
            {
                "code": "package_does_not_fit",
                "budget_bytes": budget_bytes,
                "minimum_bytes": minimum_bytes,
                "slack_bytes": budget_bytes - minimum_bytes,
                "error": (
                    "放不下：即使把所有非保真块都压成 PCM8，ZIP 至少需要 "
                    f"{minimum_bytes} 字节，而填写的上限只有 {budget_bytes} 字节"
                    f"（还差 {minimum_bytes - budget_bytes} 字节）。"
                    "请调大字节上限，或减少强制保真块。"
                ),
            }
        )

    chosen, _value, used_weight = knapsack_min_error(items, budget_bytes - base)
    for i in chosen:
        bits[i] = DEPTH_PCM16

    total_bytes = base + used_weight
    total_sse = 0
    for m in metrics:
        i = m["index"]
        if bits[i] == DEPTH_PCM8:
            total_sse += m["sse_if_pcm8"]

    return {
        "bit_depths": bits,
        "minimum_bytes": int(minimum_bytes),
        "predicted_zip_bytes": int(total_bytes),
        "total_sse": int(total_sse),
    }


# ---------------------------------------------------------------------------
# Manifest + ZIP rendering / restoration
# ---------------------------------------------------------------------------


def _manifest_bytes(
    *,
    n_samples: int,
    bit_depths: Sequence[int],
    metrics: Sequence[Dict[str, Any]],
    total_sse: int,
) -> bytes:
    chunks = []
    for m, depth in zip(metrics, bit_depths):
        flags = (1 if m["intersection_forced"] else 0) | (
            2 if m["manual_fidelity"] else 0
        )
        chunks.append(
            {
                "i": m["index"],
                "s": m["start_sample"],
                "e": m["end_sample"],
                "n": m["samples"],
                "b": depth,
                "f": flags,
                "sse": 0 if depth == DEPTH_PCM16 else m["sse_if_pcm8"],
            }
        )
    manifest = {
        "package_format": PACKAGE_FORMAT,
        "sample_rate": SAMPLE_RATE,
        "chunk_seconds": CHUNK_SECONDS,
        "chunk_samples": CHUNK_SAMPLES,
        "total_samples": int(n_samples),
        "tracks": ["A", "B"],
        "pcm8_rule": "unsigned u8=(s16>>8)+128; restore s16=(u8-128)*256",
        "total_sse": int(total_sse),
        "chunks": chunks,
    }
    raw = json.dumps(manifest, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(raw) > MANIFEST_SLOT:
        # Should be impossible with the 30 s / 30 chunk upload cap.
        raise PackageError(
            f"manifest 长度 {len(raw)} 超过固定槽 {MANIFEST_SLOT}，无法精确计费"
        )
    return raw + b" " * (MANIFEST_SLOT - len(raw))


def _zip_info(name: str, compressed: bool) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_DATE)
    zi.compress_type = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
    zi.external_attr = (0o644 & 0xFFFF) << 16
    zi.create_system = 3
    return zi


def render_zip(
    a: np.ndarray,
    b: np.ndarray,
    metrics: Sequence[Dict[str, Any]],
    bit_depths: Sequence[int],
    total_sse: int,
) -> bytes:
    manifest = _manifest_bytes(
        n_samples=len(a), bit_depths=bit_depths, metrics=metrics, total_sse=total_sse
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=DEFLATE_LEVEL
    ) as zf:
        zf.writestr(_zip_info(MANIFEST_NAME, compressed=False), manifest)
        for m, depth in zip(metrics, bit_depths):
            i = m["index"]
            s, e = m["start_sample"], m["end_sample"]
            name_a, name_b = chunk_entry_names(i)
            # Pass compresslevel explicitly: a caller-built ZipInfo otherwise
            # keeps its own default _compresslevel instead of the ZipFile's.
            zf.writestr(
                _zip_info(name_a, True),
                encode_chunk_wav(a[s:e], depth),
                compresslevel=DEFLATE_LEVEL,
            )
            zf.writestr(
                _zip_info(name_b, True),
                encode_chunk_wav(b[s:e], depth),
                compresslevel=DEFLATE_LEVEL,
            )
    return buf.getvalue()


def build_package(
    a: np.ndarray,
    b: np.ndarray,
    budget_bytes: Any,
    segments_payload: Any = None,
    manual_chunks: Any = None,
) -> Dict[str, Any]:
    """Full planning + rendering pipeline.

    Returns a JSON-serializable report (without the ZIP bytes) plus the exact
    ZIP blob under ``zip_blob``.  Raises :class:`PackageDoesNotFit` when the
    floor exceeds the budget; no state is mutated by this pure function.
    """
    a = np.asarray(a)
    b = np.asarray(b)
    if a.ndim != 1 or b.ndim != 1 or len(a) != len(b) or len(a) == 0:
        raise PackageError("A/B 必须是等长、非空的单声道采样数组")

    budget = parse_budget(budget_bytes)
    ranges = normalize_fidelity_ranges(segments_payload, len(a))
    bounds = chunk_bounds(len(a))
    manual = normalize_manual_chunks(manual_chunks, len(bounds))
    metrics = build_chunk_metrics(a, b, ranges, manual)
    allocation = solve_allocation(metrics, budget)
    bits = allocation["bit_depths"]

    blob = render_zip(a, b, metrics, bits, allocation["total_sse"])

    # The size model is exact (and tested); keep a defensive repair loop so a
    # zlib/zipfile implementation surprise can never produce an over-cap ZIP.
    while len(blob) > budget:
        candidates = [
            m["index"]
            for m in metrics
            if bits[m["index"]] == DEPTH_PCM16 and not m["forced"]
        ]
        if not candidates:
            floor = len(blob)
            raise PackageDoesNotFit(
                {
                    "code": "package_does_not_fit",
                    "budget_bytes": budget,
                    "minimum_bytes": floor,
                    "error": (
                        f"放不下：最低也要 {floor} 字节，上限只有 {budget} 字节。"
                    ),
                }
            )
        # Safest repair: sacrifice the optional upgrade with the smallest SSE.
        victim = min(candidates, key=lambda i: metrics[i]["sse_if_pcm8"])
        bits[victim] = DEPTH_PCM8
        allocation["total_sse"] += metrics[victim]["sse_if_pcm8"]
        blob = render_zip(a, b, metrics, bits, allocation["total_sse"])

    intersection_chunks = sorted(
        m["index"] for m in metrics if m["intersection_forced"]
    )
    forced_chunks = sorted(m["index"] for m in metrics if m["forced"])

    chunk_rows: List[Dict[str, Any]] = []
    for m, depth in zip(metrics, bits):
        row_sse = 0 if depth == DEPTH_PCM16 else m["sse_if_pcm8"]
        chunk_rows.append(
            {
                "index": m["index"],
                "start_sample": m["start_sample"],
                "end_sample": m["end_sample"],
                "start": m["start_sample"] / SAMPLE_RATE,
                "end": m["end_sample"] / SAMPLE_RATE,
                "duration": (m["end_sample"] - m["start_sample"]) / SAMPLE_RATE,
                "samples": m["samples"],
                "bit_depth": depth,
                "intersection_forced": m["intersection_forced"],
                "manual_fidelity": m["manual_fidelity"],
                "forced": m["forced"],
                "sse_pair": row_sse,
                "sse_if_pcm8": m["sse_if_pcm8"],
                "bytes8_pair": m["bytes8_pair"],
                "bytes16_pair": m["bytes16_pair"],
                "extra_bytes_to_16": m["extra_bytes_to_16"],
            }
        )

    return {
        "budget_bytes": budget,
        "zip_bytes": len(blob),
        "minimum_bytes": allocation["minimum_bytes"],
        "predicted_zip_bytes": allocation["predicted_zip_bytes"],
        "total_sse": allocation["total_sse"],
        "chunk_count": len(bounds),
        "pcm16_chunk_indices": [i for i, d in enumerate(bits) if d == DEPTH_PCM16],
        "pcm8_chunk_indices": [i for i, d in enumerate(bits) if d == DEPTH_PCM8],
        "intersection_chunks": intersection_chunks,
        "manual_chunks": manual,
        "forced_chunks": forced_chunks,
        "segments": ranges,
        "chunks": chunk_rows,
        "zip_blob": blob,
    }


# ---------------------------------------------------------------------------
# Unpack / restore (server-side verification + restored-audio endpoint)
# ---------------------------------------------------------------------------


def _parse_wav_restore(raw: bytes) -> Tuple[np.ndarray, int]:
    """Parse our PCM16/PCM8 chunk WAV into restored int16 samples."""
    try:
        with wave.open(io.BytesIO(raw), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            frames = wf.readframes(nframes)
    except (wave.Error, EOFError, struct.error) as e:
        # Fall back to the strict PCM16 parser for extensible oddities.
        try:
            return parse_pcm16_wav(raw), DEPTH_PCM16
        except WavError:
            raise PackageError(f"ZIP 内 WAV 无法解析：{e}") from e

    if channels != 1 or framerate != SAMPLE_RATE:
        raise PackageError("ZIP 内分块 WAV 必须是 16 kHz 单声道")
    if sampwidth == 2:
        arr = np.frombuffer(frames, dtype="<i2")
        return np.ascontiguousarray(arr, dtype=np.int16), DEPTH_PCM16
    if sampwidth == 1:
        u8 = np.frombuffer(frames, dtype=np.uint8)
        return high8_decode(u8), DEPTH_PCM8
    raise PackageError(f"ZIP 内分块 WAV 位宽不受支持：{sampwidth} 字节")


def restore_zip(blob: bytes) -> Dict[str, Any]:
    """Unzip a generated package and restore full-length PCM16 A/B tracks.

    Returns ``{"a": Int16Array, "b": ..., "manifest": dict, "chunks": [...]}``.
    """
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 22:
        raise PackageError("ZIP 包无效或已损坏")
    try:
        zf = zipfile.ZipFile(io.BytesIO(bytes(blob)))
    except zipfile.BadZipFile as e:
        raise PackageError(f"不是合法 ZIP：{e}") from e

    with zf:
        try:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError) as e:
            raise PackageError(f"ZIP 缺少合法 {MANIFEST_NAME}：{e}") from e

        chunk_meta = manifest.get("chunks") or []
        a_parts: List[np.ndarray] = []
        b_parts: List[np.ndarray] = []
        chunk_info: List[Dict[str, Any]] = []
        for meta in sorted(chunk_meta, key=lambda c: c["i"]):
            i = int(meta["i"])
            name_a, name_b = chunk_entry_names(i)
            try:
                raw_a = zf.read(name_a)
                raw_b = zf.read(name_b)
            except KeyError as e:
                raise PackageError(f"ZIP 缺少分块 {e}") from e
            ra, bits_a = _parse_wav_restore(raw_a)
            rb, bits_b = _parse_wav_restore(raw_b)
            if bits_a != bits_b:
                raise PackageError(f"第 {i} 块 A/B 位深不一致，包已损坏")
            if len(ra) != int(meta.get("n", len(ra))) or len(rb) != len(ra):
                raise PackageError(f"第 {i} 块长度与清单不符，包已损坏")
            a_parts.append(ra)
            b_parts.append(rb)
            chunk_info.append({"index": i, "bit_depth": bits_a, "samples": len(ra)})

    if not a_parts:
        raise PackageError("ZIP 清单中没有任何分块")
    a_out = np.concatenate(a_parts).astype(np.int16, copy=False)
    b_out = np.concatenate(b_parts).astype(np.int16, copy=False)
    return {"a": a_out, "b": b_out, "manifest": manifest, "chunks": chunk_info}


def zip_sse_against(blob: bytes, a: np.ndarray, b: np.ndarray) -> int:
    """Total restoration SSE of a package versus the original tracks."""
    restored = restore_zip(blob)
    ra, rb = restored["a"], restored["b"]
    if len(ra) != len(a) or len(rb) != len(b):
        raise PackageError("还原长度与原音不一致")
    da = a.astype(np.int32) - ra.astype(np.int32)
    db = b.astype(np.int32) - rb.astype(np.int32)
    return int((da * da).sum(dtype=np.int64) + (db * db).sum(dtype=np.int64))
