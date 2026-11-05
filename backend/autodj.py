"""DonJon's brain: the shared room state, queue, and the automatic DJ."""
from __future__ import annotations

import asyncio
import random
import threading
import time
import uuid
from typing import Any, Callable

from .db import Database
from .library import Library, camelot_for

VIBES = {
    "warmup": {"bpm": (100, 124), "energy": 0.35, "label": "Warm-up"},
    "peak": {"bpm": (120, 132), "energy": 0.72, "label": "Peak time"},
    "chaos": {"bpm": (124, 142), "energy": 0.92, "label": "Chaos"},
}


def _num(c: str) -> int:
    try:
        return int("".join(ch for ch in c if ch.isdigit()) or 0)
    except ValueError:
        return 0


def _letter(c: str) -> str:
    return c[-1].upper() if c else ""


def harmonic_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.35
    if a == b:
        return 1.0
    an, bn = _num(a), _num(b)
    al, bl = _letter(a), _letter(b)
    diff = min(abs(an - bn), 12 - abs(an - bn))
    if diff == 0:
        return 0.85
    if diff == 1 and al == bl:
        return 0.9
    if diff == 1:
        return 0.65
    if diff == 2 and al == bl:
        return 0.55
    return 0.15


class AppState:
    def __init__(self, room_code: str):
        self.lock = threading.RLock()
        self.room_code = room_code
        # DonJon is always an AI DJ now.  There is no manual-deck mode.
        self.mode = "dj"
        self.autodj = True
        self.vibe = "peak"
        self.queue: list[dict] = []
        self.history: list[int] = []
        self.members: dict[str, dict] = {}
        self.host_id: str | None = None
        self.host_lock = False
        self.last_message = ""
        self.jobs: dict[str, dict] = {}
        self.revision = 0
        self.started_at = time.time()

    def touch(self) -> None:
        self.revision += 1

    def add_member(self, cid: str, name: str, host: bool = False) -> None:
        with self.lock:
            self.members[cid] = {
                "id": cid, "name": name or "Guest",
                "host": host, "joined_at": time.time(),
            }
            if host and not self.host_id:
                self.host_id = cid
            self.touch()

    def remove_member(self, cid: str) -> None:
        with self.lock:
            self.members.pop(cid, None)
            if self.host_id == cid:
                self.host_id = next((k for k, v in self.members.items() if v["host"]), None)
            self.touch()

    def add_queue(self, track: dict, added_by: str, priority: bool = False) -> dict:
        with self.lock:
            item = {
                "uid": uuid.uuid4().hex[:10],
                "track": track,
                "added_by": added_by or "Guest",
                "votes": 0,
                "voters": [],
                "priority": bool(priority),
                "added_at": time.time(),
            }
            if priority:
                self.queue.insert(0, item)
            else:
                self.queue.append(item)
            self.touch()
            return item

    def move_queue(self, uid: str, delta: int) -> bool:
        with self.lock:
            for i, q in enumerate(self.queue):
                if q["uid"] == uid:
                    j = max(0, min(len(self.queue) - 1, i + int(delta)))
                    if i != j:
                        self.queue.insert(j, self.queue.pop(i))
                        self.touch()
                    return True
        return False

    def play_next(self, uid: str) -> bool:
        with self.lock:
            for i, q in enumerate(self.queue):
                if q["uid"] == uid:
                    q["priority"] = True
                    self.queue.insert(0, self.queue.pop(i))
                    self.touch()
                    return True
        return False

    def remove_queue_track(self, track_id: int) -> None:
        with self.lock:
            self.queue = [q for q in self.queue if q["track"].get("id") != track_id]
            self.touch()

    def refresh_track(self, track: dict) -> None:
        with self.lock:
            for q in self.queue:
                if q["track"].get("id") == track.get("id"):
                    q["track"] = track
            self.touch()

    def remove_queue(self, uid: str) -> bool:
        with self.lock:
            before = len(self.queue)
            self.queue = [q for q in self.queue if q["uid"] != uid]
            self.touch()
            return len(self.queue) != before

    def vote(self, uid: str, cid: str) -> int:
        with self.lock:
            for q in self.queue:
                if q["uid"] == uid:
                    if cid not in q["voters"]:
                        q["voters"].append(cid)
                        q["votes"] = len(q["voters"])
                    self.touch()
                    return q["votes"]
        return 0

    def pop_queue(self) -> dict | None:
        with self.lock:
            if not self.queue:
                return None
            # Queued "play next" items are at the front; otherwise manual
            # queue order wins over the vote count.
            best = self.queue[0]
            self.queue.remove(best)
            self.touch()
            return best

    def queue_snapshot(self) -> list[dict]:
        with self.lock:
            return [{
                "uid": q["uid"],
                "track": q["track"],
                "added_by": q["added_by"],
                "votes": len(q["voters"]),
                "priority": bool(q.get("priority")),
            } for q in self.queue]

    def add_job(self, kind: str, title: str, total: int = 0) -> str:
        job_id = uuid.uuid4().hex[:10]
        with self.lock:
            self.jobs[job_id] = {
                "id": job_id, "kind": kind, "title": title,
                "total": max(0, int(total)), "done": 0,
                "status": "running", "message": "", "created_at": time.time(),
            }
            self.jobs = dict(sorted(self.jobs.items(),
                                    key=lambda kv: kv[1]["created_at"])[-8:])
            self.touch()
        return job_id

    def update_job(self, job_id: str, **kw) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job.update(kw)
            job["updated_at"] = time.time()
            self.touch()

    def snapshot(self, extra: dict | None = None) -> dict:
        with self.lock:
            now = time.time()
            self.jobs = {
                k: v for k, v in self.jobs.items()
                if v.get("status") == "running" or now - float(v.get("created_at") or 0) < 120
            }
            data = {
                "room": self.room_code,
                "mode": self.mode,
                "autodj": self.autodj,
                "vibe": self.vibe,
                "queue": self.queue_snapshot(),
                "members": list(self.members.values()),
                "host_id": self.host_id,
                "host_lock": self.host_lock,
                "jobs": list(self.jobs.values()),
                "revision": self.revision,
                "message": self.last_message,
                "server_ts": time.time() * 1000,
            }
        if extra:
            data.update(extra)
        return data


