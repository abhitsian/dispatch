"""Audio pipeline: record mic, transcribe with whisper, speak with radio-effect TTS."""
import os
import subprocess
import tempfile
import time
import wave
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd

from paths import RESOURCE_DIR, USER_DATA_DIR

# Voice recordings are user data — must be in a writable location.
# Sounds (FX cache) are also writable because we generate them on first use,
# but if the bundle ships pre-built ones in RESOURCE_DIR/sounds we read those.
VOICE_DIR = USER_DATA_DIR / "voice"
SOUNDS_DIR = USER_DATA_DIR / "sounds"
VOICE_DIR.mkdir(parents=True, exist_ok=True)
SOUNDS_DIR.mkdir(parents=True, exist_ok=True)

SAMPLE_RATE = 16000


class Recorder:
    """Toggle-record mic to a wav file."""

    def __init__(self):
        self._stream = None
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self.recording = False
        self._peak = 0.0  # most recent peak amplitude, 0..1, for UI level meter

    def start(self):
        with self._lock:
            if self.recording:
                return
            self._frames = []
            self.recording = True
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                callback=self._on_audio,
            )
            self._stream.start()

    def _on_audio(self, indata, frames, time_info, status):
        self._frames.append(indata.copy())
        # cheap peak amplitude for the menu-bar level meter
        try:
            self._peak = float(np.abs(indata).max()) / 32768.0
        except Exception:
            pass

    def level(self) -> float:
        """Current peak audio level, 0..1. 0 when not recording."""
        return self._peak if self.recording else 0.0

    def stop(self) -> Path | None:
        with self._lock:
            if not self.recording:
                return None
            self.recording = False
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None
        if not self._frames:
            return None
        audio = np.concatenate(self._frames, axis=0)
        out = VOICE_DIR / f"in_{int(time.time())}.wav"
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return out


WHISPER_MODEL = "small.en"  # ~250 MB, big accuracy bump over base.en
MLX_MODEL = "mlx-community/whisper-small.en-mlx"  # GPU-accelerated on M-series

WHISPER_PROMPT = (
    "Dispatch radio channel. Units: Unit one, Unit two, Unit three, "
    "Unit four, Unit five, Unit six, Unit seven. Commands: status check, "
    "report in, permission granted, permission denied, negative, affirmative, "
    "elevate, all units, talk to all, over."
)

# Keep an in-process mlx-whisper model loaded across transcribes so the first
# call pays the model-load cost once and every subsequent call is sub-second.
_MLX_AVAILABLE: bool | None = None  # tri-state: None = unprobed, then bool


def _mlx_try_transcribe(wav: Path) -> str | None:
    """Use mlx-whisper if installed — much faster on Apple Silicon than the
    openai-whisper CLI. Returns text on success, None to fall back."""
    global _MLX_AVAILABLE
    if _MLX_AVAILABLE is False:
        return None
    # Dynamic import — py2app's static walker recurses forever on mlx, so we
    # hide the dependency behind importlib. Bundled .app falls back to the CLI;
    # source-tree (venv-equipped) runs use the fast path.
    try:
        import importlib
        mlx_whisper = importlib.import_module("mlx_whisper")
        _MLX_AVAILABLE = True
    except Exception:
        _MLX_AVAILABLE = False
        return None
    try:
        result = mlx_whisper.transcribe(
            str(wav),
            path_or_hf_repo=MLX_MODEL,
            language="en",
            temperature=0,
            initial_prompt=WHISPER_PROMPT,
            no_speech_threshold=0.5,
            verbose=False,
        )
        return (result.get("text") or "").strip()
    except Exception:
        # If mlx fails for a specific clip, fall back to the CLI path.
        return None


def _preprocess(in_wav: Path) -> Path:
    """Clean up the raw mic recording before whisper sees it:
        - trim leading silence (so whisper doesn't hallucinate openings)
        - high-pass to drop mic rumble
        - dynamic normalize so quiet speech is audible
    Returns a sibling _clean.wav.
    """
    out = in_wav.with_name(in_wav.stem + "_clean.wav")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(in_wav),
                "-af",
                "silenceremove=start_periods=1:start_silence=0.15:start_threshold=-45dB,"
                "highpass=f=80,lowpass=f=7500,"
                "dynaudnorm=p=0.95:m=12:s=8",
                "-ar", "16000", "-ac", "1",
                str(out),
            ],
            check=True, capture_output=True, timeout=15,
        )
        return out
    except Exception:
        return in_wav  # fall back to the raw recording


