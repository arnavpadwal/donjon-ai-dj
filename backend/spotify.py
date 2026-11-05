"""Spotify discovery bridge.

Spotify's API only provides metadata and DRM streams, so DonJon uses it for
search and playlist discovery, then resolves each track to the local library or
to a YouTube audio download so the finished mix can still run through Mixxx.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import DATA
from .library import clean_text

SETTINGS = DATA / "spotify.json"


class SpotifyError(RuntimeError):
    pass


class SpotifyNotConfigured(SpotifyError):
    pass


class SpotifyService:
    def __init__(self):
        self.lock = threading.RLock()
        self.client_id = ""
        self.client_secret = ""
        self._token = ""
        self._token_at = 0.0
        self._load()

    # ------------------------------------------------------------------ settings
    def _load(self) -> None:
        try:
            data = json.loads(SETTINGS.read_text(encoding="utf-8"))
            self.client_id = str(data.get("client_id") or "")
            self.client_secret = str(data.get("client_secret") or "")
        except Exception:
            pass
        import os
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID", self.client_id)
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", self.client_secret)

    def configure(self, client_id: str, client_secret: str) -> None:
        with self.lock:
            self.client_id = client_id.strip()
            self.client_secret = client_secret.strip()
            self._token = ""
            self._token_at = 0.0
            SETTINGS.write_text(json.dumps({
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, indent=2), encoding="utf-8")

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def clear(self) -> None:
        with self.lock:
            self.client_id = ""
            self.client_secret = ""
            self._token = ""
            self._token_at = 0.0
            try:
                SETTINGS.unlink(missing_ok=True)
            except Exception:
                pass

    def status(self) -> dict:
        hint = ""
        if self.client_id:
            hint = (self.client_id[:4] + "…" + self.client_id[-4:]
                    if len(self.client_id) > 9 else "saved")
        out = {"configured": self.configured, "ok": False, "saved": self.configured,
               "client_id_hint": hint, "error": ""}
        if not self.configured:
            out["error"] = "Add a Spotify Client ID and Secret to search Spotify."
            return out
        try:
            self._get_token()
            out["ok"] = True
        except Exception as e:
            out["error"] = str(e)
        return out

    # --------------------------------------------------------------------- token
    def _get_token(self) -> str:
        with self.lock:
            if self._token and time.time() - self._token_at < 3000:
                return self._token
            if not self.configured:
                raise SpotifyNotConfigured("Spotify is not configured")
            auth = base64.b64encode(
                f"{self.client_id}:{self.client_secret}".encode()
            ).decode()
            body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
            req = urllib.request.Request(
                "https://accounts.spotify.com/api/token", data=body,
                headers={"Authorization": f"Basic {auth}",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="ignore")[:300]
                raise SpotifyError(f"Spotify auth failed ({e.code}): {detail}") from e
            except Exception as e:
                raise SpotifyError(f"Spotify auth failed: {e}") from e
            self._token = data.get("access_token", "")
            self._token_at = time.time()
            if not self._token:
                raise SpotifyError("Spotify did not return an access token")
            return self._token

    def _get(self, path: str, params: dict | None = None) -> dict:
        token = self._get_token()
        url = f"https://api.spotify.com/v1{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")[:300]
            if e.code == 401:
                self._token = ""
            raise SpotifyError(f"Spotify request failed ({e.code}): {detail}") from e
        except Exception as e:
            raise SpotifyError(f"Spotify request failed: {e}") from e

    # ------------------------------------------------------------------- parsing
    @staticmethod
    def _track(t: dict) -> dict:
        images = ((t.get("album") or {}).get("images") or [])
        return {
            "id": t.get("id") or "",
            "title": clean_text(t.get("name") or "Untitled"),
            "artist": clean_text(", ".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name"))),
            "album": clean_text((t.get("album") or {}).get("name") or ""),
            "duration": round(float(t.get("duration_ms") or 0) / 1000, 1),
            "artwork": images[-1].get("url", "") if images else "",
            "url": (t.get("external_urls") or {}).get("spotify", ""),
            "uri": t.get("uri") or "",
            "source": "spotify",
        }

    @staticmethod
    def playlist_id(url: str) -> str:
        m = re.search(r"playlist[/:]([A-Za-z0-9]+)", url or "")
        if m:
            return m.group(1)
        m = re.search(r"^([A-Za-z0-9]{16,})$", (url or "").strip())
        return m.group(1) if m else ""

    # -------------------------------------------------------------------- public
    def search(self, query: str, limit: int = 10) -> list[dict]:
        data = self._get("/search", {"q": query, "type": "track", "limit": limit})
        return [self._track(t) for t in ((data.get("tracks") or {}).get("items") or []) if t]

    def playlist(self, url: str, limit: int = 100) -> dict:
        pid = self.playlist_id(url)
        if not pid:
            raise SpotifyError("Not a Spotify playlist link")
        meta = self._get(f"/playlists/{pid}")
        tracks: list[dict] = []
        offset = 0
        while len(tracks) < limit:
            page = self._get(f"/playlists/{pid}/tracks",
                             {"limit": min(100, limit - len(tracks)), "offset": offset})
            items = page.get("items") or []
            for item in items:
                t = (item or {}).get("track") or {}
                if t and t.get("type", "track") == "track":
                    tracks.append(self._track(t))
            if not page.get("next") or not items:
                break
            offset += len(items)
        images = (meta.get("images") or [])
        return {
            "id": pid,
            "name": meta.get("name") or "Spotify playlist",
            "owner": ((meta.get("owner") or {}).get("display_name") or ""),
            "artwork": images[-1].get("url", "") if images else "",
            "count": len(tracks),
            "tracks": tracks,
        }


def find_local(db, title: str, artist: str) -> dict | None:
    """Best-effort local match before falling back to a YouTube download."""
    words = [w for w in re.split(r"\W+", f"{artist} {title}") if len(w) > 2]
    if not words:
        return None
    hits = db.list_tracks(words[0], limit=40)
    tl = title.lower().strip()
    al = artist.lower().strip()
    scored = []
    for t in hits:
        score = 0
        if tl and tl in (t.get("title") or "").lower():
            score += 3
        if al and al in (t.get("artist") or "").lower():
            score += 2
        score += sum(1 for w in words if w.lower() in f"{t.get('title','')} {t.get('artist','')}".lower())
        if score >= 5:
            scored.append((score, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored else None
