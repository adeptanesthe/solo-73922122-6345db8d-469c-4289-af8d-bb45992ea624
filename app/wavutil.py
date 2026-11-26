"""WAV / PCM16 reading, writing and lightweight validation.

The application only accepts exactly:
  * RIFF/WAVE containers
  * PCM (or WAVE_FORMAT_EXTENSIBLE carrying PCM)
  * 16-bit signed little-endian samples
  * mono
  * 16 kHz

A small hand-written RIFF parser is used instead of :mod:`wave` so that
WAVE_FORMAT_EXTENSIBLE files (which ``wave`` rejects on some versions) and
files with extra chunks are still handled, while float / 24-bit / stereo
files get precise error messages.
"""

from __future__ import annotations

import io
import struct
import wave
from typing import Tuple

import numpy as np

SAMPLE_RATE = 16_000
MAX_SECONDS = 30.0
MAX_SAMPLES = int(SAMPLE_RATE * MAX_SECONDS)  # 480_000

PCM16_MIN = -32_768
PCM16_MAX = 32_767
# Symmetric full scale used for dBFS / silence estimates.
FULL_SCALE = 32_768.0

# RMS below ~ -75 dBFS is treated as silence.  Exact digital silence (the
# common case in generated test files) is of course rejected as well.
SILENCE_RMS_THRESHOLD = 10 ** (-75.0 / 20.0) * FULL_SCALE  # ~= 5.83 LSB


class WavError(ValueError):
    """Raised when an uploaded file is not an accepted PCM16 WAV."""


def parse_pcm16_wav(raw: bytes) -> np.ndarray:
    """Parse *raw* WAV bytes into a 1-D ``np.int16`` array.

    Raises :class:`WavError` with a Chinese, user-facing message on any
    format violation.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise WavError("上传内容不是有效的文件字节")
    data = bytes(raw)
    if len(data) < 12:
        raise WavError("文件太小，不是完整的 WAV 文件")
    if data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WavError("文件头不是 RIFF/WAVE，请确认导出为标准 WAV")

    pos = 12
    fmt_body: bytes | None = None
    pcm = bytearray()
    chunks_seen: set[str] = set()

    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (chunk_size,) = struct.unpack_from("<I", data, pos + 4)
        body_start = pos + 8
        body_end = body_start + chunk_size
        if body_end > len(data):
            # Be lenient only when the final data chunk is truncated by a
            # byte or two; anything else is a malformed upload.
            if chunk_id == b"data":
                body_end = len(data)
                chunk_size = body_end - body_start
            else:
                raise WavError("WAV 块长度超出文件末尾，文件可能已损坏")
        try:
            cid = chunk_id.decode("ascii")
        except UnicodeDecodeError:
            cid = ""
        chunks_seen.add(cid)

        body = data[body_start:body_end]
        if chunk_id == b"fmt ":
            fmt_body = body
        elif chunk_id == b"data":
            pcm.extend(body)
        # Other chunks (LIST, fact, bext, ...) are ignored.

        pos = body_start + chunk_size + (chunk_size & 1)

    if fmt_body is None:
        raise WavError("WAV 缺少 fmt 格式块")
    if len(fmt_body) < 16:
        raise WavError("fmt 格式块长度不足，WAV 文件可能已损坏")

    audio_format, channels, sample_rate, _byte_rate, _block_align, bits = (
        struct.unpack_from("<HHIIHH", fmt_body, 0)
    )

    # WAVE_FORMAT_EXTENSIBLE: real format tag lives in the GUID prefix.
    if audio_format == 0xFFFE:
        if len(fmt_body) < 40:
            raise WavError("EXTENSIBLE 格式块长度不足，无法识别实际编码")
        sub_format_tag = struct.unpack_from("<H", fmt_body, 24)[0]
        if sub_format_tag != 1:
            raise WavError(
                f"仅支持 PCM16 编码，该 EXTENSIBLE WAV 的子格式标签为 "
                f"{sub_format_tag}（非 PCM）"
            )
        audio_format = 1

    if audio_format != 1:
        names = {3: "IEEE float", 6: "A-law", 7: "μ-law"}
        label = names.get(audio_format, f"格式标签 {audio_format}")
        raise WavError(f"仅支持 PCM16 整数编码，检测到 {label}")
    if bits != 16:
        raise WavError(f"仅支持 16 bit 采样，检测到 {bits} bit")
    if channels != 1:
        raise WavError(f"仅支持单声道（mono），检测到 {channels} 声道")
    if sample_rate != SAMPLE_RATE:
        raise WavError(
            f"采样率必须为 {SAMPLE_RATE} Hz（16 kHz），检测到 {sample_rate} Hz"
        )
    if len(pcm) < 2:
        raise WavError("WAV 中没有音频采样（data 块为空）")
    if len(pcm) % 2 == 1:
        pcm = pcm[:-1]

    samples = np.frombuffer(bytes(pcm), dtype="<i2")
    if samples.ndim != 1:
        samples = samples.reshape(-1)
    return np.ascontiguousarray(samples, dtype=np.int16)


def validate_pair(raw_a: bytes, raw_b: bytes) -> Tuple[np.ndarray, np.ndarray]:
    """Parse and cross-validate the aligned A/B upload pair."""
    a = parse_pcm16_wav(raw_a)
    b = parse_pcm16_wav(raw_b)
    if len(a) != len(b):
        raise WavError(
            "两份 WAV 必须等长且时间对齐："
            f"A 为 {len(a)} 个采样（{len(a) / SAMPLE_RATE:.3f}s），"
            f"B 为 {len(b)} 个采样（{len(b) / SAMPLE_RATE:.3f}s）"
        )
    if len(a) == 0:
        raise WavError("音频为空")
    if len(a) > MAX_SAMPLES:
        raise WavError(
            f"音频至多 {MAX_SECONDS:.0f} 秒（{MAX_SAMPLES} 个采样），"
            f"当前为 {len(a) / SAMPLE_RATE:.2f} 秒（{len(a)} 个采样）"
        )
    return a, b


def rms_dbfs(rms_value: float) -> float:
    if rms_value <= 0:
        return float("-inf")
    return 20.0 * np.log10(rms_value / FULL_SCALE)


def is_silent_rms(rms_value: float) -> bool:
    return rms_value < SILENCE_RMS_THRESHOLD


def write_pcm16_wav(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Encode a 1-D int16 array as a standard mono PCM16 WAV byte string."""
    arr = np.asarray(samples)
    if arr.dtype != np.int16:
        arr = np.clip(np.rint(arr), PCM16_MIN, PCM16_MAX).astype("<i2")
    else:
        arr = arr.astype("<i2", copy=False)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(arr.tobytes())
    return buf.getvalue()


def gain_limited_samples(b: np.ndarray, gain: float) -> np.ndarray:
    """Return quantized ``gain * b`` as int16.

    Calibration guarantees this never clips; the clip is only a defensive
    guard for direct callers.
    """
    y = b.astype(np.float64) * float(gain)
    yq = np.rint(y)
    if np.any(yq > PCM16_MAX) or np.any(yq < PCM16_MIN):
        yq = np.clip(yq, PCM16_MIN, PCM16_MAX)
    return yq.astype("<i2")