def _clean_env_for_external_python() -> dict:
    """py2app exports PYTHONPATH/PYTHONHOME pointing inside the bundle. When we
    then invoke `whisper` (its own Python shebang), it tries to load *our*
    modules from inside the .app and crashes. Strip those vars before spawn.
    Also force UTF-8 so whisper's progress-bar bytes don't crash text decode."""
    env = os.environ.copy()
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE",
              "PYTHONUSERBASE", "PYTHONNOUSERSITE",
              "RESOURCEPATH", "ARGVZERO"):
        env.pop(k, None)
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "en_US.UTF-8"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def transcribe(wav_path: Path) -> str:
    """Run local openai-whisper on the recording. Returns text."""
    if not wav_path.exists():
        return ""
    # Skip ultra-short clips — likely an accidental tap, whisper would
    # hallucinate "Thanks for watching!" or similar.
    try:
        import wave
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            if frames / rate < 0.5:
                return ""
    except Exception:
        pass

    clean = _preprocess(wav_path)

    # Fast path — mlx-whisper runs on Apple Silicon GPU/Neural Engine.
    mlx_text = _mlx_try_transcribe(clean)
    if mlx_text is not None:
        raw = mlx_text
        if raw.lower() in {"thank you.", "thanks for watching.", "you", ""}:
            return ""
        return raw

    # Fallback — openai-whisper CLI (slower but always available).
    out_dir = clean.parent
    try:
        result = subprocess.run(
            [
                "whisper",
                str(clean),
                "--model", WHISPER_MODEL,
                "--language", "en",
                "--output_format", "txt",
                "--output_dir", str(out_dir),
                "--fp16", "False",
                "--verbose", "False",
                "--temperature", "0",
                "--no_speech_threshold", "0.5",
                "--initial_prompt", WHISPER_PROMPT,
            ],
            capture_output=True,
            # text-mode with explicit UTF-8 + replace so whisper's progress-bar
            # bytes (▕ / ▏ etc.) never crash the parent's decode.
            encoding="utf-8", errors="replace",
            timeout=180,
            env=_clean_env_for_external_python(),
        )
    except subprocess.TimeoutExpired:
        return "[transcribe failed: timeout]"
    except Exception as exc:
        return f"[transcribe failed to spawn: {exc}]"
    if result.returncode != 0:
        # Surface the real reason so we can debug the next time.
        err = (result.stderr or "").strip().splitlines()
        tail = " ".join(err[-3:])[-300:] if err else "(no stderr)"
        return f"[transcribe failed exit={result.returncode}: {tail}]"

    txt = out_dir / (clean.stem + ".txt")
    if not txt.exists():
        return ""
    raw = txt.read_text(encoding="utf-8").strip()
    if raw.lower() in {"thank you.", "thanks for watching.", "you", ""}:
        return ""
    return raw


# Per-agent macOS voice — all American, picked for police LMR feel:
# deep / level / flat-affect over British or Australian.
VOICE_MAP = {
    "ALPHA": "Ralph",       # deep US male — sergeant / ops lead
    "BRAVO": "Reed",        # level US male — intel/analyst
    "CHARLIE": "Fred",      # gruff older US male — field operator
    "DELTA": "Samantha",    # clear US female — dispatcher peer
    "DISPATCH": "Kathy",    # alt US female — system / welcome
}

# Faster delivery = more clipped / police-radio feel.
SPEECH_RATE = 215


def _radio_filter(in_wav: Path, out_wav: Path):
    """Apply a police-LMR radio effect: narrow P25-style band, hard compression,
    mid-presence boost, light overdrive, and continuous hiss mixed UNDER the voice."""
    # Heavy filter chain on the voice:
    voice_chain = (
        # P25 / Motorola-style narrow band (~300-2700 Hz)
        "highpass=f=300,lowpass=f=2700,"
        # Squelch-style hard compression
        "acompressor=threshold=-22dB:ratio=9:attack=4:release=80,"
        # Mid-presence boost — that radio "crunch" in the voice
        "equalizer=f=1700:width_type=h:width=700:g=6,"
        "equalizer=f=900:width_type=h:width=500:g=3,"
        # Light overdrive / asymmetric clipping
        "acrusher=level_in=1.2:level_out=1.0:bits=8:mode=log:aa=1,"
        # Tighten dynamics again post-overdrive
        "acompressor=threshold=-14dB:ratio=4:attack=2:release=40,"
        "volume=1.6"
    )
    # Mix in continuous low static UNDER the voice using filter_complex.
    # anoisesrc generates colored noise for the duration of the voice.
    fc = (
        f"[0:a]{voice_chain}[v];"
        "anoisesrc=color=pink:amplitude=0.06:seed=42[n0];"
        "[n0]highpass=f=400,lowpass=f=2500,volume=0.18[n];"
        "[v][n]amix=inputs=2:duration=first:dropout_transition=0[m];"
        # Final master limiter so it sits like a radio transmission
        "[m]acompressor=threshold=-10dB:ratio=8:attack=1:release=20[out]"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(in_wav),
            "-filter_complex", fc,
            "-map", "[out]",
            "-ar", "22050", "-ac", "1",
            str(out_wav),
        ],
        check=True,
    )


