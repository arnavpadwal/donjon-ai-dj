"""Drive a hidden Mixxx instance from DonJon.

Why keyboard automation?  The Arch Mixxx build used here links PortMidi but
does not instantiate the PortMidi controller enumerator, so a virtual MIDI
controller never appears in the UI.  Mixxx's keyboard mapping is the stable,
documented control surface that is always available.  Mixxx runs on a private
Xvfb display, so the host's browser can keep focus while DonJon sends keys.

The audio itself -- beat sync, EQ, filters, loops, samplers and the actual
mix -- is 100% Mixxx.  DonJon only automates the same controls a DJ would use.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from .config import (
    AUDIO_SINK, FX_DIR, KEYMAP, KEYMAP_FILE, LIBRARY, MIXXX_DB, MIXXX_LOG,
    PROFILE, SAMPLE_RATE,
)
from .db import Database
from .library import Library, camelot_for

HID_STUB_C = r"""
/* DonJon: skip USB-HID enumeration.  On some Arch/Mixxx builds hid_enumerate
   blocks forever on a built-in device; DonJon controls Mixxx with the keyboard
   mapping instead, so HID controllers are not needed. */
struct hid_device_info;
int hid_init(void) { return 0; }
int hid_exit(void) { return 0; }
struct hid_device_info *hid_enumerate(unsigned short v, unsigned short p) {
    (void)v; (void)p; return 0;
}
void hid_free_enumeration(struct hid_device_info *d) { (void)d; }
"""


class Deck:
    def __init__(self, number: int):
        self.number = number
        self.track: dict | None = None
        self.playing = False
        self.started_at = 0.0
        self.base_pos = 0.0
        self.duration = 0.0
        self.bpm = 0.0

    def position(self) -> float:
        if self.playing:
            return min(self.duration or 1e9,
                       self.base_pos + (time.monotonic() - self.started_at))
        return min(self.duration or 1e9, self.base_pos)

    def start_clock(self) -> None:
        self.started_at = time.monotonic()
        self.playing = True

    def stop_clock(self) -> None:
        if self.playing:
            self.base_pos = self.position()
        self.playing = False


class MixxxEngine:
    def __init__(self, db: Database, library: Library, audio=None):
        self.db = db
        self.library = library
        self.audio = audio
        self.decks = {1: Deck(1), 2: Deck(2)}
        self.current_deck = 1
        self.xfader = 0.0            # 0 = deck A, 1 = deck B
        self.ready = False
        self.error = ""
        self.display = ":99"
        self.events: list[dict] = []
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._xvfb: subprocess.Popen | None = None
        self._start_thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._win_id: str | None = None
        self._first_load = True

    # ----------------------------------------------------------------- logging
    def log(self, message: str, kind: str = "info") -> None:
        item = {"t": time.time(), "kind": kind, "message": message}
        with self._lock:
            self.events.append(item)
            self.events = self.events[-80:]
        print(f"[mixxx:{kind}] {message}", flush=True)

    # ----------------------------------------------------------------- profile
    def ensure_profile(self) -> None:
        PROFILE.mkdir(parents=True, exist_ok=True)
        # Seed the Mixxx DB with our library path before startup.  WAL is
        # important: DonJon reads BPM/key while Mixxx is scanning/writing.
        try:
            conn = sqlite3.connect(MIXXX_DB)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS directories (directory TEXT UNIQUE)")
            for folder in (LIBRARY, FX_DIR):
                conn.execute("INSERT OR IGNORE INTO directories VALUES (?)", (str(folder),))
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass

        (PROFILE / "soundconfig.xml").write_text(
            f"""<SoundManagerConfig api="ALSA" samplerate="{SAMPLE_RATE}" latency="6" sync_buffers="2" force_network_clock="0" deck_count="2">
  <SoundDevice name="pulse" portAudioIndex="13" alsaHwDevice="pulse">
    <output type="Master" index="0" channel="0" channel_count="2"/>
  </SoundDevice>
</SoundManagerConfig>
""", encoding="utf-8")

        # Mixxx's config parser is space separated, *not* key=value.
        (PROFILE / "mixxx.cfg").write_text(
            """[Config]
Version 2.5.6
show_menubar_hint 0
hide_menubar 1
StartInFullscreen 0
ScaleFactor 1

[Soundcard]
Samplerate 48000

