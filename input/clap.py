"""Clap-to-wake — DSP-based onset detector + pattern matcher.

Alternative wake trigger to "Hey Sage". Useful when your hands are dirty,
you're on a phone call, or you just want a non-verbal way to summon ODIN.

Why DSP not CNN: claps are acoustically distinctive — sharp transient
(<30 ms rise time), broadband spectrum (energy spread across the band,
unlike speech which clusters in formants), and high peak amplitude. A
small set of thresholds catches them with low false-positive rate. The
huwprosser CNN approach exists but requires bundling a model file and
fighting room-specific tuning issues we don't have to solve.

Pattern matching: a single loud bang from a door, a slammed cabinet, or
a desk thump trivially passes the clap shape test. So we require a
DOUBLE-clap (2 claps within ~600 ms) by default. The pattern is
configurable: single | double | triple, with `cooldown_sec` enforcing
a quiet window after a match so the detector doesn't double-fire.

Integration: HEIMDALL feeds each audio hop (500 ms, 16 kHz, float32) to
`ClapDetector.add_hop(hop)`. Returns True the instant the configured
pattern is satisfied. False otherwise. State is internal.
"""

from collections import deque
from typing import Optional

import numpy as np


# DSP defaults — tuned against typical laptop-mic room ambient. Override
# via config keys if your environment is noisier or quieter.
DEFAULT_PEAK_THRESHOLD       = 0.18    # absolute peak amplitude floor (0-1)
DEFAULT_RMS_RATIO            = 7.0     # peak / baseline_rms ratio for transient
DEFAULT_HF_RATIO             = 0.35    # min fraction of energy above 4 kHz
DEFAULT_MIN_INTERVAL_SEC     = 0.10    # minimum gap between consecutive claps
DEFAULT_MAX_INTERVAL_SEC     = 0.80    # max gap so claps "belong to the same pattern"
DEFAULT_COOLDOWN_SEC         = 1.20    # quiet window after a match before we can fire again
DEFAULT_BASELINE_HOPS        = 6       # rolling RMS baseline length (3 s at 0.5 s hops)


class ClapDetector:
    def __init__(self,
                 sample_rate: int = 16000,
                 pattern: str = "double",
                 peak_threshold: float = DEFAULT_PEAK_THRESHOLD,
                 rms_ratio: float = DEFAULT_RMS_RATIO,
                 hf_ratio: float = DEFAULT_HF_RATIO,
                 min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
                 max_interval_sec: float = DEFAULT_MAX_INTERVAL_SEC,
                 cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
                 baseline_hops: int = DEFAULT_BASELINE_HOPS):
        self.sample_rate = sample_rate
        self.required = {"single": 1, "double": 2, "triple": 3}.get(pattern, 2)
        self.peak_threshold = peak_threshold
        self.rms_ratio = rms_ratio
        self.hf_ratio = hf_ratio
        self.min_interval_sec = min_interval_sec
        self.max_interval_sec = max_interval_sec
        self.cooldown_sec = cooldown_sec

        # Rolling RMS history → baseline for transient detection.
        self._rms_history: deque = deque(maxlen=baseline_hops)
        # Recent clap timestamps measured in AUDIO TIME (seconds of audio
        # processed since startup), NOT wall time. This makes pattern
        # detection robust to processing latency: even if HEIMDALL stalls
        # for 2 s on a Whisper call between hops, the gap between two
        # claps is still the gap between their AUDIO positions.
        self._clap_times: deque = deque(maxlen=4)
        # Audio clock — incremented by hop_duration on every add_hop().
        self._audio_clock: float = 0.0
        # Audio-time of last pattern match, for cooldown.
        self._last_fire: float = -1e9

    # ── Public API ────────────────────────────────────────────────
    def add_hop(self, hop: np.ndarray) -> bool:
        """Feed one audio chunk. Returns True iff the configured pattern
        just completed. Caller should treat True like a wake-word match.

        Internally advances an audio clock by hop_duration so pattern
        matching is robust to processing latency / wall-clock skew."""
        if hop is None or len(hop) == 0:
            return False
        # Advance audio clock FIRST so the clap timestamp reflects the END
        # of the chunk it's in (close enough for pattern matching).
        self._audio_clock += len(hop) / float(self.sample_rate)
        now = self._audio_clock

        if now - self._last_fire < self.cooldown_sec:
            return False

        rms = float(np.sqrt(np.mean(hop.astype(np.float32) ** 2)))
        peak = float(np.max(np.abs(hop)))

        # Baseline: median of recent RMS so a single loud hop doesn't
        # poison the comparison.
        if len(self._rms_history) >= 3:
            baseline = float(np.median(self._rms_history)) or 1e-6
        else:
            baseline = 1e-6
        self._rms_history.append(rms)

        # Transient gate: must be (a) loud in absolute terms AND (b)
        # loud relative to baseline. Either alone catches false positives
        # (background hiss has high relative deltas, sustained music has
        # high absolute energy).
        if peak < self.peak_threshold:
            return False
        if peak < baseline * self.rms_ratio:
            return False

        # Sharpness gate: a real clap is ~25 ms of loud surrounded by
        # silence within the ~500 ms hop. Sustained loud audio (white
        # noise, fan, music) has loud samples spread across the whole hop.
        # We check the fraction of samples below 10 % of the peak — claps
        # have >70 % quiet, sustained loud audio has <30 %.
        quiet_frac = float(np.mean(np.abs(hop) < (peak * 0.10)))
        if quiet_frac < 0.55:
            return False

        # Spectral gate: claps are flat / broadband, speech is formant-clustered.
        # We check the fraction of energy above 4 kHz — claps usually have
        # >35 % up there, normal speech has <20 %.
        if not self._is_broadband(hop):
            return False

        # Avoid double-counting one clap that straddles two hops — require
        # the configured minimum gap between consecutive transients.
        if self._clap_times and (now - self._clap_times[-1]) < self.min_interval_sec:
            return False

        self._clap_times.append(now)

        # Prune any old transients that fell outside the pattern window.
        cutoff = now - self.max_interval_sec * (self.required + 1)
        while self._clap_times and self._clap_times[0] < cutoff:
            self._clap_times.popleft()

        # Pattern match: do we have `required` claps with neighbours all
        # within max_interval_sec of each other?
        if len(self._clap_times) >= self.required:
            recent = list(self._clap_times)[-self.required:]
            if all(recent[i+1] - recent[i] <= self.max_interval_sec
                   for i in range(len(recent) - 1)):
                self._last_fire = now
                self._clap_times.clear()
                return True
        return False

    # ── DSP helpers ──────────────────────────────────────────────
    def _is_broadband(self, hop: np.ndarray) -> bool:
        """Claps spread energy across the band; voice clusters in formants.
        Return True if ≥ hf_ratio of the spectral energy sits above 4 kHz."""
        n = len(hop)
        if n < 256:
            return True   # too short to FFT meaningfully; let it through
        # Real FFT, magnitude squared = power spectrum.
        spec = np.abs(np.fft.rfft(hop * np.hanning(n))) ** 2
        freqs = np.fft.rfftfreq(n, d=1.0 / self.sample_rate)
        total = float(spec.sum())
        if total <= 0:
            return False
        hf = float(spec[freqs >= 4000].sum())
        return (hf / total) >= self.hf_ratio