MIC_CLICK = SOUNDS_DIR / "mic_click.wav"      # sharp transient at start of TX
ROGER_BEEP = SOUNDS_DIR / "roger_beep.wav"    # Motorola courtesy tone
SQUELCH_TAIL = SOUNDS_DIR / "squelch_tail.wav"  # short hiss after roger beep
ALERT_TONE = SOUNDS_DIR / "alert_tone.wav"    # 2-tone dispatch "attention" chirp


def _build_mic_click(path: Path):
    if path.exists():
        return
    # ~25ms broadband noise burst -> bandpass for 'cone' feel -> sharp fade
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", "anoisesrc=color=white:duration=0.05:amplitude=0.45",
            "-af",
            "highpass=f=900,lowpass=f=3200,"
            "afade=t=in:st=0:d=0.002,"
            "afade=t=out:st=0.018:d=0.03,"
            "volume=0.9",
            "-ac", "1", "-ar", "22050",
            str(path),
        ],
        check=True,
    )


def _build_roger_beep(path: Path):
    """Motorola-style courtesy tone — single short sine at ~1500 Hz, fast attack/decay."""
    if path.exists():
        return
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", "sine=frequency=1500:duration=0.11",
            "-af",
            "afade=t=in:st=0:d=0.005,"
            "afade=t=out:st=0.085:d=0.025,"
            # apply the same band/compression so it sounds like it came over the radio
            "highpass=f=300,lowpass=f=2700,"
            "acompressor=threshold=-18dB:ratio=6:attack=2:release=30,"
            "volume=0.6",
            "-ac", "1", "-ar", "22050",
            str(path),
        ],
        check=True,
    )


def _build_alert_tone(path: Path):
    """Classic dispatch 2-tone attention alert — low tone then high tone,
    each ~250 ms, run through the radio band so it sounds *transmitted*."""
    if path.exists():
        return
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i",
            "sine=frequency=853:duration=0.28[t1];"
            "sine=frequency=1477:duration=0.32[t2];"
            "[t1][t2]concat=n=2:v=0:a=1",
            "-af",
            "afade=t=in:st=0:d=0.01,"
            "afade=t=out:st=0.58:d=0.02,"
            "highpass=f=300,lowpass=f=2700,"
            "acompressor=threshold=-18dB:ratio=6:attack=2:release=20,"
            "volume=0.7",
            "-ac", "1", "-ar", "22050",
            str(path),
        ],
        check=True,
    )


def _build_squelch_tail(path: Path):
    """The brief hiss/breath you hear right after the carrier drops."""
    if path.exists():
        return
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", "anoisesrc=color=pink:duration=0.18:amplitude=0.35",
            "-af",
            "highpass=f=500,lowpass=f=2600,"
            "afade=t=in:st=0:d=0.01,"
            "afade=t=out:st=0.1:d=0.08,"
            "volume=0.55",
            "-ac", "1", "-ar", "22050",
            str(path),
        ],
        check=True,
    )


def _ensure_assets():
    # Rebuild only if a file is missing. (Cached sounds live in USER_DATA_DIR;
    # delete them by hand if you want them regenerated.) The previous mtime-based
    # staleness check tripped inside the py2app bundle where __file__ is in a zip.
    for p, build in (
        (MIC_CLICK, _build_mic_click),
        (ROGER_BEEP, _build_roger_beep),
        (SQUELCH_TAIL, _build_squelch_tail),
        (ALERT_TONE, _build_alert_tone),
    ):
        if not p.exists():
            build(p)


def play_alert():
    """Play the 2-tone dispatch attention chirp. Blocking."""
    _ensure_assets()
    subprocess.run(["afplay", str(ALERT_TONE)], check=False)


# ---------- Channel — single FIFO playback queue ----------
import queue
from dataclasses import dataclass as _dc, field as _field
from typing import Callable, Optional


@_dc
class RadioItem:
    """One thing on the radio channel.

    kind:
      "alert"  — the 2-tone dispatch attention chirp
      "tx"     — a spoken transmission (callsign + text)
    """
    kind: str
    text: str = ""
    callsign: str = "DISPATCH"
    label: str = ""   # short label for the queue display ("UNIT-3: status check")


