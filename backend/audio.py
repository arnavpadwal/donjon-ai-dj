"""Capture Mixxx's master output and fan it out to every browser as timed PCM.

All clients receive the same 20 ms frames, each stamped with the server time it
was read.  Browsers schedule the frames on their own AudioContext; the constant
capture-to-playback buffer keeps every phone/laptop on the same beat.
"""
from __future__ import annotations

import asyncio
import collections
import math
import os
import struct
import subprocess
import threading
import time
from typing import Any

from .config import (
    AUDIO_SINK, CHUNK_BYTES, CHUNK_FRAMES, SAMPLE_RATE, CHANNELS,
)

MAGIC = b"DONJ"


def _sink_exists() -> bool:
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"],
                             capture_output=True, text=True, timeout=5)
        return AUDIO_SINK in out.stdout
    except Exception:
        return False


def ensure_sink() -> bool:
    if _sink_exists():
        return True
    try:
        subprocess.run(
            ["pactl", "load-module", "module-null-sink",
             f"sink_name={AUDIO_SINK}", "rate=48000", "channels=2",
             f"sink_properties=device.description={AUDIO_SINK}"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False
    return _sink_exists()


class AudioClient:
    def __init__(self, ws):
        self.ws = ws
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=80)

    def push(self, frame: bytes) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass


class AudioStreamer:
    def __init__(self):
        self.clients: set[AudioClient] = set()
        self._lock = threading.Lock()
        self._frames: collections.deque[bytes] = collections.deque(maxlen=120)
        self._seq = 0
        self._timeline_start_ms = 0.0
        self._timeline_index = 0
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pump_task: asyncio.Task | None = None
        self.rms = 0.0
        self.ready = False
        self.last_error = ""
        self.frames_sent = 0
        self._beep_until = 0.0
        self._beep_phase = 0.0

    # ---------------------------------------------------------------- lifecycle
    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if not ensure_sink():
            self.last_error = "could not create the DonJon audio sink"
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True,
                                        name="audio-capture")
        self._thread.start()

    async def start_pump(self) -> None:
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump_loop())

    async def _pump_loop(self) -> None:
        while True:
            frames = []
            with self._lock:
                while self._frames:
                    frames.append(self._frames.popleft())
            if frames and self.clients:
                now_ms = time.time() * 1000.0
                live = []
                for f in frames:
                    # Never let a newly connected client (or a stalled pump)
                    # play a stale backlog.  1.5 s is enough to absorb normal
                    # Wi-Fi jitter while still staying effectively live.
                    f_ts = struct.unpack_from("<d", f, 8)[0]
                    if now_ms - f_ts > 1500:
                        continue
                    live.append(f)
                for f in live:
                    for c in list(self.clients):
                        c.push(f)
                self.frames_sent += len(live) * len(self.clients)
            await asyncio.sleep(0.01)

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._pump_task:
            self._pump_task.cancel()

    # ------------------------------------------------------------------ capture
    def _capture_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            cmd = [
                "ffmpeg", "-nostdin", "-loglevel", "error",
                "-f", "pulse", "-fragment_size", str(CHUNK_BYTES),
                "-i", f"{AUDIO_SINK}.monitor",
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-",
            ]
            env = dict(os.environ)
            env["PULSE_LATENCY_MSEC"] = "100"
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    bufsize=0, env=env,
                )
                # Timestamp audio by its position in the stream, not by the
                # moment ffmpeg happened to hand us a burst of samples.  This
                # is what keeps the room on a smooth, regular clock.
                self._timeline_start_ms = time.time() * 1000.0
                self._timeline_index = 0
                self.ready = True
                self.last_error = ""
                backoff = 1.0
                buf = bytearray()
                while not self._stop.is_set():
                    chunk = self._proc.stdout.read(CHUNK_BYTES - len(buf))
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) < CHUNK_BYTES:
                        continue
                    pcm = bytes(buf)
                    buf.clear()
                    if time.time() < self._beep_until:
                        pcm = self._mix_beep(pcm)
                    seq = self._seq
                    self._seq = (self._seq + 1) & 0xFFFFFFFF
                    ts = self._timeline_start_ms + (
                        self._timeline_index * CHUNK_FRAMES * 1000.0 / SAMPLE_RATE
                    )
                    self._timeline_index += 1
                    frame = struct.pack("<4sId", MAGIC, seq, ts) + pcm
                    self._update_rms(pcm)
                    with self._lock:
                        self._frames.append(frame)
            except Exception as e:
                self.last_error = str(e)
            finally:
                try:
                    if self._proc:
                        self._proc.terminate()
                except Exception:
                    pass
                self._proc = None
                self.ready = False
            if not self._stop.is_set():
                self.last_error = self.last_error or "capture stopped"
                self._stop.wait(backoff)
                backoff = min(5.0, backoff * 1.6)

    def beep(self, seconds: float = 1.2) -> None:
        self._beep_phase = 0.0
        self._beep_until = time.time() + seconds

    def _mix_beep(self, pcm: bytes) -> bytes:
        import array
        a = array.array("h")
        a.frombytes(pcm)
        for i in range(0, len(a) - 1, 2):
            v = int(9000 * math.sin(self._beep_phase))
            self._beep_phase += 2 * math.pi * 1000 / SAMPLE_RATE
            a[i] = max(-32768, min(32767, a[i] + v))
            a[i + 1] = max(-32768, min(32767, a[i + 1] + v))
        return a.tobytes()

    def _update_rms(self, pcm: bytes) -> None:
        # Cheap RMS on a subset of samples, enough for the UI meter.
        n = len(pcm) // 2
        if n <= 0:
            return
        try:
            import array
            a = array.array("h")
            a.frombytes(pcm[: n * 2])
            step = max(1, len(a) // 400)
            s = sum(a[i] * a[i] for i in range(0, len(a), step))
            count = len(range(0, len(a), step)) or 1
            self.rms = math.sqrt(s / count) / 32768.0
        except Exception:
            pass

    # ------------------------------------------------------------------- clients
    def add_client(self, ws) -> AudioClient:
        c = AudioClient(ws)
        self.clients.add(c)
        return c

    def remove_client(self, c: AudioClient) -> None:
        self.clients.discard(c)

    def state(self) -> dict:
        return {
            "ready": self.ready,
            "rms": round(self.rms, 3),
            "clients": len(self.clients),
            "frames_sent": self.frames_sent,
            "error": self.last_error,
            "sample_rate": SAMPLE_RATE,
            "chunk_ms": round(CHUNK_FRAMES / SAMPLE_RATE * 1000, 1),
        }
