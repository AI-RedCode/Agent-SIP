#!/usr/bin/env python3
"""Generate deterministic ambient loops as a fallback for Agent-SIP.

Real recordings are preferred: the shipped office.wav, typing.wav, cafe.wav,
and street.wav files are royalty-free recordings prepared for seamless use.
"""

import math
import random
import struct
import wave
from pathlib import Path

RATE = 8000
DURATION = 20
CROSSFADE_SAMPLES = RATE // 10  # 100 ms
OUTPUT_DIR = Path(__file__).parent.parent / "assets" / "ambient"
SEEDS = {
    "office.wav": 20260806,
    "callcenter.wav": 20260807,
    "cafe.wav": 20260808,
    "quiet.wav": 20260809,
}


def _soft_noise(rng: random.Random, state: float, amount: float) -> tuple[float, float]:
    """Return gently low-pass-filtered noise and its new filter state."""
    state = state * 0.985 + rng.gauss(0, amount) * 0.015
    return state, state


def _render(kind: str, rng: random.Random, sample_count: int) -> list[float]:
    samples: list[float] = []
    texture = 0.0
    impulse = 0.0
    ring = 0.0
    ring_phase = 0

    for index in range(sample_count):
        t = index / RATE
        texture, noise = _soft_noise(rng, texture, 500)

        if kind == "office":
            value = rng.gauss(0, 85) + noise * 0.35
            value += 75 * math.sin(2 * math.pi * 60 * t)
            value += 45 * math.sin(2 * math.pi * 120 * t)
            if rng.random() < 0.0015:
                impulse += rng.uniform(280, 620)
            impulse *= 0.90
            value += impulse * (1 if index % 2 else -1)

        elif kind == "callcenter":
            # Several slowly changing, speech-like bands form indistinct far-off chatter.
            value = rng.gauss(0, 105) + noise * 0.55
            value += 85 * math.sin(2 * math.pi * (178 + 17 * math.sin(t * 0.41)) * t)
            value += 65 * math.sin(2 * math.pi * (263 + 23 * math.sin(t * 0.29)) * t)
            if ring_phase <= 0 and rng.random() < 0.000018:
                ring_phase = int(rng.uniform(0.45, 0.8) * RATE)
            if ring_phase > 0:
                envelope = min(1.0, ring_phase / (RATE * 0.08), (ring_phase % (RATE * 0.22)) / (RATE * 0.04))
                ring = 190 * envelope * (
                    math.sin(2 * math.pi * 440 * t) + 0.55 * math.sin(2 * math.pi * 480 * t)
                )
                ring_phase -= 1
            else:
                ring = 0.0
            value += ring

        elif kind == "cafe":
            value = rng.gauss(0, 90) + noise * 0.6
            value += 65 * math.sin(2 * math.pi * (145 + 12 * math.sin(t * 0.37)) * t)
            value += 50 * math.sin(2 * math.pi * (218 + 19 * math.sin(t * 0.23)) * t)
            if rng.random() < 0.00012:
                impulse += rng.uniform(350, 750)
            impulse *= 0.965
            value += impulse * math.sin(2 * math.pi * 1350 * t)

        else:  # quiet
            value = rng.gauss(0, 22) + noise * 0.10
            value += 32 * math.sin(2 * math.pi * 60 * t)
            value += 16 * math.sin(2 * math.pi * 120 * t)

        samples.append(value)
    return samples


def _seamless_loop(samples: list[float], frame_count: int) -> list[int]:
    """Crossfade an extra tail into the head, preserving exactly frame_count frames."""
    head = samples[:CROSSFADE_SAMPLES]
    tail = samples[frame_count:frame_count + CROSSFADE_SAMPLES]
    overlap = []
    for index, (head_sample, tail_sample) in enumerate(zip(head, tail)):
        # Equal-power fades keep the background level steady through the seam.
        phase = (index + 1) / (CROSSFADE_SAMPLES + 1)
        fade_in = math.sin(phase * math.pi / 2)
        fade_out = math.cos(phase * math.pi / 2)
        overlap.append(tail_sample * fade_out + head_sample * fade_in)
    loop = overlap + samples[CROSSFADE_SAMPLES:frame_count]
    return [max(-32768, min(32767, round(value))) for value in loop]


def generate() -> None:
    frame_count = RATE * DURATION
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, seed in SEEDS.items():
        raw = _render(filename.removesuffix(".wav"), random.Random(seed), frame_count + CROSSFADE_SAMPLES)
        samples = _seamless_loop(raw, frame_count)
        with wave.open(str(OUTPUT_DIR / filename), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(RATE)
            target.writeframes(struct.pack(f"<{len(samples)}h", *samples))


if __name__ == "__main__":
    generate()