class AutoDJ:
    def __init__(self, db: Database, library: Library, engine, state: AppState,
                 audio=None, on_change: Callable[[], None] | None = None):
        self.db = db
        self.library = library
        self.engine = engine
        self.state = state
        self.audio = audio
        self.on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._transition_lock = threading.Lock()
        self.transitioning = False
        self.user_paused = False

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="autodj")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _log(self, msg: str, kind: str = "info") -> None:
        self.state.last_message = msg
        self.engine.log(msg, kind)
        self._changed()

    # ------------------------------------------------------------------- queue
    def queue_add(self, track_id: int, by: str = "Guest",
                  priority: bool = False) -> dict | None:
        track = self.db.track(track_id)
        if not track:
            return None
        cur = self.engine.decks[self.engine.current_deck].track
        if cur and cur.get("id") == track_id:
            return None
        item = self.state.add_queue(track, by, priority=priority)
        self._changed()
        return item

    def queue_play_next(self, track_id: int, by: str = "Guest") -> dict | None:
        return self.queue_add(track_id, by, priority=True)

    def queue_move(self, uid: str, delta: int) -> bool:
        ok = self.state.move_queue(uid, delta)
        self._changed()
        return ok

    def queue_remove(self, uid: str) -> bool:
        ok = self.state.remove_queue(uid)
        self._changed()
        return ok

    def queue_playlist(self, playlist_id: int, by: str = "Guest",
                       priority: bool = False, replace: bool = False) -> int:
        if replace:
            with self.state.lock:
                self.state.queue.clear()
                self.state.touch()
        added = 0
        for t in self.db.playlist_tracks(playlist_id):
            if self.queue_add(t["id"], by, priority=priority):
                added += 1
        self._log(f"Queued {added} track(s) from playlist")
        self._changed()
        return added

    def edit_track(self, track_id: int, title: str, artist: str, album: str) -> dict | None:
        self.db.update_track(track_id, title, artist, album)
        track = self.db.track(track_id)
        if not track:
            return None
        self.state.refresh_track(track)
        for d in self.engine.decks.values():
            if d.track and d.track.get("id") == track_id:
                d.track = {**d.track, "title": track["title"],
                           "artist": track["artist"], "album": track["album"]}
        self.engine.update_track_metadata(track)
        self._log(f"Edited {track['title']}")
        self._changed()
        return track

    def delete_track(self, track_id: int, delete_file: bool = True) -> bool:
        track = self.db.track(track_id)
        if not track:
            return False
        for d in self.engine.decks.values():
            if d.track and d.track.get("id") == track_id:
                self.engine.stop(d.number)
                d.track = None
        self.state.remove_queue_track(track_id)
        self.engine.remove_track(track)
        self.db.delete_track(track_id)
        if delete_file:
            try:
                from pathlib import Path
                Path(track["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        self._log(f"Removed {track.get('title','track')} from the library", "warn")
        self._changed()
        return True

    def vote(self, uid: str, cid: str) -> int:
        v = self.state.vote(uid, cid)
        self._changed()
        return v

    # ------------------------------------------------------------------- modes
    def set_mode(self, mode: str) -> None:
        mode = "dj" if mode == "dj" else "normal"
        self.state.mode = mode
        self.state.autodj = mode == "dj"
        self.user_paused = False
        self.state.touch()
        self._log(f"Mode: {'Automatic AI DJ' if mode == 'dj' else 'Normal playback'}")
        if mode == "dj":
            self._ensure_playing()
        self._changed()

    def set_vibe(self, vibe: str) -> None:
        if vibe in VIBES:
            self.state.vibe = vibe
            self.state.touch()
            self._log(f"Vibe: {VIBES[vibe]['label']}")
            self._changed()

    # ------------------------------------------------------------------ choices
    def pick_next(self, exclude: set[int] | None = None) -> dict | None:
        excluded = set(exclude or set()) | set(self.state.history[-10:])
        q = self.state.pop_queue()
        if q and q["track"]["id"] not in excluded:
            return q["track"]
        if q:
            # Put it back if it was just played.
            self.state.queue.insert(0, q)
        # A simple player follows the room queue.  No random library picks.
        return None

    @staticmethod
    def _skip_for(track: dict, field: str) -> int:
        bpm = float(track.get("bpm") or 120)
        sec = float(track.get(field) or 0)
        if sec <= 0 or bpm <= 0:
            return 16
        # Align to a 4-beat phrase.
        return max(0, int(round(sec * bpm / 60.0 / 4.0)) * 4)

    def _mix_out_seconds(self, deck) -> float:
        track = deck.track or {}
        cue = float(track.get("mix_out_sec") or 0)
        if cue > 0:
            return min(deck.duration - 1.0, cue)
        bpm = float(deck.bpm or 128)
        return min(deck.duration * 0.6, 32 * 4 * 60.0 / bpm)

    # ------------------------------------------------------------------ driving
    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            try:
                if not self.engine.ready or self.transitioning or self.user_paused:
                    continue
                deck_no = self.engine.current_deck
                deck = self.engine.decks[deck_no]
                if self.state.mode == "dj":
                    if not deck.track:
                        nxt = self.pick_next()
                        if nxt:
                            self._start_first(nxt, deck_no)
                    elif not deck.playing:
                        if deck.position() >= deck.duration - 0.5:
                            nxt = self.pick_next({deck.track.get("id")})
                            if nxt:
                                self._start_first(nxt, 3 - deck_no)
                        else:
                            self.engine.play(deck_no)
                    else:
                        if deck.position() >= self._mix_out_seconds(deck):
                            self._advance()
                else:
                    # Normal mode simply keeps the queue moving with a short mix.
                    if deck.playing and deck.duration and deck.position() >= deck.duration - 1.0:
                        nxt = self.pick_next({deck.track.get("id")})
                        if nxt:
                            self._start_first(nxt, 3 - deck_no)
            except Exception as e:
                self.engine.log(f"AutoDJ loop: {e}", "warn")

    def _start_first(self, track: dict, deck_no: int | None = None) -> None:
        deck_no = deck_no or self.engine.current_deck
        if self.transitioning:
            return
        self.user_paused = False
        self.transitioning = True
        try:
            other = 3 - deck_no
            if self.engine.decks[other].playing:
                self.engine.stop(other)
            self.engine.load(deck_no, track, skip_beats=self._skip_for(track, "drop_sec"))
            self.engine.set_crossfader(0.0 if deck_no == 1 else 1.0, 0)
            self.engine.play(deck_no)
            self.engine.restore_levels()
            self.engine.current_deck = deck_no
            self.state.history.append(track["id"])
            self.state.history = self.state.history[-25:]
            self.state.touch()
            self._log(f"Now playing: {track.get('artist','')} - {track.get('title','')}", "success")
            self._changed()
        finally:
            self.transitioning = False

    def _advance(self, force: bool = False, track_id: int | None = None) -> None:
        with self._transition_lock:
            if self.transitioning:
                return
            self.user_paused = False
            out_no = self.engine.current_deck
            out = self.engine.decks[out_no]
            if not out.track:
                nxt = self.db.track(track_id) if track_id else self.pick_next()
                if nxt:
                    self._start_first(nxt, out_no)
                return
            nxt = self.db.track(track_id) if track_id else self.pick_next({out.track.get("id")})
            if not nxt:
                self._log("Queue empty, waiting for the next request", "warn")
                return
            in_no = 3 - out_no
            self.transitioning = True
            self._log(f"Mixing in: {nxt.get('artist','')} - {nxt.get('title','')}")
            self._changed()
            try:
                # Load and start the incoming deck silently behind the crossfader.
                self.engine.load(in_no, nxt, skip_beats=self._skip_for(nxt, "mix_in_sec"))
                self.engine.play(in_no)
                # Incoming starts at mix_in (8 bars before its drop); an 8-bar
                # half therefore lands the bass swap on the drop.
                bars = 16 if self.state.mode == "dj" else 4
                def progress(p, msg):
                    self.state.last_message = msg
                    self._changed()
                self.engine.transition(out_no, in_no, bars=bars,
                                       bpm=float(out.bpm or nxt.get("bpm") or 128),
                                       progress=progress)
                self.state.history.append(nxt["id"])
                self.state.history = self.state.history[-25:]
                self.state.touch()
                self._log(f"Mix complete → {nxt.get('artist','')} - {nxt.get('title','')}", "success")
            except Exception as e:
                self._log(f"Transition failed: {e}", "error")
            finally:
                self.transitioning = False
                self._changed()

    def _ensure_playing(self) -> None:
        deck = self.engine.decks[self.engine.current_deck]
        if not deck.track:
            nxt = self.pick_next()
            if nxt:
                self._start_first(nxt, self.engine.current_deck)
        elif not deck.playing:
            self.engine.play(self.engine.current_deck)

    # ------------------------------------------------------------------ actions
    def play_track(self, track_id: int, by: str = "Host") -> bool:
        track = self.db.track(track_id)
        if not track:
            return False
        self.user_paused = False
        self.state.remove_queue_track(track_id)
        if self.transitioning:
            self.state.add_queue(track, by, priority=True)
            self._changed()
            return True
        cur = self.engine.decks[self.engine.current_deck]
        if cur.track and cur.playing:
            # Quick 4-bar mix into the request.
            threading.Thread(target=self._advance, kwargs={"track_id": track_id},
                             daemon=True).start()
        else:
            self._start_first(track, self.engine.current_deck)
        return True

    def next(self) -> None:
        threading.Thread(target=self._advance, daemon=True).start()

    def restart(self) -> None:
        if self.transitioning:
            return
        deck_no = self.engine.current_deck
        track = self.engine.decks[deck_no].track
        if not track:
            return
        # Reload rather than using [Channel]start: a fresh reader is more
        # reliable after a long mix and starts at the hook again.
        self.engine.load(deck_no, track,
                         skip_beats=self._skip_for(track, "drop_sec"))
        self.engine.play(deck_no)
        self.engine.restore_levels()
        self.user_paused = False
        self._changed()

    def toggle(self) -> None:
        if self.transitioning:
            return
        deck = self.engine.decks[self.engine.current_deck]
        if deck.track:
            self.engine.play(self.engine.current_deck)
            self.user_paused = not self.engine.decks[self.engine.current_deck].playing
        else:
            self.user_paused = False
            self._ensure_playing()
        self._changed()

    def deck_action(self, deck: int, action: str) -> bool:
        if self.transitioning:
            return False
        if action in ("play", "pause"):
            self.engine.play(deck)
            self.user_paused = not self.engine.decks[deck].playing
        elif action == "sync":
            self.engine.sync(deck)
        elif action == "cue":
            self.engine.cue(deck)
        elif action == "loop":
            self.engine.loop(deck)
        else:
            return False
        self._changed()
        return True

    def sampler(self, n: int) -> None:
        self.engine.sampler(n)
        self._log(f"Overlay {n} fired")
        self._changed()

    def state_extra(self) -> dict:
        return {
            "engine": self.engine.state(),
            "audio": self.audio.state() if self.audio else {},
            "vibes": VIBES,
        }
