"""Thread-safe SQLite store for the DonJon library and playlists."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable

from .config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    token TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL DEFAULT 0,
    bpm REAL NOT NULL DEFAULT 0,
    musical_key TEXT NOT NULL DEFAULT '',
    camelot TEXT NOT NULL DEFAULT '',
    energy REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT 'local',
    artwork TEXT NOT NULL DEFAULT '',
    analyzed INTEGER NOT NULL DEFAULT 0,
    intro_sec REAL NOT NULL DEFAULT 0,
    drop_sec REAL NOT NULL DEFAULT 0,
    outro_sec REAL NOT NULL DEFAULT 0,
    mix_in_sec REAL NOT NULL DEFAULT 0,
    mix_out_sec REAL NOT NULL DEFAULT 0,
    sections INTEGER NOT NULL DEFAULT 0,
    added_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_added ON tracks(added_at DESC);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, track_id)
);
"""


class Database:
    def __init__(self, path=DB_PATH):
        self.path = str(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.lock:
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tracks)")}
            for name, decl in (
                ("analyzed", "INTEGER NOT NULL DEFAULT 0"),
                ("intro_sec", "REAL NOT NULL DEFAULT 0"),
                ("drop_sec", "REAL NOT NULL DEFAULT 0"),
                ("outro_sec", "REAL NOT NULL DEFAULT 0"),
                ("mix_in_sec", "REAL NOT NULL DEFAULT 0"),
                ("mix_out_sec", "REAL NOT NULL DEFAULT 0"),
                ("sections", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE tracks ADD COLUMN {name} {decl}")
            self.conn.commit()

    @contextmanager
    def tx(self):
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ----------------------------- tracks ---------------------------------
    def add_track(self, path: str, token: str, title: str, artist: str = "",
                  album: str = "", duration: float = 0, bpm: float = 0,
                  musical_key: str = "", camelot: str = "", energy: float = 0.5,
                  source: str = "local", artwork: str = "",
                  analyzed: bool = False) -> dict:
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO tracks
                   (path, token, title, artist, album, duration, bpm, musical_key,
                    camelot, energy, source, artwork, analyzed, added_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(path), token, title, artist, album, duration, bpm,
                 musical_key, camelot, energy, source, artwork,
                 1 if analyzed else 0, time.time()),
            )
            if cur.rowcount == 0:
                return self.track_by_path(path) or {}
            return self.track(cur.lastrowid)

    def track(self, track_id: int) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        return dict(row) if row else None

    def track_by_path(self, path: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM tracks WHERE path=?", (str(path),)).fetchone()
        return dict(row) if row else None

    def track_by_token(self, token: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM tracks WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None

    def all_tracks(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM tracks ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]

    def list_tracks(self, query: str = "", limit: int = 300) -> list[dict]:
        q = (query or "").strip()
        with self.lock:
            if q:
                like = f"%{q}%"
                rows = self.conn.execute(
                    """SELECT * FROM tracks
                       WHERE title LIKE ? OR artist LIKE ? OR album LIKE ? OR token LIKE ?
                       ORDER BY added_at DESC LIMIT ?""",
                    (like, like, like, like, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM tracks ORDER BY added_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def update_analysis(self, token: str, bpm: float = 0, musical_key: str = "",
                        camelot: str = "", duration: float = 0,
                        energy: float = 0, analyzed: bool = True,
                        intro_sec: float = 0, drop_sec: float = 0,
                        outro_sec: float = 0, mix_in_sec: float = 0,
                        mix_out_sec: float = 0, sections: bool = False) -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE tracks SET
                   bpm=CASE WHEN ?>0 THEN ? ELSE bpm END,
                   musical_key=CASE WHEN ?<>'' THEN ? ELSE musical_key END,
                   camelot=CASE WHEN ?<>'' THEN ? ELSE camelot END,
                   duration=CASE WHEN ?>0 THEN ? ELSE duration END,
                   energy=CASE WHEN ?>0 THEN ? ELSE energy END,
                   analyzed=CASE WHEN ? THEN 1 ELSE analyzed END,
                   intro_sec=CASE WHEN ?>0 THEN ? ELSE intro_sec END,
                   drop_sec=CASE WHEN ?>0 THEN ? ELSE drop_sec END,
                   outro_sec=CASE WHEN ?>0 THEN ? ELSE outro_sec END,
                   mix_in_sec=CASE WHEN ?>0 THEN ? ELSE mix_in_sec END,
                   mix_out_sec=CASE WHEN ?>0 THEN ? ELSE mix_out_sec END,
                   sections=CASE WHEN ? THEN 1 ELSE sections END
                   WHERE token=?""",
                (bpm, bpm, musical_key, musical_key, camelot, camelot,
                 duration, duration, energy, energy, 1 if analyzed else 0,
                 intro_sec, intro_sec, drop_sec, drop_sec, outro_sec, outro_sec,
                 mix_in_sec, mix_in_sec, mix_out_sec, mix_out_sec,
                 1 if sections else 0, token),
            )

    def set_artwork(self, token: str, artwork: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE tracks SET artwork=? WHERE token=?", (artwork, token))

    def mark_analyzed(self, token: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE tracks SET analyzed=1 WHERE token=?", (token,))

    def pending_analysis(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM tracks WHERE analyzed=0 ORDER BY added_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def analysis_candidates(self, limit: int = 8) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM tracks WHERE analyzed=0 OR sections=0 "
                "ORDER BY added_at ASC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_track(self, track_id: int, title: str | None = None,
                     artist: str | None = None, album: str | None = None) -> None:
        with self.tx() as c:
            if title is not None:
                c.execute("UPDATE tracks SET title=? WHERE id=?", (title.strip(), track_id))
            if artist is not None:
                c.execute("UPDATE tracks SET artist=? WHERE id=?", (artist.strip(), track_id))
            if album is not None:
                c.execute("UPDATE tracks SET album=? WHERE id=?", (album.strip(), track_id))

    def delete_track(self, track_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM tracks WHERE id=?", (track_id,))
            c.execute("DELETE FROM playlist_tracks WHERE track_id=?", (track_id,))

    # ---------------------------- playlists -------------------------------
    def playlists(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT p.*, COUNT(pt.track_id) AS count
                   FROM playlists p LEFT JOIN playlist_tracks pt ON p.id=pt.playlist_id
                   GROUP BY p.id ORDER BY p.created_at"""
            ).fetchall()
        return [dict(r) for r in rows]

    def create_playlist(self, name: str) -> dict:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO playlists (name, created_at) VALUES (?,?)",
                      (name.strip(), time.time()))
            row = c.execute("SELECT * FROM playlists WHERE name=?", (name.strip(),)).fetchone()
        return dict(row)

    def rename_playlist(self, playlist_id: int, name: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE playlists SET name=? WHERE id=?", (name.strip(), playlist_id))

    def delete_playlist(self, playlist_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
            c.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))

    def playlist_tracks(self, playlist_id: int) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT t.* FROM tracks t JOIN playlist_tracks pt ON t.id=pt.track_id
                   WHERE pt.playlist_id=? ORDER BY pt.position""",
                (playlist_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_to_playlist(self, playlist_id: int, track_id: int) -> None:
        with self.tx() as c:
            pos = c.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM playlist_tracks WHERE playlist_id=?",
                (playlist_id,),
            ).fetchone()[0]
            c.execute("INSERT OR IGNORE INTO playlist_tracks VALUES (?,?,?)",
                      (playlist_id, track_id, pos))

    def remove_from_playlist(self, playlist_id: int, track_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?",
                      (playlist_id, track_id))