[Library]
RescanOnStartup 1
""", encoding="utf-8")

        (PROFILE / "Custom.kbd.cfg").write_text(KEYMAP_FILE, encoding="utf-8")
        self.ensure_fx()
        # Deliberately empty: loading a sampler bank at startup makes Mixxx
        # write to its DB while the library scanner is also writing.  DonJon
        # loads the overlays into samplers from the library after startup.
        (PROFILE / "samplers.xml").write_text("<samplerbank/>", encoding="utf-8")
        self.compile_hid_stub()

    def compile_hid_stub(self) -> None:
        src = PROFILE / "hid_stub.c"
        out = PROFILE / "hid_stub.so"
        src.write_text(HID_STUB_C, encoding="utf-8")
        if out.exists():
            return
        try:
            subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(out), str(src)],
                           capture_output=True, timeout=30)
        except Exception:
            pass

    # --------------------------------------------------------------------- fx
    def ensure_fx(self) -> None:
        import soundfile as sf
        sr = SAMPLE_RATE
        if not (FX_DIR / "donjon_fx_riser.wav").exists():
            n = int(sr * 4)
            t = np.arange(n) / sr
            f = 180 * 2 ** (np.linspace(0, 4.5, n))
            sweep = np.sin(2 * np.pi * np.cumsum(f) / sr)
            noise = np.random.default_rng(3).standard_normal(n)
            env = np.linspace(0, 1, n) ** 2
            x = (0.35 * sweep + 0.18 * noise * env) * env * 0.7
            sf.write(str(FX_DIR / "donjon_fx_riser.wav"), np.stack([x, x], 1), sr)
        if not (FX_DIR / "donjon_fx_impact.wav").exists():
            n = int(sr * 2.2)
            t = np.arange(n) / sr
            f = np.linspace(120, 38, n)
            env = np.exp(-t * 2.4)
            x = (0.9 * np.sin(2 * np.pi * f * t) +
                 0.15 * np.random.default_rng(4).standard_normal(n) * np.exp(-t * 9)) * env
            sf.write(str(FX_DIR / "donjon_fx_impact.wav"), np.stack([x, x], 1), sr)
        if not (FX_DIR / "donjon_fx_clap.wav").exists():
            n = int(sr * 1.2)
            t = np.arange(n) / sr
            rng = np.random.default_rng(5)
            x = np.zeros(n)
            for at in (0.0, 0.5):
                i = int(at * sr)
                L = int(0.09 * sr)
                x[i:i + L] += rng.standard_normal(L) * np.exp(-np.linspace(0, 18, L)) * 0.5
            sf.write(str(FX_DIR / "donjon_fx_clap.wav"), np.stack([x, x], 1), sr)
        if not (FX_DIR / "donjon_fx_horn.wav").exists():
            n = int(sr * 1.4)
            t = np.arange(n) / sr
            env = np.minimum(1, t * 40) * np.exp(-np.maximum(0, t - 0.9) * 8)
            x = sum(0.22 * np.sin(2 * np.pi * fr * t) for fr in (392, 494, 587))
            sf.write(str(FX_DIR / "donjon_fx_horn.wav"), np.stack([x * env, x * env], 1), sr)

    # ------------------------------------------------------------------ startup
    def start_async(self) -> None:
        if self._start_thread and self._start_thread.is_alive():
            return
        self._start_thread = threading.Thread(target=self.start, daemon=True,
                                              name="mixxx-start")
        self._start_thread.start()

    def start(self) -> None:
        if self.ready:
            return
        self.error = ""
        try:
            self.ensure_profile()
            self.start_xvfb()
            self.start_mixxx()
            self.wait_for_window()
            self.ready = True
            # Defensive: a previous Mixxx session (or a lost key) must never
            # leave the hidden mixer at zero gain.  Set both channel volumes
            # and the master gain to unity before the first song.
            self.restore_levels()
            self.log("Mixxx DJ engine online", "success")
            threading.Thread(target=self._register_all, daemon=True,
                             name="mixxx-register").start()
            # Let Mixxx finish its first library scan before we start reading
            # its database.  With WAL this is not strictly required, but it
            # keeps the very first scan fast on slow disks.
            time.sleep(8)
            self.library.start_analysis_sync()
            self._load_samplers()
            self._watch_thread = threading.Thread(target=self._watch, daemon=True,
                                                  name="mixxx-watch")
            self._watch_thread.start()
        except Exception as e:
            self.error = str(e)
            self.ready = False
            self.log(f"Mixxx failed to start: {e}", "error")

    def _free_display(self) -> str:
        for n in range(99, 121):
            if not Path(f"/tmp/.X{n}-lock").exists():
                return f":{n}"
        return ":119"

    def start_xvfb(self) -> None:
        self.display = self._free_display()
        try:
            self._xvfb = subprocess.Popen(
                ["Xvfb", self.display, "-screen", "0", "1600x1000x24",
                 "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise RuntimeError("Xvfb is required (install xorg-server-xvfb)") from e
        time.sleep(1.2)
        if self._xvfb.poll() is not None:
            raise RuntimeError("Xvfb did not start")

    def start_mixxx(self) -> None:
        # A crashed previous run can leave Mixxx holding the profile DB and
        # the audio device.  Only target our isolated profile.
        try:
            subprocess.run(["pkill", "-f", f"mixxx --settings-path {PROFILE}"],
                           capture_output=True, timeout=5)
            time.sleep(0.4)
        except Exception:
            pass
        env = os.environ.copy()
        env.update({
            "DISPLAY": self.display,
            "QT_QPA_PLATFORM": "xcb",
            "PULSE_SINK": AUDIO_SINK,
            "PULSE_LATENCY_MSEC": "250",
        })
        stub = PROFILE / "hid_stub.so"
        if stub.exists():
            env["LD_PRELOAD"] = str(stub)
        log = open(MIXXX_LOG, "ab", buffering=0)
        self._proc = subprocess.Popen(
            ["mixxx", "--settings-path", str(PROFILE), "--safe-mode",
             "--log-level", "warning"],
            stdout=log, stderr=log, env=env,
        )
        self.log(f"Mixxx starting on {self.display}")

    def _xdo(self, *args: str, timeout: float = 8) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        return subprocess.run(["xdotool", *args], env=env, capture_output=True,
                              text=True, timeout=timeout)

    def _window_ids(self, name: str = "Mixxx$", main: bool = False) -> list[str]:
        """Find Mixxx windows even after the title becomes "Track | Mixxx"."""
        ids: list[str] = []
        try:
            r = self._xdo("search", "--onlyvisible", "--name", name)
            ids = [x for x in r.stdout.split() if x]
            if not ids:
                r = self._xdo("search", "--all", "--name", name)
                ids = [x for x in r.stdout.split() if x]
        except Exception:
            return []
        if not main:
            return ids
        # There are several Mixxx-owned windows (selection owner, scanner
        # dialog).  The main window is by far the largest one.
        best, best_area = None, 0
        for wid in ids:
            try:
                geo = self._xdo("getwindowgeometry", "--shell", wid).stdout
                wm = re.search(r"WIDTH=(\d+)", geo)
                hm = re.search(r"HEIGHT=(\d+)", geo)
                area = (int(wm.group(1)) if wm else 0) * (int(hm.group(1)) if hm else 0)
                if area > best_area:
                    best, best_area = wid, area
            except Exception:
                continue
        return [best] if best else ids

    def wait_for_window(self, timeout: float = 45) -> None:
        deadline = time.time() + timeout
        win = None
        while time.time() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise RuntimeError(f"Mixxx exited during startup (see {MIXXX_LOG})")
            ids = self._window_ids(main=True)
            if ids:
                win = ids[-1]
                break
            time.sleep(0.5)
        if not win:
            raise RuntimeError("Mixxx window did not appear")
        self._win_id = win
        # Pin the window so the mixer and library panes always land at the
        # coordinates the automation clicks.
        try:
            self._xdo("windowsize", win, "1280", "668")
            self._xdo("windowmove", win, "0", "0")
        except Exception:
            pass
        # The one-time library scanner progress window can steal focus.
        scan_deadline = time.time() + 25
        while time.time() < scan_deadline:
            if not self._window_ids("Library Scanner"):
                break
            time.sleep(0.5)
        self.focus()

    def focus(self) -> None:
        ids = self._window_ids(main=True)
        if ids:
            self._win_id = ids[-1]
            try:
                self._xdo("windowfocus", self._win_id)
            except Exception:
                pass

    def _key(self, seq: str) -> None:
        with self._lock:
            self.focus()
            try:
                self._xdo("key", "--clearmodifiers", seq)
            except Exception as e:
                self.log(f"key {seq} failed: {e}", "warn")
            time.sleep(0.008)

    def _type(self, text: str) -> None:
        with self._lock:
            self.focus()
            try:
                self._xdo("type", "--clearmodifiers", "--delay", "1", text,
                          timeout=15)
            except Exception as e:
                self.log(f"type failed: {e}", "warn")
            time.sleep(0.02)

    def _click(self, x: int, y: int) -> None:
        with self._lock:
            self.focus()
            self._xdo("mousemove", "--sync", str(x), str(y))
            self._xdo("click", "1")
            time.sleep(0.1)

    def _library_search(self, query: str) -> None:
        """Search the library with real mouse focus.

        Keyboard-only focus in Xvfb is unreliable (Qt may not consider the
        window active), so clicking the box is the dependable path.
        """
        self._click(60, 565)
        self._key("ctrl+a")
        self._key("BackSpace")
        self._type(query)
        time.sleep(0.55)

    def _select_first_result(self) -> None:
        self._click(400, 525)

    def _deck_has_waveform(self, deck: int) -> bool:
        """Cheap visual check that Mixxx actually loaded a track."""
        path = f"/tmp/donjon_deck{deck}.png"
        x = 72 if deck == 1 else 766
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        try:
            subprocess.run(["import", "-window", "root", path], env=env,
                           capture_output=True, timeout=6)
            r = subprocess.run(
                ["convert", path, "-crop", f"210x64+{x}+207",
                 "-colorspace", "Gray", "-format", "%[fx:mean]", "info:"],
                capture_output=True, text=True, timeout=6)
            return float((r.stdout or "0").strip() or 0) > 0.17
        except Exception:
            # Never fail a mix because the monitor check itself broke.
            return True

    def _mixxx_has_file(self, name: str) -> bool:
        try:
            conn = sqlite3.connect(f"file:{MIXXX_DB}?mode=ro", uri=True, timeout=1)
            row = conn.execute(
                "SELECT 1 FROM track_locations WHERE filename LIKE ? LIMIT 1",
                (f"%{name}%",),
            ).fetchone()
            conn.close()
            return bool(row)
        except sqlite3.Error:
            return False

    def register_track(self, track: dict) -> bool:
        """Make a DonJon track visible to the running Mixxx library.

        Mixxx does not reliably hot-add files that arrive after startup, so a
        new download/import is written into Mixxx's own tables.  The DB is in
        WAL mode, which lets the running app keep scanning while we write.
        """
        token = track.get("token") or ""
        path = track.get("path") or ""
        if not token or not path or self._mixxx_has_file(token):
            return True
        p = Path(path)
        if not p.exists():
            return False
        for attempt in range(4):
            try:
                conn = sqlite3.connect(MIXXX_DB, timeout=4)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """INSERT OR IGNORE INTO track_locations
                       (location, filename, directory, filesize, fs_deleted, needs_verification)
                       VALUES (?,?,?,?,0,0)""",
                    (str(p), p.name, str(p.parent), p.stat().st_size),
                )
                row = conn.execute("SELECT id FROM track_locations WHERE location=?",
                                   (str(p),)).fetchone()
                if row:
                    loc_id = row[0]
                    conn.execute(
                        """INSERT OR IGNORE INTO library
                           (artist, title, album, location, duration, bpm, key,
                            datetime_added, mixxx_deleted, played, header_parsed, filetype)
                           VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP,0,0,1,?)""",
                        (track.get("artist") or "", track.get("title") or "",
                         track.get("album") or "", loc_id,
                         float(track.get("duration") or 0),
                         float(track.get("bpm") or 0),
                         track.get("musical_key") or track.get("camelot") or "",
                         (p.suffix or ".mp3").lstrip(".").lower()),
                    )
                conn.commit()
                conn.close()
                return True
            except sqlite3.Error:
                try:
                    conn.close()
                except Exception:
                    pass
                time.sleep(0.15)
        self.log(f"Could not register {token} in Mixxx DB", "warn")
        return False

    def update_track_metadata(self, track: dict) -> None:
        try:
            conn = sqlite3.connect(MIXXX_DB, timeout=4)
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute("SELECT id FROM track_locations WHERE location=?",
                               (track.get("path", ""),)).fetchone()
            if row:
                conn.execute(
                    """UPDATE library SET artist=?, title=?, album=?, bpm=?, key=?
                       WHERE location=?""",
                    (track.get("artist") or "", track.get("title") or "",
                     track.get("album") or "", float(track.get("bpm") or 0),
                     track.get("musical_key") or track.get("camelot") or "",
                     row[0]),
                )
                conn.commit()
            conn.close()
        except sqlite3.Error:
            self.log("Could not update Mixxx metadata", "warn")

    def remove_track(self, track: dict) -> None:
        try:
            conn = sqlite3.connect(MIXXX_DB, timeout=4)
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute("SELECT id FROM track_locations WHERE location=?",
                               (track.get("path", ""),)).fetchone()
            if row:
                conn.execute("DELETE FROM library WHERE location=?", (row[0],))
                conn.execute("DELETE FROM track_locations WHERE id=?", (row[0],))
                conn.commit()
            conn.close()
        except sqlite3.Error:
            self.log("Could not remove track from Mixxx", "warn")

    def _register_all(self) -> None:
        for tr in self.db.all_tracks():
            if not self._mixxx_has_file(tr.get("token", "")):
                self.register_track(tr)

    def _load_samplers(self) -> None:
        """Load the riser and impact overlays used by the AI transitions."""
        names = ("riser", "impact")
        deadline = time.time() + 35
        while time.time() < deadline:
            if all(self._mixxx_has_file(f"donjon_fx_{n}") for n in names):
                break
            time.sleep(1.5)
        with self._lock:
            for i, name in enumerate(names, start=1):
                if not self._mixxx_has_file(f"donjon_fx_{name}"):
                    self.log(f"Sampler {i} ({name}) was not indexed in time", "warn")
                    continue
                self._library_search(f"location:donjon_fx_{name}")
                self._select_first_result()
                self._key(KEYMAP[f"sampler{i}_load"])
                time.sleep(0.25)
        self.log("Sampler overlays loaded", "success")

    def _watch(self) -> None:
        silent_since: dict[int, float] = {}
        while not self._stop.wait(3):
            if self._proc and self._proc.poll() is not None:
                self.ready = False
                self.error = f"Mixxx stopped (exit {self._proc.returncode})"
                self.log(self.error, "error")
                return
            # Recover from a dead output path (wrong fader/crossfader) or an
            # unconfirmed load.  Twelve seconds of digital silence on a
            # playing deck is never a musical breakdown.
            if self.audio and self.audio.ready and self.audio.rms < 0.0008:
                for n, d in self.decks.items():
                    if not d.playing or d.position() < 8:
                        silent_since.pop(n, None)
                        continue
                    since = silent_since.setdefault(n, time.time())
                    if time.time() - since < 12:
                        continue
                    silent_since[n] = time.time()
                    if self._deck_has_waveform(n):
                        self.restore_levels()
                        self.set_crossfader(0.0 if n == 1 else 1.0, 0)
                        self.log(f"Deck {'AB'[n-1]} silent; reset levels", "warn")
                    elif d.track:
                        self.log(f"Deck {'AB'[n-1]} empty; reloading", "warn")
                        self.load(n, d.track, skip_beats=0)
                        self.play(n)
                        self.restore_levels()
            else:
                silent_since.clear()

    # ------------------------------------------------------------------ transport
    def _deck_key(self, deck: int, action: str) -> None:
        self._key(KEYMAP[f"d{deck}_{action}"])

    def restore_levels(self) -> None:
        for key in ("d1_vol_one", "d2_vol_one", "master_gain_one"):
            self._key(KEYMAP[key])

    def load(self, deck: int, track: dict, skip_beats: int = 16) -> None:
        d = self.decks[deck]
        with self._lock:
            # New downloads may not be hot-added by Mixxx's watcher.  Register
            # them in Mixxx's library tables before searching.
            self.register_track(track)
            deadline = time.time() + 6
            while time.time() < deadline and not self._mixxx_has_file(track["token"]):
                time.sleep(0.5)
            # Stop the deck unconditionally.  Our state can be a fraction of a
            # second behind Mixxx, and Mixxx refuses to load over a playing
            # deck (playing-track protection), so never trust the cached flag.
            had_track = bool(d.track)
            self._deck_key(deck, "stop")
            d.playing = False
            d.base_pos = 0.0
            if self._first_load:
                # Mixxx's library view needs a moment after boot before its
                # search results settle.
                time.sleep(2.0)
                self._first_load = False
            if had_track:
                # Ejecting leaves an empty waveform, so the check below proves
                # that *this* load (not the previous track) succeeded.
                self._deck_key(deck, "eject")
                time.sleep(0.2)
            # Search by the unique token in the file *location* and click the
            # result so LoadSelectedTrack has a selection to load.
            loaded = False
            for attempt in range(5):
                self._library_search(f"location:{track['token']}")
                self._select_first_result()
                self._deck_key(deck, "load")
                time.sleep(0.55)
                if self._deck_has_waveform(deck):
                    loaded = True
                    break
                # The very first load can race Mixxx's library scan.  A
                # double-click is the GUI's own load path and is a good
                # fallback when the keyboard mapping has not been applied yet.
                self._xdo("click", "--repeat", "2", "--delay", "80", "1")
                time.sleep(0.9)
                if self._deck_has_waveform(deck):
                    loaded = True
                    break
                time.sleep(1.2)
            if not loaded:
                self.log(f"Deck {'AB'[deck-1]} did not confirm the load; "
                         "continuing with the next attempt", "warn")
            d.track = track
            d.duration = float(track.get("duration") or 0)
            d.bpm = float(track.get("bpm") or 120)
            d.base_pos = 0.0
            d.playing = False
            if skip_beats > 0 and d.bpm > 0:
                max_beats = max(0, int(d.duration * d.bpm / 60 * 0.85))
                beats = min(int(skip_beats), max_beats)
                while beats >= 16:
                    self._deck_key(deck, "jump16f")
                    beats -= 16
                if beats >= 8:
                    self._deck_key(deck, "jump8f")
                    beats -= 8
                if beats >= 4:
                    self._deck_key(deck, "jump4f")
                    beats -= 4
                applied = min(int(skip_beats), max_beats) - beats
                d.base_pos = min(d.duration * 0.85, applied * 60.0 / d.bpm)
            self.log(f"Deck {'AB'[deck-1]} loaded: {track.get('artist','')} - {track.get('title','')}")

    def play(self, deck: int) -> None:
        d = self.decks[deck]
        if not d.track:
            return
        if d.playing:
            self._deck_key(deck, "play")
            d.stop_clock()
        else:
            self._deck_key(deck, "play")
            d.start_clock()

    def pause(self, deck: int) -> None:
        d = self.decks[deck]
        if not d.playing:
            return
        # If the track already ran out, Mixxx has stopped it.  Sending play here
        # would restart the track from the top, so just fix our clock.
        if d.duration and d.position() >= d.duration - 0.4:
            d.playing = False
            d.base_pos = d.duration
            return
        self._deck_key(deck, "play")
        d.stop_clock()

    def stop(self, deck: int) -> None:
        self._deck_key(deck, "stop")
        d = self.decks[deck]
        d.base_pos = 0.0
        d.playing = False

    def sync(self, deck: int) -> None:
        self._deck_key(deck, "sync")

    def cue(self, deck: int) -> None:
        self._deck_key(deck, "cue")

    def beatjump(self, deck: int, forward: bool = True) -> None:
        self._deck_key(deck, "jump16f" if forward else "jump16b")
        d = self.decks[deck]
        delta = 16 * 60.0 / max(1.0, d.bpm)
        d.base_pos = max(0.0, d.base_pos + (delta if forward else -delta))

    # --------------------------------------------------------------------- mixer
    def set_crossfader(self, target: float, duration: float = 0.0) -> None:
        target = min(1.0, max(0.0, target))
        with self._lock:
            if duration <= 0:
                if target <= 0.01:
                    self._key(KEYMAP["xfader_a"])
                elif target >= 0.99:
                    self._key(KEYMAP["xfader_b"])
                else:
                    self._key(KEYMAP["xfader_center"])
                self.xfader = target
                return
            # 100 small steps across the requested duration.
            steps = max(1, int(abs(target - self.xfader) / 0.01))
            interval = duration / max(1, steps)
            key = KEYMAP["xfader_up"] if target > self.xfader else KEYMAP["xfader_down"]
            for _ in range(steps):
                if self._stop.is_set():
                    break
                self._key(key)
                time.sleep(max(0.015, interval - 0.01))
            self.xfader = target

    def eq(self, deck: int, band: str, off: bool) -> None:
        self._deck_key(deck, f"{band}_{'kill' if off else 'on'}")

    def filter(self, deck: int, on: bool) -> None:
        self._deck_key(deck, "filter_zero" if on else "filter_on")

    def loop(self, deck: int, roll: bool = False) -> None:
        self._deck_key(deck, "looproll8" if roll else "loop4")

    def sampler(self, n: int) -> None:
        self._key(KEYMAP[f"sampler{max(1, min(2, n))}"])

    def transition(self, out_deck: int, in_deck: int, bars: int = 8,
                   style: str = "bass_swap", bpm: float = 128.0,
                   progress=None) -> None:
        """A real bass-swap mix: sync, EQ swap, filter sweep, sampler overlay."""
        beat = 60.0 / max(60.0, bpm or 128.0)
        bar = beat * 4
        total = bars * bar
        start = time.monotonic()

        def at(fraction: float) -> float:
            return start + total * fraction

        def wait_until(t: float) -> None:
            while time.monotonic() < t and not self._stop.is_set():
                time.sleep(0.02)

        def report(p: float, msg: str) -> None:
            if progress:
                try:
                    progress(p, msg)
                except Exception:
                    pass

        # Incoming deck: synced, low end held back.
        self.sync(in_deck)
        self.eq(in_deck, "low", True)
        self.eq(in_deck, "mid", False)
        self.eq(in_deck, "high", False)
        if not self.decks[in_deck].playing:
            self.play(in_deck)
        report(0.02, "incoming deck synced, bass held back")

        # Phase 1: bring the new track in with the crossfader while the bass is
        # still on the outgoing track.  This is the part that makes it a mix
        # rather than a fade.
        half = total / 2
        steps = 50
        for i in range(steps):
            self._key(KEYMAP["xfader_up"] if in_deck == 2 else KEYMAP["xfader_down"])
            self.xfader = min(1.0, max(0.0, self.xfader + (0.01 if in_deck == 2 else -0.01)))
            wait_until(start + half * (i + 1) / steps)
            if i == 8:
                report(0.2, "crossfader moving, outgoing bass intact")
                self.sampler(1)          # riser
            if i == 18:
                self.loop(out_deck, roll=False)
                report(0.35, "4-beat loop fill on outgoing deck")
        report(0.5, "halfway: bass swap")
        # The drop: swap low end.
        self.eq(out_deck, "low", True)
        self.eq(in_deck, "low", False)
        self.sampler(2)                  # impact
        # Phase 2: finish the crossfade and sweep the old track out.
        for i in range(steps):
            self._key(KEYMAP["xfader_up"] if in_deck == 2 else KEYMAP["xfader_down"])
            self.xfader = min(1.0, max(0.0, self.xfader + (0.01 if in_deck == 2 else -0.01)))
            if i == 10:
                self.filter(out_deck, on=True)
                report(0.7, "filter sweep on outgoing deck")
            wait_until(start + half + half * (i + 1) / steps)
        # Clean finish.
        self.set_crossfader(1.0 if in_deck == 2 else 0.0, 0)
        self.eq(out_deck, "low", False)
        self.eq(out_deck, "mid", False)
        self.eq(out_deck, "high", False)
        self.filter(out_deck, on=False)
        self.eq(in_deck, "low", False)
        self.eq(in_deck, "mid", False)
        self.eq(in_deck, "high", False)
        self.loop(out_deck, roll=False)   # clear the 4-beat fill
        self.stop(out_deck)               # unconditional [ChannelN] stop
        self.restore_levels()
        self.current_deck = in_deck
        report(1.0, "mix complete")

    # --------------------------------------------------------------------- state
    def state(self) -> dict:
        decks = {}
        for n, d in self.decks.items():
            tr = d.track or {}
            decks[str(n)] = {
                "track": {
                    "id": tr.get("id"), "title": tr.get("title", ""),
                    "artist": tr.get("artist", ""), "duration": d.duration,
                    "bpm": d.bpm, "camelot": tr.get("camelot", ""),
                    "artwork": tr.get("artwork", ""),
                } if d.track else None,
                "playing": d.playing,
                "position": round(d.position(), 2),
                "duration": round(d.duration, 2),
            }
        return {
            "ready": self.ready,
            "error": self.error,
            "display": self.display,
            "current_deck": self.current_deck,
            "crossfader": round(self.xfader, 3),
            "decks": decks,
            "events": self.events[-25:],
        }

    def shutdown(self) -> None:
        self._stop.set()
        self.library.stop_analysis_sync()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=8)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._xvfb and self._xvfb.poll() is None:
            try:
                self._xvfb.terminate()
            except Exception:
                pass
