"""
core/baseline.py — Shared adaptive baseline helpers.

Both detectors need the same idea: "what is normal for THIS machine, for THIS
game, right now". Without it a fixed threshold either calls a 240 Hz player's
very visible 30 ms hitch healthy, or calls every frame of a 30 fps game a
stutter.

DetectionEngine already learned a baseline, but it did so by appending every
calm sample to a plain list and running statistics.mean over the tail. That is
correct yet grows without bound (one float per second, forever) and cannot be
reused per frame, where samples arrive 60-240x faster. WelfordBaseline computes
the same mean/variance in O(1) space, with a decay step so an old scene stops
dominating the estimate once the game moves on.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

# After this many observations the accumulator weight is halved. Frame data
# arrives fast enough that without decay the first minute of a menu screen
# would anchor the baseline for the rest of the session.
DECAY_AFTER_SAMPLES = 4000
DECAY_FACTOR = 0.5


class WelfordBaseline:
    """Streaming mean/standard deviation with bounded influence of old data."""

    __slots__ = ("_count", "_mean", "_m2", "_total_seen", "_min_samples", "_decay_after")

    def __init__(self, min_samples: int = 120, decay_after: int = DECAY_AFTER_SAMPLES):
        self._min_samples = max(2, min_samples)
        self._decay_after = max(self._min_samples * 2, decay_after)
        self.reset()

    def reset(self):
        self._count = 0.0
        self._mean = 0.0
        self._m2 = 0.0
        self._total_seen = 0

    def add(self, value: float):
        self._total_seen += 1
        self._count += 1.0
        delta = value - self._mean
        self._mean += delta / self._count
        self._m2 += delta * (value - self._mean)
        if self._count >= self._decay_after:
            # Keep mean and variance, shrink their weight, so the next scene can
            # move the baseline instead of being averaged into irrelevance.
            self._count *= DECAY_FACTOR
            self._m2 *= DECAY_FACTOR

    @property
    def sample_count(self) -> int:
        return self._total_seen

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def std(self) -> float:
        if self._count < 2.0:
            return 0.0
        variance = self._m2 / (self._count - 1.0)
        return math.sqrt(variance) if variance > 0.0 else 0.0

    @property
    def is_ready(self) -> bool:
        return self._total_seen >= self._min_samples

    def threshold(self, sigmas: float, floor: float, minimum_std: float = 0.0) -> float:
        """
        mean + sigmas*std, but never below `floor`.

        The floor matters: on a perfectly steady 240 Hz capture the standard
        deviation approaches zero, and a pure mean+3σ threshold would then flag
        ordinary frame-to-frame jitter as a stutter. The floor keeps the
        detector honest about what a human can actually perceive.
        """
        if not self.is_ready:
            return floor
        std = max(self.std, minimum_std)
        return max(floor, self._mean + sigmas * std)

    def ratio_to_mean(self, value: float) -> float:
        """How many times the learned normal this value is (1.0 == normal)."""
        if self._mean <= 0.0:
            return 0.0
        return value / self._mean