class RadioChannel:
    """Serial radio channel. One thing speaks at a time."""

    def __init__(self):
        self._q: "queue.Queue[RadioItem]" = queue.Queue()
        self._lock = threading.Lock()
        self._muted = False
        self._current: Optional[RadioItem] = None
        self._on_change: Callable[[], None] = lambda: None
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    # ----- subscribe -----
    def set_on_change(self, fn: Callable[[], None]):
        self._on_change = fn

    # ----- introspection -----
    def is_busy(self) -> bool:
        with self._lock:
            return self._current is not None

    def queue_depth(self) -> int:
        # approximate (qsize is not exact but fine for UI)
        return self._q.qsize()

    def current_label(self) -> str:
        with self._lock:
            return self._current.label if self._current else ""

    def upcoming_labels(self) -> list[str]:
        # peek without draining — copy under the queue's internal lock
        with self._q.mutex:
            return [it.label for it in list(self._q.queue)]

    # ----- mute -----
    def set_muted(self, on: bool):
        self._muted = bool(on)
        self._on_change()

    @property
    def muted(self) -> bool:
        return self._muted

    def interrupt(self):
        """Cut the current transmission immediately AND drain the queue.
        Called when the user grants/denies — channel should go quiet at once."""
        with self._q.mutex:
            self._q.queue.clear()
        # afplay is the only thing actively producing sound (say/ffmpeg only
        # build wav files), so killing it silences the current TX. Only one
        # afplay runs at a time per our serial design.
        try:
            subprocess.run(["killall", "afplay"],
                            capture_output=True, timeout=1)
        except Exception:
            pass
        self._on_change()

    # ----- enqueue API -----
    def enqueue_tx(self, text: str, callsign: str = "DISPATCH", label: str = ""):
        self._q.put(RadioItem(kind="tx", text=text, callsign=callsign,
                              label=label or f"{callsign}: {text[:40]}"))
        self._on_change()

    def enqueue_alert(self, label: str = "alert chirp"):
        self._q.put(RadioItem(kind="alert", label=label))
        self._on_change()

    # ----- worker -----
    def _loop(self):
        while True:
            item = self._q.get()
            with self._lock:
                self._current = item
            try:
                self._on_change()
                if not self._muted:
                    if item.kind == "alert":
                        play_alert()
                    elif item.kind == "tx":
                        speak(item.text, item.callsign)
            except Exception:
                # never crash the playback thread; log to stderr via print
                import traceback; traceback.print_exc()
            finally:
                with self._lock:
                    self._current = None
                self._on_change()


# Module-level singleton — everyone shares one channel.
CHANNEL = RadioChannel()


def speak(text: str, callsign: str = "DISPATCH", *, blocking: bool = True):
    """Speak `text` in the agent's voice with police-radio styling.

    Envelope: mic-key click  ->  filtered voice + under-static  ->  Motorola
    roger beep  ->  squelch tail. Output is a single 22.05 kHz mono WAV that
    plays through afplay.
    """
    _ensure_assets()
    voice = VOICE_MAP.get(callsign.upper(), "Kathy")
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "raw.aiff"
        dry = Path(td) / "dry.wav"
        wet = Path(td) / "wet.wav"
        final = Path(td) / "final.wav"

        # 1. macOS `say` -> AIFF, faster rate for clipped delivery
        subprocess.run(
            ["say", "-v", voice, "-r", str(SPEECH_RATE), "-o", str(raw), text],
            check=True,
        )
        # 2. AIFF -> wav 22050 mono
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(raw),
                "-ac", "1", "-ar", "22050",
                str(dry),
            ],
            check=True,
        )
        # 3. Voice through radio filter chain
        _radio_filter(dry, wet)
        # 4. Concatenate envelope. Re-encode (don't '-c copy') because the
        #    asset files have varying durations/seek points.
        list_path = Path(td) / "concat.txt"
        list_path.write_text(
            "\n".join(
                f"file '{p}'" for p in (MIC_CLICK, wet, ROGER_BEEP, SQUELCH_TAIL)
            )
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-ac", "1", "-ar", "22050",
                str(final),
            ],
            check=True,
        )
        # 5. Play
        play = subprocess.Popen(["afplay", str(final)])
        if blocking:
            play.wait()
        else:
            return play


def speak_async(text: str, callsign: str = "DISPATCH"):
    threading.Thread(target=speak, args=(text, callsign), daemon=True).start()
