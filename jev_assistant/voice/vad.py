"""End-of-speech detection, so the user never has to press anything to stop talking.

Silero VAD (its ONNX model ships in voice/assets, run with onnxruntime at <1 ms per frame);
if onnxruntime is missing, an adaptive energy detector, which is fine in a quiet room but easier
to fool with background noise.
"""

from __future__ import annotations

import importlib.util
import logging
from collections import deque
from pathlib import Path

import numpy as np

from .audio import FRAME
from .base import STT_RATE

log = logging.getLogger("jev.voice")

FRAME_MS = FRAME * 1000 / STT_RATE  # 32 ms


def silero_model_path() -> Path | None:
    """Silero VAD (MIT, snakers4/silero-vad) ships in voice/assets; faster-whisper's copy is a fallback."""
    if not importlib.util.find_spec("onnxruntime"):
        return None
    folders = [Path(__file__).parent / "assets"]
    spec = importlib.util.find_spec("faster_whisper")  # locate without importing it (slow)
    if spec and spec.submodule_search_locations:
        folders.append(Path(spec.submodule_search_locations[0], "assets"))
    for folder in folders:
        models = sorted(folder.glob("silero_vad*.onnx"))
        if models:
            return models[-1]
    return None


class SileroVAD:
    name = "silero"
    CONTEXT = 64  # samples of the previous frame the model expects in front of each frame

    def __init__(self, path: Path):
        import onnxruntime
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(str(path), sess_options=opts,
                                                    providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.context = np.zeros(self.CONTEXT, np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.context, frame])[None]
        prob, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.context = frame[-self.CONTEXT:]
        return float(prob.ravel()[0])


class EnergyVAD:
    """Loudness relative to a running noise-floor estimate, mapped to a 0..1 'probability'."""

    name = "energy"
    MIN_RMS = 0.004   # about -48 dBFS: quieter than this is never speech

    def reset(self) -> None:
        self.noise: float | None = None

    def __init__(self):
        self.reset()

    def __call__(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(frame ** 2))) + 1e-6
        if self.noise is None:
            self.noise = rms
        prob = float(np.clip((rms / self.noise - 2.5) / 3, 0, 1)) if rms > self.MIN_RMS else 0.0
        if prob < 0.5:  # only learn the floor from non-speech
            self.noise = 0.95 * self.noise + 0.05 * rms
        return prob


def make_vad() -> SileroVAD | EnergyVAD:
    path = silero_model_path()
    if path:
        try:
            return SileroVAD(path)
        except Exception:
            log.exception("Silero VAD failed to load; using the energy detector")
    return EnergyVAD()


class Endpointer:
    """Feeds frames through the VAD and says when the utterance is over.

    push() returns None while listening, "speech" once when speech starts, and then one of
    "end" (trailing silence), "max" (length cap) or "no_speech" (nothing said in time).

    Background-aware: a frame only counts as the user if it sounds like speech AND is clearly
    louder than the background, whose level is measured when listening starts and tracked
    whenever the user isn't talking. Song vocals, a TV or a podcast in the room are speech-like
    to the VAD, but they're quieter than someone talking into the laptop, so they no longer keep
    the utterance "open" after the user stops."""

    START_FRAMES = 3       # ~100 ms of speech before we believe it (ignores clicks)
    ON, OFF = 0.5, 0.35    # VAD hysteresis: harder to start speech than to stay in it
    REL_ON, REL_OFF = 1.6, 1.2  # loudness over the background's loud moments: start (+4 dB) / continue (+1.6 dB)
                                # (tuned on commands mixed with continuous background speech from
                                #  -20 to -6 dB: always stopped 0.2-1.2 s after the command, never early)
    SKIP_FRAMES = 6        # ~190 ms: the listening chime leaks from the speakers into the mic
    CALIBRATION_FRAMES = 8 # then ~250 ms of background before the user normally starts talking
    WINDOW_FRAMES = 47     # ~1.5 s of recent background kept for the level estimate
    PERCENTILE = 90        # music and speech have gaps: compare against their loud parts, not the gaps
    MIN_RMS = 0.002        # below about -54 dBFS nothing counts as speech, however quiet the room

    def __init__(self, vad, silence_ms: int, max_seconds: float, no_speech_seconds: float):
        self.vad = vad
        self.vad.reset()
        self.silence_frames = max(1, round(silence_ms / FRAME_MS))
        self.max_frames = round(max_seconds * 1000 / FRAME_MS)
        self.no_speech_frames = round(no_speech_seconds * 1000 / FRAME_MS)
        self.frames = 0
        self.run = 0          # consecutive speech frames (before start) / silent frames (after)
        self.speaking = False
        self.speech_start = 0  # frame index where speech began
        self.user_like = False  # was the last frame loud enough over the background to be the user?
        self._background: deque[float] = deque(maxlen=self.WINDOW_FRAMES)  # RMS of non-user frames

    @property
    def background(self) -> float:
        """How loud everything that isn't the user gets (its 90th-percentile frame level)."""
        return float(np.percentile(self._background, self.PERCENTILE)) if self._background else 0.0

    def push(self, pcm: bytes) -> str | None:
        frame = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        if len(frame) != FRAME:
            frame = np.pad(frame[:FRAME], (0, max(0, FRAME - len(frame))))
        prob = self.vad(frame)
        rms = float(np.sqrt(np.mean(frame ** 2)))
        self.frames += 1
        if self.frames <= self.SKIP_FRAMES + self.CALIBRATION_FRAMES:
            self.user_like = False
            if self.frames > self.SKIP_FRAMES:
                self._background.append(rms)
            return None
        floor = max(self.background, self.MIN_RMS / self.REL_OFF)
        self.user_like = rms >= floor * self.REL_OFF
        if not self.speaking:
            is_user = prob >= self.ON and rms >= floor * self.REL_ON and rms >= self.MIN_RMS
            self.run = self.run + 1 if is_user else 0
            if not is_user:
                self._background.append(rms)
            if self.run >= self.START_FRAMES:
                self.speaking, self.run = True, 0
                self.speech_start = self.frames - self.START_FRAMES
                return "speech"
            return "no_speech" if self.frames >= self.no_speech_frames else None
        still_user = prob >= self.OFF and rms >= floor * self.REL_OFF
        self.run = 0 if still_user else self.run + 1
        if not still_user:
            self._background.append(rms)
        if self.run >= self.silence_frames:
            return "end"
        return "max" if self.frames >= self.max_frames else None


class NoiseGate:
    """Turns down audio that isn't louder than the background before it reaches the recogniser,
    so a TV or song vocals in the gaps between your words don't get transcribed as part of the
    command. Keyed to the Endpointer's background estimate; holds open briefly after your voice
    so word endings aren't clipped. Attenuates rather than mutes, which recognisers handle better
    than hard silence."""

    HOLD_FRAMES = 4       # ~130 ms kept open after the last loud-enough frame
    FLOOR_GAIN = 0.15     # about -16 dB for everything else

    def __init__(self, endpoint: Endpointer):
        self.endpoint = endpoint
        self.hold = 0

    def __call__(self, pcm: bytes) -> bytes:
        """Call right after endpoint.push(pcm)."""
        if self.endpoint.user_like:
            self.hold = self.HOLD_FRAMES
            return pcm
        if self.hold > 0:
            self.hold -= 1
            return pcm
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * self.FLOOR_GAIN
        return x.astype(np.int16).tobytes()
