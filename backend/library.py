"""Library management: local import, generated demo tracks, YouTube search/download,
and pulling BPM/key analysis back out of Mixxx's library database."""
from __future__ import annotations

import base64
import html as html_lib
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

from .config import AUDIO_EXTS, LIBRARY, FX_DIR, MIXXX_DB, SAMPLE_RATE, COVERS
from .db import Database

CAMELOT = {
    "Abm": "1A", "G#m": "1A", "B": "1B",
    "Ebm": "2A", "D#m": "2A", "F#": "2B", "Gb": "2B",
    "Bbm": "3A", "A#m": "3A", "Db": "3B", "C#": "3B",
    "Fm": "4A", "Ab": "4B", "G#": "4B",
    "Cm": "5A", "Eb": "5B", "D#": "5B",
    "Gm": "6A", "Bb": "6B", "A#": "6B",
    "Dm": "7A", "F": "7B",
    "Am": "8A", "C": "8B",
    "Em": "9A", "G": "9B",
    "Bm": "10A", "D": "10B",
    "F#m": "11A", "A": "11B",
    "C#m": "12A", "E": "12B",
}

CAMELOT_KEYS = list(CAMELOT.keys())


def clean_text(text: str) -> str:
    """Normalise stylised YouTube titles and strip decorative emoji."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def camelot_for(key: str) -> str:
    if not key:
        return ""
    k = key.strip()
    if re.fullmatch(r"\d{1,2}[ABab]", k):
        return k.upper()
    k = k.replace("♯", "#").replace("♭", "b")
    k = re.sub(r"\s+", "", k)
    k = re.sub(r"(?i)minor|min$", "m", k)
    k = re.sub(r"(?i)major|maj$", "", k)
    if len(k) > 1 and k[1] in "b#" and k[0].isalpha():
        pass
    if k and k[0].islower():
        k = k[0].upper() + k[1:]
    return CAMELOT.get(k, "")


def _decode_mono(path: str, sr: int = 22050) -> np.ndarray | None:
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le",
             "-ac", "1", "-ar", str(sr), "-"],
            capture_output=True, timeout=180,
        )
        if out.returncode != 0 or not out.stdout:
            return None
        x = np.frombuffer(out.stdout, dtype="<f4").astype(np.float32)
        return x if x.size > sr * 4 else None
    except Exception:
        return None


def _detect_bpm(x: np.ndarray, sr: int) -> float:
    """Spectral-flux autocorrelation tempo estimate, 70–180 BPM."""
    frame, hop = 1024, 512
    if x.size < frame * 8:
        return 0.0
    n = 1 + (x.size - frame) // hop
    frames = np.lib.stride_tricks.sliding_window_view(x, frame)[::hop]
    win = np.hanning(frame).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames * win, axis=1))
    flux = np.maximum(0.0, np.diff(spec, axis=0)).sum(axis=1)
    env = np.maximum(0.0, flux - float(np.mean(flux)))
    if env.size < 8 or float(np.max(env)) <= 0:
        return 0.0
    env = env / float(np.max(env))
    n = env.size
    fft = np.fft.rfft(env, 2 * n)
    ac = np.fft.irfft(fft * np.conj(fft))[:n]
    ac = ac / (ac[0] if ac[0] else 1.0)
    fps = sr / hop
    best_bpm, best = 0.0, -1.0
    for bpm in np.arange(70.0, 180.01, 0.25):
        lag = int(round(60.0 / bpm * fps))
        if lag < 2 or lag >= n:
            continue
        score = ac[lag] + 0.45 * ac[min(n - 1, 2 * lag)] + 0.2 * ac[min(n - 1, 3 * lag)]
        score *= math.exp(-0.5 * ((bpm - 124.0) / 38.0) ** 2)
        if score > best:
            best, best_bpm = score, float(bpm)
    return round(best_bpm, 1) if best > 0.02 else 0.0


def _detect_key(x: np.ndarray, sr: int) -> str:
    frame, hop = 4096, 2048
    if x.size < frame * 4:
        return ""
    frames = np.lib.stride_tricks.sliding_window_view(x, frame)[::hop]
    win = np.hanning(frame).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames * win, axis=1))
    freqs = np.fft.rfftfreq(frame, 1.0 / sr)
    band = (freqs >= 80) & (freqs <= 2400)
    spec = spec[:, band]
    freqs = freqs[band]
    if spec.size == 0:
        return ""
    energy = spec.mean(axis=1)
    keep = energy > np.percentile(energy, 55)
    if not np.any(keep):
        return ""
    pitch = np.round(69 + 12 * np.log2(freqs / 440.0)).astype(int) % 12
    chroma = np.bincount(pitch, weights=spec[keep].sum(axis=0), minlength=12)
    if chroma.sum() <= 0:
        return ""
    chroma = chroma / chroma.sum()
    major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    best, best_score = "", -1e9
    for root in range(12):
        for profile, suffix in ((major, ""), (minor, "m")):
            score = float(np.dot(chroma, np.roll(profile, root)))
            if score > best_score:
                best_score = score
                best = names[root] + suffix
    return best


def _detect_sections(x: np.ndarray, sr: int, bpm: float) -> dict:
    """Find the intro / first hook / outro and derive a DJ mix window.

    We play the hook of the current record and start the next record eight bars
    before its drop.  The 16-bar transition then lands the bass swap exactly on
    the incoming drop.
    """
    duration = len(x) / sr
    bar = 4.0 * 60.0 / max(60.0, bpm or 120.0)
    if duration < bar * 8:
        return {"intro_sec": 0.0, "drop_sec": max(0.0, duration * 0.2),
                "outro_sec": duration, "mix_in_sec": 0.0,
                "mix_out_sec": max(0.0, duration - bar * 4)}
    win = max(256, int(sr * 0.5))
    hop = max(128, win // 2)
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    rms = np.sqrt(np.mean(frames.astype(np.float32) ** 2, axis=1))
    if rms.size > 8:
        rms = np.convolve(rms, np.ones(5) / 5, mode="same")
    peak = float(np.percentile(rms, 95)) if rms.size else 0.0
    if peak <= 0:
        return {"intro_sec": 0.0, "drop_sec": duration * 0.2,
                "outro_sec": duration, "mix_in_sec": 0.0,
                "mix_out_sec": max(0.0, duration - bar * 4)}
    times = np.arange(rms.size) * (hop / sr)
    # Intro: first sustained low-level musical activity.
    intro = 0.0
    active = np.where(rms > peak * 0.22)[0]
    if active.size:
        intro = max(0.0, float(times[active[0]]) - 1.0)
    # Drop: first sustained high-energy section after the intro.
    drop = None
    threshold = peak * 0.60
    for i in range(len(rms) - 4):
        if times[i] < 8:
            continue
        if np.all(rms[i:i + 4] >= threshold):
            drop = float(times[i])
            break
    if drop is None:
        after = np.where(times >= 8)[0]
        if after.size:
            drop = float(times[after[int(np.argmax(rms[after]))]])
        else:
            drop = duration * 0.2
    # Play the hook for 32 bars, then begin the 16-bar mix.  Never earlier than
    # eight bars before the file ends.
    snap = 4.0 * 60.0 / max(60.0, bpm or 120.0)
    mix_in = max(0.0, drop - 8.0 * bar)
    mix_in = round(mix_in / snap) * snap
    mix_out = min(duration - 4.0 * bar, drop + 32.0 * bar)
    if mix_out < mix_in + 8.0 * bar:
        mix_out = max(mix_in + 8.0 * bar, duration - 4.0 * bar)
    mix_out = round(mix_out / snap) * snap
    outro = min(duration, mix_out + 16.0 * bar)
    return {
        "intro_sec": round(intro, 2),
        "drop_sec": round(drop, 2),
        "outro_sec": round(outro, 2),
        "mix_in_sec": round(mix_in, 2),
        "mix_out_sec": round(mix_out, 2),
    }


def analyze_file(path: str) -> dict:
    x = _decode_mono(path)
    if x is None:
        return {}
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    bpm = _detect_bpm(x, 22050)
    key = _detect_key(x, 22050)
    sections = _detect_sections(x, 22050, bpm)
    return {
        "bpm": bpm,
        "musical_key": key,
        "camelot": camelot_for(key) if key else "",
        "energy": round(min(1.0, max(0.15, rms * 7.0)), 2),
        **sections,
        "sections": True,
    }


def probe(path: Path) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:format_tags=title,artist,album",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(out.stdout or "{}")
        fmt = data.get("format", {})
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        return {
            "duration": float(fmt.get("duration") or 0),
            "title": tags.get("title", ""),
            "artist": tags.get("artist", ""),
            "album": tags.get("album", ""),
        }
    except Exception:
        return {"duration": 0, "title": "", "artist": "", "album": ""}


def _safe(text: str, limit: int = 80) -> str:
    text = re.sub(r"[^A-Za-z0-9 _.\-]+", "", text or "").strip()
    text = re.sub(r"\s+", "_", text)
    return text[:limit] or "Untitled"


def _mixxx_key_to_camelot(key: str) -> str:
    """Mixxx can store either a musical key or a Camelot code."""
    if not key:
        return ""
    k = key.strip()
    if re.fullmatch(r"\d{1,2}[ABab]", k):
        return k.upper()
    # Mixxx 2.5 sometimes writes "A minor" / "F# major"
    return camelot_for(k)


class Library:
    def __init__(self, db: Database):
        self.db = db
        self._analysis_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ files
    def _new_token(self) -> str:
        return "dj" + secrets.token_hex(4)

    def _dest_for(self, token: str, title: str, artist: str, suffix: str) -> Path:
        return LIBRARY / f"{token}__{_safe(artist)}_-_{_safe(title)}{suffix}"

    def add_file(self, src: Path | str, *, title: str = "", artist: str = "",
                 album: str = "", source: str = "local", copy: bool = True,
                 bpm: float = 0, camelot: str = "", energy: float = 0.5,
                 suffix: str | None = None, artwork: str = "",
                 cache_artwork: bool = True) -> dict:
        src = Path(src)
        info = probe(src)
        title = clean_text(title or info["title"] or src.stem)
        artist = clean_text(artist or info["artist"])
        album = clean_text(album or info["album"])
        duration = info["duration"]
        token = self._new_token()
        suffix = suffix or src.suffix.lower()
        if suffix not in AUDIO_EXTS:
            suffix = ".mp3"

        if copy:
            dest = self._dest_for(token, title, artist, suffix)
            if dest.exists():
                dest = LIBRARY / f"{token}_{secrets.token_hex(2)}__{_safe(title)}{suffix}"
            shutil.copy2(src, dest)
        else:
            # Keep the file where it is, but make it uniquely searchable in Mixxx.
            dest = src
            if token not in src.name:
                new_name = f"{token}__{_safe(artist)}_-_{_safe(title)}{src.suffix.lower()}"
                new_path = src.with_name(new_name)
                if new_path != src and not new_path.exists():
                    src.rename(new_path)
                    dest = new_path

        artwork_url = self._artwork_for(dest, token, title, artist, artwork,
                                        cache=cache_artwork)
        return self.db.add_track(
            str(dest), token, title, artist, album, duration,
            bpm=bpm, camelot=camelot, energy=energy, source=source,
            artwork=artwork_url, analyzed=bool(bpm > 0 and camelot),
        )

    # --------------------------------------------------------------- artwork
    def _fallback_cover(self, token: str, title: str, artist: str) -> str:
        path = COVERS / f"{token}.svg"
        hue = int(token[-6:], 16) % 360
        hue2 = (hue + 58) % 360
        title_e = html_lib.escape((title or "Untitled")[:42])
        artist_e = html_lib.escape((artist or "DonJon")[:46])
        initial = html_lib.escape((title or "D")[:1].upper())
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="420" height="420" viewBox="0 0 420 420">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="hsl({hue},72%,34%)"/>
<stop offset="0.55" stop-color="hsl({hue2},55%,13%)"/>
<stop offset="1" stop-color="hsl({(hue+120)%360},48%,10%)"/>
</linearGradient></defs>
<rect width="420" height="420" fill="url(#g)"/>
<circle cx="330" cy="86" r="132" fill="rgba(255,255,255,0.07)"/>
<circle cx="58" cy="356" r="104" fill="rgba(0,0,0,0.22)"/>
<text x="34" y="128" font-family="Inter,Segoe UI,sans-serif" font-size="96" font-weight="800" fill="rgba(255,255,255,0.92)">{initial}</text>
<text x="34" y="314" font-family="Inter,Segoe UI,sans-serif" font-size="27" font-weight="800" fill="#fff">{title_e}</text>
<text x="34" y="348" font-family="Inter,Segoe UI,sans-serif" font-size="17" font-weight="600" fill="rgba(255,255,255,0.68)">{artist_e}</text>
</svg>"""
        path.write_text(svg, encoding="utf-8")
        return f"/art/{token}.svg"

    def _artwork_for(self, audio: Path, token: str, title: str, artist: str,
                     remote: str = "", cache: bool = True) -> str:
        # 1. Download the YouTube/Spotify cover once so it also works offline.
        if remote and remote.startswith(("http://", "https://")) and not cache:
            return remote
        if remote and remote.startswith(("http://", "https://")):
            cover = COVERS / f"{token}.jpg"
            tmp = COVERS / f"{token}.download"
            try:
                req = urllib.request.Request(remote, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=12) as r:
                    tmp.write_bytes(r.read())
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp),
                     "-vf", "scale=420:-1", "-q:v", "3", str(cover)],
                    capture_output=True, timeout=25,
                )
                if cover.exists() and cover.stat().st_size > 800:
                    return f"/art/{token}.jpg"
            except Exception:
                pass
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
        # 2. Use embedded album art from the audio file.
        cover = COVERS / f"{token}.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio),
                 "-an", "-map", "0:v:0", "-frames:v", "1",
                 "-vf", "scale=420:-1", "-q:v", "3", str(cover)],
                capture_output=True, timeout=25,
            )
            if cover.exists() and cover.stat().st_size > 800:
                return f"/art/{token}.jpg"
        except Exception:
            pass
        if cover.exists():
            try:
                cover.unlink()
            except Exception:
                pass
        # 3. A designed gradient cover with the track's initial.
        return self._fallback_cover(token, title, artist)

    def backfill_artwork(self, fetch_remote: bool = True) -> int:
        changed = 0
        for t in self.db.all_tracks():
            if t.get("artwork"):
                continue
            remote = ""
            if fetch_remote and t.get("source") in ("youtube", "spotify"):
                try:
                    results = self.youtube_search(
                        f"{t.get('artist','')} {t.get('title','')}".strip(), limit=1)
                    if results:
                        remote = results[0].get("artwork") or ""
                except Exception:
                    pass
            art = self._artwork_for(Path(t["path"]), t["token"], t["title"],
                                    t.get("artist", ""), remote)
            self.db.set_artwork(t["token"], art)
            changed += 1
        return changed

    def scan_library(self) -> list[dict]:
        added = []
        known = {t["path"] for t in self.db.all_tracks()}
        for p in sorted(LIBRARY.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
            if str(p) in known:
                continue
            m = re.match(r"(dj[0-9a-f]{8})__", p.name)
            if not m:
                # Adopt a pre-existing file, renaming it with a stable token.
                token = self._new_token()
                newp = self._dest_for(token, p.stem, "", p.suffix.lower())
                try:
                    p.rename(newp)
                except OSError:
                    newp = p
                p = newp
            added.append(self.add_file(p, copy=False))
        return added

    def import_folder(self, folder: str) -> dict:
        root = Path(folder).expanduser()
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(folder)
        added = 0
        skipped = 0
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
            # Skip files we already track by name or content path.
            if any(t["token"] in p.name for t in self.db.all_tracks()):
                skipped += 1
                continue
            self.add_file(p, source="import", copy=True)
            added += 1
        return {"added": added, "skipped": skipped}

    # --------------------------------------------------------------- demos
    def generate_demos(self) -> int:
        if self.db.all_tracks():
            return 0
        # Six original 64-second loops.  Not copyrighted, and deliberately
        # compatible with each other so the first auto-mix sounds intentional.
        specs = [
            ("Neon Skyline", "DonJon Lab", 124, "Am", 0.55, [0, 0, 5, 3], 220.0),
            ("Midnight Circuit", "DonJon Lab", 126, "Cm", 0.62, [0, 3, 5, 3], 261.63),
            ("Velvet Static", "DonJon Lab", 128, "Gm", 0.72, [0, 5, 3, 7], 196.0),
            ("Afterglow Drive", "DonJon Lab", 130, "Dm", 0.80, [0, 3, 7, 5], 293.66),
            ("Chrome Sunrise", "DonJon Lab", 122, "F", 0.45, [0, 4, 5, 3], 174.61),
            ("Redline Ritual", "DonJon Lab", 132, "Em", 0.92, [0, 5, 3, 7], 164.81),
        ]
        made = 0
        for title, artist, bpm, key, energy, progression, root in specs:
            wav = FX_DIR / f"demo_{_safe(title)}.wav"
            if not wav.exists():
                self._synth_track(wav, bpm, root, progression)
            mp3 = wav.with_suffix(".mp3")
            if not mp3.exists():
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                     "-codec:a", "libmp3lame", "-q:a", "3", str(mp3)],
                    check=False, timeout=90,
                )
            if mp3.exists():
                self.add_file(
                    mp3, title=title, artist=artist, album="DonJon Demo Reel",
                    source="demo", bpm=float(bpm), camelot=camelot_for(key),
                    energy=energy, suffix=".mp3",
                )
                made += 1
        return made

    @staticmethod
    def _synth_track(path: Path, bpm: int, root_hz: float,
                     progression: list[int], seconds: float = 64.0) -> None:
        sr = SAMPLE_RATE
        n = int(sr * seconds)
        t = np.arange(n, dtype=np.float32) / sr
        beat = 60.0 / bpm
        bar = beat * 4
        rng = np.random.default_rng(abs(hash((bpm, root_hz))) % 2**32)

        kick = np.zeros(n, dtype=np.float32)
        snare = np.zeros(n, dtype=np.float32)
        hats = np.zeros(n, dtype=np.float32)
        bass = np.zeros(n, dtype=np.float32)
        pad = np.zeros(n, dtype=np.float32)
        arp = np.zeros(n, dtype=np.float32)

        def add(buf, start, env, wave):
            i = int(start * sr)
            m = min(len(wave), n - i)
            if m > 0:
                buf[i:i + m] += (wave[:m] * env[:m]).astype(np.float32)

        semis = [0, 2, 3, 5, 7, 8, 10]
        for b in range(int(seconds / beat) + 1):
            at = b * beat
            # kick on every beat with a soft off-beat ghost
            L = int(0.18 * sr)
            env = np.exp(-np.linspace(0, 16, L)).astype(np.float32)
            f = np.linspace(90, 42, L)
            add(kick, at, env, 0.62 * np.sin(2 * np.pi * f * np.arange(L) / sr))
            if b % 4 in (1, 3):
                L2 = int(0.16 * sr)
                noise = rng.standard_normal(L2).astype(np.float32)
                env2 = np.exp(-np.linspace(0, 22, L2)).astype(np.float32)
                add(snare, at, env2, 0.20 * noise)
            # hats on 8ths
            for off in (0.0, 0.5):
                L3 = int(0.035 * sr)
                h = rng.standard_normal(L3).astype(np.float32)
                he = np.exp(-np.linspace(0, 30, L3)).astype(np.float32)
                add(hats, at + off * beat, he, 0.055 * h)

        for b in range(int(seconds / bar) + 1):
            start = b * bar
            root = root_hz * 2 ** (progression[b % len(progression)] / 12.0)
            # bass: root on 1 and 3, fifth on the & of 2
            for k, mul in ((0.0, 1.0), (0.75, 1.5), (2.0, 1.0), (3.5, 1.5)):
                L = int(0.22 * sr)
                tt = np.arange(L) / sr
                f = root * mul
                env = np.exp(-np.linspace(0, 8, L)).astype(np.float32)
                add(bass, start + k * beat, env, 0.22 * np.sin(2 * np.pi * f * tt))
            # pad chord (root, minor/major third, fifth, octave)
            third = 3 if "m" in (progression and "") else 4
            # use a minor-ish colour for all demos; the key label is advisory
            chord = [0, 3, 7, 12]
            L = int(bar * sr)
            tt = np.arange(L) / sr
            env = np.minimum(1, np.linspace(0, 0.15, L)) * np.exp(-tt * 0.5)
            env = env.astype(np.float32)
            for semi in chord:
                f = root * 2 ** (semi / 12.0)
                pad_i = int(start * sr)
                m = min(L, n - pad_i)
                if m > 0:
                    wave = (0.045 * np.sin(2 * np.pi * f * tt[:m]) +
                            0.022 * np.sin(2 * np.pi * f * 2.01 * tt[:m]))
                    pad[pad_i:pad_i + m] += (wave * env[:m]).astype(np.float32)
            # arp on 16ths
            for step in range(16):
                semi = [0, 3, 7, 12, 7, 3, 10, 7][step % 8]
                f = root * 2 ** (semi / 12.0) * 2
                L2 = int(0.12 * sr)
                tt2 = np.arange(L2) / sr
                env2 = np.exp(-np.linspace(0, 14, L2)).astype(np.float32)
                add(arp, start + step * beat / 4, env2,
                    0.035 * np.sin(2 * np.pi * f * tt2))

        mix = kick + snare + hats + bass + pad + arp
        # A little stereo width and a soft limiter.
        left = mix + 0.08 * arp
        right = mix - 0.08 * arp
        stereo = np.stack([left, right], axis=1)
        peak = float(np.max(np.abs(stereo))) or 1.0
        stereo = np.tanh(stereo / peak * 1.2) * 0.88
        import soundfile as sf
        sf.write(str(path), stereo.astype(np.float32), sr, subtype="PCM_16")

    # ---------------------------------------------------------------- youtube
    def youtube_search(self, query: str, limit: int = 10) -> list[dict]:
        # YouTube Music is the primary catalogue: songs, real artwork, clean
        # artist names.  Fall back to yt-dlp's YouTube search if it is blocked.
        try:
            from ytmusicapi import YTMusic
            yt = YTMusic()
            results = yt.search(query, filter="songs", limit=limit)
            out = []
            for r in results:
                vid = r.get("videoId")
                if not vid:
                    continue
                thumbs = r.get("thumbnails") or []
                artwork = (thumbs[-1].get("url", "") if thumbs
                           else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
                artist = ", ".join(
                    a.get("name", "") for a in (r.get("artists") or []) if a.get("name"))
                out.append({
                    "id": vid,
                    "title": clean_text(r.get("title") or "Untitled"),
                    "artist": clean_text(artist or r.get("album") or "YouTube Music"),
                    "album": clean_text((r.get("album") or {}).get("name") if isinstance(r.get("album"), dict) else r.get("album") or ""),
                    "duration": float(r.get("duration_seconds") or 0),
                    "artwork": artwork,
                })
            if out:
                return out
        except Exception:
            pass
        try:
            import yt_dlp
        except Exception as e:
            raise RuntimeError("yt-dlp is not installed in the server environment") from e
        opts = {
            "quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True, "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        out = []
        for e in (info or {}).get("entries", []) or []:
            if not e:
                continue
            thumbs = e.get("thumbnails") or []
            artwork = thumbs[-1].get("url", "") if thumbs else f"https://i.ytimg.com/vi/{e.get('id')}/mqdefault.jpg"
            out.append({
                "id": e.get("id"),
                "title": clean_text(e.get("title") or "Untitled"),
                "artist": clean_text(e.get("uploader") or e.get("channel") or "YouTube Music"),
                "duration": float(e.get("duration") or 0),
                "artwork": artwork,
            })
        return out

    def youtube_download(self, video_id: str, title: str, artist: str,
                         source: str = "youtube", artwork: str = "",
                         cache_artwork: bool = True) -> dict:
        try:
            import yt_dlp
        except Exception as e:
            raise RuntimeError("yt-dlp is not installed in the server environment") from e
        title = clean_text(title) or "YouTube track"
        artist = clean_text(artist) or "YouTube"
        tmp = FX_DIR / f"dl_{video_id}_{int(time.time())}"
        tmp.mkdir(parents=True, exist_ok=True)
        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "bestaudio/best",
            "outtmpl": str(tmp / "%(id)s.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        candidates = sorted(tmp.glob("*.mp3"))
        if not candidates:
            raise RuntimeError("download produced no audio file")
        track = self.add_file(
            candidates[0], title=title, artist=artist,
            source=source, suffix=".mp3", artwork=artwork,
            cache_artwork=cache_artwork,
        )
        shutil.rmtree(tmp, ignore_errors=True)
        return track

    # ------------------------------------------------------------ youtube playlists
    @staticmethod
    def _playlist_id(url: str) -> str:
        m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url or "")
        if m:
            return m.group(1)
        m = re.search(r"playlist[/:]([A-Za-z0-9_-]+)", url or "")
        return m.group(1) if m else ""

    def youtube_playlist(self, url: str, limit: int = 100) -> dict:
        pid = self._playlist_id(url)
        if pid:
            try:
                from ytmusicapi import YTMusic
                yt = YTMusic()
                pl = yt.get_playlist(pid, limit=max(1, limit))
                tracks = []
                for e in (pl.get("tracks") or []):
                    vid = e.get("videoId")
                    if not vid:
                        continue
                    thumbs = e.get("thumbnails") or []
                    artwork = (thumbs[-1].get("url", "") if thumbs
                               else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
                    artist = ", ".join(
                        a.get("name", "") for a in (e.get("artists") or []) if a.get("name"))
                    tracks.append({
                        "id": vid,
                        "title": clean_text(e.get("title") or "Untitled"),
                        "artist": clean_text(artist or "YouTube Music"),
                        "album": clean_text((e.get("album") or {}).get("name") if isinstance(e.get("album"), dict) else e.get("album") or ""),
                        "duration": float(e.get("duration_seconds") or 0),
                        "artwork": artwork,
                        "source": "youtube",
                    })
                if tracks:
                    thumbs = pl.get("thumbnails") or []
                    full_count = int(pl.get("trackCount") or len(tracks))
                    tracks = tracks[:max(1, limit)]
                    return {
                        "id": pid,
                        "name": clean_text(pl.get("title") or "YouTube Music playlist"),
                        "count": full_count,
                        "artwork": (thumbs[-1].get("url", "") if thumbs else tracks[0]["artwork"]),
                        "tracks": tracks,
                    }
            except Exception:
                pass
        try:
            import yt_dlp
        except Exception as e:
            raise RuntimeError("yt-dlp is not installed in the server environment") from e
        opts = {
            "quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True, "noplaylist": False,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("could not read that playlist")
        entries = []
        for e in (info.get("entries") or [])[:limit]:
            if not e:
                continue
            thumbs = e.get("thumbnails") or []
            entries.append({
                "id": e.get("id"),
                "title": clean_text(e.get("title") or "Untitled"),
                "artist": clean_text(e.get("uploader") or e.get("channel") or "YouTube Music"),
                "duration": float(e.get("duration") or 0),
                "artwork": (thumbs[-1].get("url", "") if thumbs
                            else f"https://i.ytimg.com/vi/{e.get('id')}/mqdefault.jpg"),
                "source": "youtube",
            })
        return {
            "id": info.get("id") or "",
            "name": clean_text(info.get("title") or "YouTube Music playlist"),
            "count": int(info.get("playlist_count") or len(entries)),
            "artwork": entries[0]["artwork"] if entries else "",
            "tracks": entries,
        }

    def import_youtube_playlist(self, url: str, limit: int = 100,
                                progress=None, playlist_name: str = "") -> dict:
        preview = self.youtube_playlist(url, limit=limit)
        name = playlist_name.strip() or preview["name"]
        pl = self.db.create_playlist(name)
        tracks = preview["tracks"]
        total = len(tracks)
        existing = {t["title"].lower(): t for t in self.db.all_tracks()}
        added = 0
        errors = 0

        def fetch(entry):
            hit = existing.get((entry.get("title") or "").lower())
            if hit:
                if not hit.get("artwork") and entry.get("artwork"):
                    self.db.set_artwork(hit["token"], entry["artwork"])
                    hit["artwork"] = entry["artwork"]
                return hit
            return self.youtube_download(
                entry["id"], entry["title"], entry["artist"],
                artwork=entry.get("artwork", ""), cache_artwork=False)

        # Four concurrent downloads turn a 174-track playlist from a 25-minute
        # job into a manageable one.  Each track still lands in the playlist.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fetch, e): e for e in tracks}
            for i, fut in enumerate(as_completed(futures), start=1):
                entry = futures[fut]
                try:
                    track = fut.result()
                    self.db.add_to_playlist(pl["id"], track["id"])
                    added += 1
                except Exception:
                    errors += 1
                if progress:
                    try:
                        progress(i, total, f"{i}/{total} · {entry['title'][:44]}")
                    except Exception:
                        pass
        return {"playlist": pl, "playlist_id": pl["id"], "added": added,
                "errors": errors, "total": total, "name": name}

    # ---------------------------------------------------------------- analysis
    def start_analysis_sync(self, interval: float = 4.0) -> None:
        if self._analysis_thread and self._analysis_thread.is_alive():
            return
        self._stop.clear()
        self._analysis_thread = threading.Thread(
            target=self._analysis_loop, args=(interval,), daemon=True,
            name="mixxx-analysis",
        )
        self._analysis_thread.start()

    def stop_analysis_sync(self) -> None:
        self._stop.set()

    def analyze_pending(self) -> int:
        """Fill BPM/key and DJ section cues for imported/downloaded tracks."""
        candidates = self.db.analysis_candidates(8)
        done = 0
        for tr in candidates:
            if time.time() - float(tr.get("added_at") or 0) < 4:
                continue
            result = {}
            try:
                result = analyze_file(tr["path"])
            except Exception:
                result = {}
            if result.get("bpm") or result.get("camelot"):
                self.db.update_analysis(
                    tr["token"], result.get("bpm", 0),
                    result.get("musical_key", ""), result.get("camelot", ""),
                    energy=result.get("energy", 0), analyzed=True,
                    intro_sec=result.get("intro_sec", 0),
                    drop_sec=result.get("drop_sec", 0),
                    outro_sec=result.get("outro_sec", 0),
                    mix_in_sec=result.get("mix_in_sec", 0),
                    mix_out_sec=result.get("mix_out_sec", 0),
                    sections=bool(result.get("sections")),
                )
                done += 1
            else:
                # Unreadable file: stop retrying it forever, but keep it usable.
                self.db.update_analysis(tr["token"], analyzed=True, sections=True)
            self.db.mark_analyzed(tr["token"])
        return done

    def _analysis_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.sync_analysis()
                self.analyze_pending()
            except Exception:
                pass

    def sync_analysis(self) -> int:
        if not MIXXX_DB.exists():
            return 0
        pending = self.db.pending_analysis()
        if not pending:
            return 0
        updated = 0
        try:
            conn = sqlite3.connect(f"file:{MIXXX_DB}?mode=ro", uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            for tr in pending:
                row = conn.execute(
                    """SELECT l.bpm, l.key, l.duration
                       FROM library l JOIN track_locations tl ON l.location=tl.id
                       WHERE tl.filename LIKE ? LIMIT 1""",
                    (f"%{tr['token']}%",),
                ).fetchone()
                if not row or not row["bpm"]:
                    continue
                camelot = _mixxx_key_to_camelot(row["key"] or "")
                self.db.update_analysis(
                    tr["token"], float(row["bpm"]), row["key"] or "",
                    camelot, float(row["duration"] or 0),
                )
                updated += 1
            conn.close()
        except sqlite3.Error:
            pass
        return updated
