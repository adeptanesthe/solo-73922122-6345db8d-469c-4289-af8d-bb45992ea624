#!/usr/bin/env python3
"""Generate the bundled audible sample pair: samples/sample_A.wav / sample_B.wav.

The pair emulates two "denoising styles" of the same dialogue-like signal:
identical phrase timing / pitch, different spectral balance and residual
noise.  B is made slightly louder overall with small per-phrase level
differences, so the calibration computes a real (but near-unity) gain.

Run from the repository root:

    python samples/make_samples.py

The script also self-checks calibration on the phrase intervals and prints
the resulting gain / ratios.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.calibration import calibrate  # noqa: E402
from app.wavutil import write_pcm16_wav  # noqa: E402

SR = 16_000
DURATION = 15.0
N = int(DURATION * SR)

# (start, end, base fundamental frequency) of dialogue-like phrases.
PHRASES = [
    (0.55, 3.30, 132.0),
    (4.05, 6.95, 118.0),
    (7.85, 10.75, 146.0),
    (11.55, 14.35, 110.0),
]


def _one_pole_lowpass(x: np.ndarray, alpha: float) -> np.ndarray:
    y = np.empty_like(x)
    prev = 0.0
    for i in range(x.shape[0]):
        prev = prev + alpha * (x[i] - prev)
        y[i] = prev
    return y


def _high_emphasis(x: np.ndarray, amount: float) -> np.ndarray:
    """Simple pre-emphasis-ish brightening."""
    d = np.empty_like(x)
    d[0] = x[0]
    d[1:] = x[1:] - 0.92 * x[:-1]
    return x + amount * d


def build_phrase(rng: np.random.Generator, dur: float, f0_base: float,
                 bright: bool) -> np.ndarray:
    n = int(dur * SR)
    tt = np.arange(n) / SR

    # Gentle declination + slow vibrato keep the tone from sounding static.
    contour = f0_base * (1.0 - 0.10 * tt / dur)
    vibrato = 1.0 + 0.018 * np.sin(2 * np.pi * 5.2 * tt + rng.random())
    f0 = contour * vibrato
    phase = 2.0 * np.pi * np.cumsum(f0) / SR

    if bright:
        harms = [(1, 1.00), (2, 0.62), (3, 0.42), (4, 0.26),
                 (5, 0.16), (6, 0.09), (7, 0.05)]
    else:
        harms = [(1, 1.00), (2, 0.42), (3, 0.20), (4, 0.08), (5, 0.03)]

    voiced = np.zeros(n)
    for k, amp in harms:
        voiced += amp * np.sin(k * phase + 0.3 * k)

    # Syllable-like amplitude pulses.
    env = np.zeros(n)
    pos = 0.08
    while pos < dur - 0.12:
        syl_dur = rng.uniform(0.13, 0.24)
        t0 = pos
        a = int(0.025 * SR)
        r = int(0.06 * SR)
        i0 = int(t0 * SR)
        i1 = min(n, int((t0 + syl_dur) * SR))
        if i1 <= i0:
            break
        length = i1 - i0
        pulse = np.ones(length)
        aa = min(a, length // 2)
        rr = min(r, length // 2)
        if aa > 0:
            pulse[:aa] = np.linspace(0, 1, aa)
        if rr > 0:
            pulse[-rr:] = np.linspace(1, 0, rr)
        # Slight random syllable accent.
        env[i0:i1] += pulse * rng.uniform(0.7, 1.15)
        pos += syl_dur * rng.uniform(0.85, 1.25)

    env = np.clip(env, 0, 1.4)
    # Smooth the envelope a little.
    kernel = np.hanning(int(0.02 * SR))
    kernel /= kernel.sum()
    env = np.convolve(env, kernel, mode="same")

    # Consonant-ish filtered noise bursts following the same envelope.
    noise = rng.standard_normal(n)
    consonants = np.diff(noise, prepend=noise[0])
    consonants /= max(1e-9, np.max(np.abs(consonants)))
    breath = 0.10 * consonants * np.clip(env, 0, 1)

    phrase = voiced * env + breath

    # Fade phrase edges cleanly.
    edge = int(0.03 * SR)
    if n > 2 * edge:
        phrase[:edge] *= np.linspace(0, 1, edge)
        phrase[-edge:] *= np.linspace(1, 0, edge)
    return phrase


def build_version(kind: str) -> np.ndarray:
    rng = np.random.default_rng(20261125 if kind == "A" else 98765432)
    x = np.zeros(N)

    phrase_signals = []
    for (s, e, f0) in PHRASES:
        sig = build_phrase(rng, e - s, f0, bright=(kind == "B"))
        phrase_signals.append((int(s * SR), sig))

    for start, sig in phrase_signals:
        end = min(N, start + sig.shape[0])
        x[start:end] += sig[: end - start]

    if kind == "A":
        # Warm / slightly muffled denoise style.
        x = _one_pole_lowpass(x, 0.42)
        # Very low residual noise floor.
        x += rng.standard_normal(N) * 0.0012
    else:
        # Brighter, crisper denoise style with a bit more hiss.
        x = _high_emphasis(x, 0.35)
        x = _one_pole_lowpass(x, 0.85)
        x += rng.standard_normal(N) * 0.0030

    # Per-phrase RMS normalization -> same target for A; B gets a shared
    # +7% lift plus small deterministic per-phrase differences.  No master
    # scaling here: main() applies one JOINT scale to A and B so the designed
    # cross-version level relation is preserved.
    out = np.zeros_like(x)
    target_a = 0.11
    b_variation = [1.00, 0.982, 1.021, 0.99]
    for i, (s_t, e_t, _f0) in enumerate(PHRASES):
        s, e = int(s_t * SR), int(e_t * SR)
        seg = x[s:e]
        rms = float(np.sqrt(np.mean(seg * seg))) or 1.0
        if kind == "A":
            scale = target_a / rms
        else:
            target_b = target_a * 1.07 * b_variation[i]
            scale = target_b / rms
        out[s:e] = seg * scale
    return out


def main() -> int:
    a_f = build_version("A")
    b_f = build_version("B")

    # Joint peak safety: one common scale keeps RMS ratios untouched and
    # guarantees comfortable PCM16 headroom.
    joint_peak = max(float(np.max(np.abs(a_f))), float(np.max(np.abs(b_f))))
    common = min(1.0, 0.62 / (joint_peak or 1.0))
    a_f *= common
    b_f *= common

    rng = np.random.default_rng(424242)
    a_f += rng.standard_normal(N) / 32768.0 * 0.4
    b_f += rng.standard_normal(N) / 32768.0 * 0.4

    a_i16 = np.clip(np.rint(a_f * 32767.0), -32768, 32767).astype("<i2")
    b_i16 = np.clip(np.rint(b_f * 32767.0), -32768, 32767).astype("<i2")

    pa = HERE / "sample_A.wav"
    pb = HERE / "sample_B.wav"
    pa.write_bytes(write_pcm16_wav(a_i16, SR))
    pb.write_bytes(write_pcm16_wav(b_i16, SR))
    print(f"wrote {pa.relative_to(ROOT)} ({pa.stat().st_size} bytes)")
    print(f"wrote {pb.relative_to(ROOT)} ({pb.stat().st_size} bytes)")

    # Self-check: calibration on the phrase intervals must be feasible.
    segs = [
        {"start_sample": int(s * SR) + 800, "end_sample": int(e * SR) - 800}
        for s, e, _ in PHRASES
    ]
    rep = calibrate(a_i16, b_i16, segs)
    print("feasible:", rep["feasible"], "| code:", rep["reason_code"])
    if rep["feasible"]:
        print("gain:", round(rep["gain"], 6),
              "| interval [", round(rep["rms_gain_lower"], 6),
              ",", round(rep["effective_gain_upper"], 6), "]")
        for row in rep["segments"]:
            print(f"  seg{row['index']}: native B/A={row['ratio_native']:.4f} "
                  f"calibrated={row['ratio_calibrated']:.4f}")
    else:
        print("REASON:", rep["reason"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
