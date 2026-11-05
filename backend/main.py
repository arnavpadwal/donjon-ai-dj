"""DonJon FastAPI server: library, room, sync stream and the AI DJ."""
from __future__ import annotations

import asyncio
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .audio import AudioStreamer
from .autodj import AppState, AutoDJ, VIBES
from .config import (
    APP_NAME, AUDIO_SINK, COVERS, FRONTEND, HOST, LIBRARY, PORT, ROOM_CODE, VERSION,
)
from .db import Database
from .library import Library
from .mixxx import MixxxEngine
from .spotify import SpotifyService, SpotifyError, find_local

db = Database()
library = Library(db)
audio = AudioStreamer()
state = AppState(ROOM_CODE)
engine = MixxxEngine(db, library, audio)
autodj = AutoDJ(db, library, engine, state, audio)
spotify = SpotifyService()


def _resolve_spotify(track: dict, job_id: str | None = None) -> dict:
    """Spotify metadata -> local file or a YouTube Music audio match."""
    local = find_local(db, track.get("title", ""), track.get("artist", ""))
    if local:
        if not local.get("artwork") and track.get("artwork"):
            from pathlib import Path
            art = library._artwork_for(Path(local["path"]), local["token"],
                                       local["title"], local.get("artist", ""),
                                       track["artwork"])
            db.set_artwork(local["token"], art)
            local["artwork"] = art
        return local
    query = f"{track.get('artist','')} {track.get('title','')}".strip()
    results = library.youtube_search(query, limit=3)
    if not results:
        raise RuntimeError(f"No match for {query}")
    # Prefer a result with a similar duration when available.
    want = float(track.get("duration") or 0)
    best = results[0]
    if want > 0:
        best = min(results, key=lambda r: abs(float(r.get("duration") or 0) - want))
    return library.youtube_download(
        best["id"], track.get("title") or best["title"],
        track.get("artist") or best["artist"], source="spotify",
        artwork=track.get("artwork", "") or best.get("artwork", ""),
        cache_artwork=False,
    )


def _start_job(kind: str, title: str, total: int, fn) -> str:
    job_id = state.add_job(kind, title, total)
    def progress(done, total, message=""):
        state.update_job(job_id, done=done, total=total, message=message)
    def worker():
        try:
            result = fn(job_id, progress)
            state.update_job(job_id, status="done", done=state.jobs[job_id].get("done", 0),
                             message="Complete")
            if isinstance(result, dict):
                state.update_job(job_id, message=result.get("message") or "Complete",
                                 playlist_id=result.get("playlist_id") or "")
        except Exception as e:
            state.update_job(job_id, status="error", message=str(e))
        hub.notify()
    threading.Thread(target=worker, daemon=True, name=f"job-{kind}").start()
    return job_id


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class RoomHub:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.clients: dict[WebSocket, dict] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def attach_loop(self) -> None:
        self.loop = asyncio.get_running_loop()
        autodj.on_change = self.notify

    def notify(self) -> None:
        """Called from AutoDJ threads."""
        if self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.broadcast_state(), self.loop)
            except Exception:
                pass

    def payload(self) -> dict:
        extra = autodj.state_extra()
        extra["network"] = {
            "ip": lan_ip(), "port": PORT,
            "url": f"http://{lan_ip()}:{PORT}",
            "room": state.room_code,
            "sink": AUDIO_SINK,
        }
        extra["version"] = VERSION
        extra["app"] = APP_NAME
        return state.snapshot(extra)

    async def broadcast_state(self) -> None:
        if not self.clients:
            return
        msg = {"type": "STATE", "state": self.payload()}
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.pop(ws, None)

    async def ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            await self.broadcast_state()

    def start_ticker(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.ticker())

    async def handle(self, ws: WebSocket) -> None:
        q = ws.query_params
        name = (q.get("name") or "Guest")[:32]
        host = (q.get("host") or "").lower() in ("1", "true", "yes")
        code = (q.get("code") or state.room_code).upper()
        if code != state.room_code:
            await ws.accept()
            await ws.send_json({"type": "ERROR", "message": "Wrong room code"})
            await ws.close()
            return
        await ws.accept()
        cid = uuid.uuid4().hex[:10]
        member = {"id": cid, "name": name or "Guest", "host": host}
        self.clients[ws] = member
        state.add_member(cid, member["name"], host)
        await ws.send_json({
            "type": "HELLO", "cid": cid, "host": host,
            "state": self.payload(),
        })
        await self.broadcast_state()
        try:
            while True:
                msg = await ws.receive_json()
                await self.on_message(ws, member, msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            self.clients.pop(ws, None)
            state.remove_member(cid)
            await self.broadcast_state()

    async def on_message(self, ws: WebSocket, member: dict, msg: dict) -> None:
        t = msg.get("type")
        if t == "PING":
            await ws.send_json({
                "type": "PONG",
                "t0": msg.get("t0"),
                "server_ts": time.time() * 1000.0,
            })
            return
        if t != "ACTION":
            return
        action = msg.get("action")
        host = bool(member.get("host"))
        # Anyone can request and vote; playback control is host/party-open.
        guest_ok = action in ("queue_add", "queue_remove", "queue_play_next",
                              "queue_priority", "queue_move", "vote", "queue_playlist")
        if state.host_lock and not host and not guest_ok:
            await ws.send_json({"type": "DENIED", "message": "Host has locked playback control"})
            return
        try:
            if action == "play_track":
                autodj.play_track(int(msg.get("track_id")), member["name"])
            elif action == "queue_add":
                autodj.queue_add(int(msg.get("track_id")), member["name"])
            elif action == "queue_remove":
                autodj.queue_remove(str(msg.get("uid")))
            elif action == "queue_play_next":
                autodj.queue_play_next(int(msg.get("track_id")), member["name"])
            elif action == "queue_priority":
                state.play_next(str(msg.get("uid")))
                hub.notify()
            elif action == "queue_move":
                autodj.queue_move(str(msg.get("uid")), int(msg.get("delta") or 1))
            elif action == "queue_playlist":
                autodj.queue_playlist(int(msg.get("playlist_id")), member["name"])
            elif action == "vote":
                autodj.vote(str(msg.get("uid")), member["id"])
            elif action == "mode":
                autodj.set_mode(str(msg.get("mode")))
            elif action == "vibe":
                autodj.set_vibe(str(msg.get("vibe")))
            elif action == "next":
                autodj.next()
            elif action == "restart":
                autodj.restart()
            elif action == "toggle":
                autodj.toggle()
            elif action == "sampler":
                autodj.sampler(int(msg.get("n") or 1))
            elif action == "eq":
                engine.eq(int(msg.get("deck") or 1), str(msg.get("band") or "low"),
                          bool(msg.get("off")))
                engine.log(f"EQ {msg.get('band')} {'cut' if msg.get('off') else 'restored'} on deck {msg.get('deck')}")
            elif action == "filter":
                engine.filter(int(msg.get("deck") or 1), bool(msg.get("on")))
                engine.log(f"Filter {'sweep' if msg.get('on') else 'reset'} on deck {msg.get('deck')}")
            elif action == "deck":
                autodj.deck_action(int(msg.get("deck") or 1), str(msg.get("deck_action")))
            elif action == "host_lock":
                if host:
                    state.host_lock = bool(msg.get("value"))
                    state.touch()
            elif action == "clear_queue":
                if host:
                    with state.lock:
                        state.queue.clear()
                        state.touch()
            else:
                return
        except Exception as e:
            engine.log(f"Action {action} failed: {e}", "warn")
        await self.broadcast_state()


hub = RoomHub()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The library is the user's catalogue; no mock songs are ever generated.
    try:
        await asyncio.to_thread(library.scan_library)
        await asyncio.to_thread(library.backfill_artwork)
    except Exception as e:
        engine.log(f"Library startup: {e}", "error")
    audio.start()
    await audio.start_pump()
    engine.start_async()
    autodj.start()
    hub.attach_loop()
    hub.start_ticker()
    yield
    autodj.stop()
    engine.shutdown()
    audio.stop()


app = FastAPI(title="DonJon", version=VERSION, lifespan=lifespan)
app.mount("/art", StaticFiles(directory=str(COVERS)), name="art")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ------------------------------------------------------------------- REST models
class ImportReq(BaseModel):
    path: str


class SearchReq(BaseModel):
    query: str


class DownloadReq(BaseModel):
    id: str
    title: str = ""
    artist: str = ""
    artwork: str = ""


class TrackReq(BaseModel):
    track_id: int


class UidReq(BaseModel):
    uid: str


class PlaylistReq(BaseModel):
    name: str


class PlaylistTrackReq(BaseModel):
    track_id: int


class ModeReq(BaseModel):
    mode: str


class VibeReq(BaseModel):
    vibe: str


class SamplerReq(BaseModel):
    n: int = 1


class DeckReq(BaseModel):
    deck: int
    action: str


class TrackEditReq(BaseModel):
    title: str = ""
    artist: str = ""
    album: str = ""


class PlaylistRenameReq(BaseModel):
    name: str


class QueueMoveReq(BaseModel):
    delta: int = 1


class SpotifyConfigReq(BaseModel):
    client_id: str
    client_secret: str


class SpotifyTrackReq(BaseModel):
    title: str
    artist: str = ""
    artwork: str = ""
    action: str = "queue"        # now | next | queue


class LinkImportReq(BaseModel):
    url: str
    limit: int = 100
    playlist_name: str = ""
    queue_after: bool = True


class SpotifyImportReq(BaseModel):
    url: str
    limit: int = 100
    queue_after: bool = True


# ------------------------------------------------------------------- REST routes
@app.get("/api/health")
def health():
    return {"ok": True, "app": APP_NAME, "version": VERSION, "time": int(time.time())}


@app.get("/api/state")
def get_state():
    return hub.payload()


@app.get("/api/network")
def network():
    ip = lan_ip()
    return {"ip": ip, "port": PORT, "url": f"http://{ip}:{PORT}",
            "room": state.room_code}


@app.get("/api/library")
def get_library(q: str = "", limit: int = 300):
    return {"tracks": db.list_tracks(q, limit), "count": len(db.all_tracks())}


@app.post("/api/library/import")
async def import_library(req: ImportReq):
    try:
        result = await asyncio.to_thread(library.import_folder, req.path)
    except FileNotFoundError:
        raise HTTPException(404, "folder not found")
    except Exception as e:
        raise HTTPException(500, str(e))
    hub.notify()
    return result


@app.post("/api/youtube/search")
async def youtube_search(req: SearchReq):
    try:
        return {"results": await asyncio.to_thread(library.youtube_search, req.query)}
    except Exception as e:
        raise HTTPException(502, f"YouTube search failed: {e}")


@app.post("/api/youtube/download")
async def youtube_download(req: DownloadReq):
    try:
        track = await asyncio.to_thread(library.youtube_download, req.id,
                                        req.title or "YouTube track",
                                        req.artist or "YouTube",
                                        artwork=req.artwork)
        hub.notify()
        return {"track": track}
    except Exception as e:
        raise HTTPException(502, f"Download failed: {e}")


@app.post("/api/youtube/track")
def youtube_track(req: DownloadReq, action: str = "queue"):
    """Download a YouTube result, then play/queue it without blocking the UI."""
    def run(job_id, progress):
        progress(0, 1, "Downloading audio…")
        track = library.youtube_download(req.id, req.title or "YouTube track",
                                         req.artist or "YouTube", artwork=req.artwork)
        progress(1, 1, "Adding to the room…")
        if action == "now":
            autodj.play_track(track["id"], req.artist or "YouTube")
        elif action == "next":
            autodj.queue_play_next(track["id"], req.artist or "YouTube")
        else:
            autodj.queue_add(track["id"], req.artist or "YouTube")
        return {"message": f"Added {track['title']}"}
    return {"job_id": _start_job("youtube", req.title or "YouTube track", 1, run)}


@app.post("/api/youtube/playlist")
async def youtube_playlist(req: LinkImportReq):
    try:
        return await asyncio.to_thread(library.youtube_playlist, req.url, min(req.limit, 500))
    except Exception as e:
        raise HTTPException(502, f"YouTube playlist failed: {e}")


@app.post("/api/youtube/playlist/import")
def youtube_playlist_import(req: LinkImportReq):
    def run(job_id, progress):
        result = library.import_youtube_playlist(
            req.url, min(req.limit, 500), progress, req.playlist_name)
        msg = f"{result['added']} tracks imported into “{result['name']}”"
        if req.queue_after and result.get("playlist_id"):
            queued = autodj.queue_playlist(result["playlist_id"], "Host", replace=True)
            msg += f" · {queued} queued"
        return {"message": msg, "playlist_id": result.get("playlist_id")}
    return {"job_id": _start_job("playlist", "YouTube Music playlist", 0, run)}


@app.get("/api/spotify/status")
def spotify_status():
    return spotify.status()


@app.post("/api/spotify/config")
def spotify_config(req: SpotifyConfigReq):
    # Save first: the user's credentials must survive a refresh even if the
    # network check fails temporarily.
    spotify.configure(req.client_id, req.client_secret)
    return spotify.status()


@app.post("/api/spotify/clear")
def spotify_clear():
    spotify.clear()
    return spotify.status()


@app.post("/api/spotify/search")
async def spotify_search(req: SearchReq):
    try:
        return {"results": await asyncio.to_thread(spotify.search, req.query, 10)}
    except SpotifyError as e:
        raise HTTPException(400, str(e))


@app.post("/api/spotify/playlist")
async def spotify_playlist(req: LinkImportReq):
    try:
        return await asyncio.to_thread(spotify.playlist, req.url, min(req.limit, 300))
    except SpotifyError as e:
        raise HTTPException(400, str(e))


@app.post("/api/spotify/track")
def spotify_track(req: SpotifyTrackReq):
    def run(job_id, progress):
        progress(0, 1, "Matching the track…")
        track = _resolve_spotify({"title": req.title, "artist": req.artist,
                                  "artwork": req.artwork})
        progress(1, 1, "Adding to the room…")
        if req.action == "now":
            autodj.play_track(track["id"], "Spotify")
        elif req.action == "next":
            autodj.queue_play_next(track["id"], "Spotify")
        else:
            autodj.queue_add(track["id"], "Spotify")
        return {"message": f"Added {track['title']}"}
    return {"job_id": _start_job("spotify", f"{req.artist} – {req.title}", 1, run)}


@app.post("/api/spotify/import")
def spotify_import(req: SpotifyImportReq):
    def run(job_id, progress):
        playlist = spotify.playlist(req.url, min(req.limit, 300))
        total = len(playlist["tracks"])
        state.update_job(job_id, total=total, message=f"Importing “{playlist['name']}”")
        dbpl = db.create_playlist(playlist["name"])
        added = errors = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_resolve_spotify, item): item
                       for item in playlist["tracks"]}
            for i, fut in enumerate(as_completed(futures), start=1):
                item = futures[fut]
                try:
                    track = fut.result()
                    db.add_to_playlist(dbpl["id"], track["id"])
                    added += 1
                except Exception:
                    errors += 1
                progress(i, total, f"{i}/{total} · {item.get('title','')[:46]}")
        msg = f"{added} tracks imported" + (f" ({errors} skipped)" if errors else "")
        if req.queue_after:
            queued = autodj.queue_playlist(dbpl["id"], "Host", replace=True)
            msg += f" · {queued} queued"
        return {"message": msg, "playlist_id": dbpl["id"]}
    return {"job_id": _start_job("spotify", "Spotify playlist", 0, run)}


@app.post("/api/play")
def play_track(req: TrackReq):
    if not autodj.play_track(req.track_id, "Host"):
        raise HTTPException(404, "track not found")
    return {"ok": True}


@app.post("/api/queue")
def add_queue(req: TrackReq):
    item = autodj.queue_add(req.track_id, "Guest")
    if not item:
        raise HTTPException(400, "track not found or already playing")
    return item


@app.delete("/api/queue/{uid}")
def remove_queue(uid: str):
    return {"ok": autodj.queue_remove(uid)}


@app.post("/api/queue/{uid}/move")
def move_queue(uid: str, req: QueueMoveReq):
    return {"ok": autodj.queue_move(uid, req.delta)}


@app.post("/api/queue/{uid}/next")
def queue_next(uid: str):
    return {"ok": state.play_next(uid)}


@app.put("/api/library/{track_id}")
def edit_track(track_id: int, req: TrackEditReq):
    track = autodj.edit_track(track_id, req.title, req.artist, req.album)
    if not track:
        raise HTTPException(404, "track not found")
    hub.notify()
    return {"track": track}


@app.delete("/api/library/{track_id}")
def delete_track(track_id: int, delete_file: bool = True):
    if not autodj.delete_track(track_id, delete_file):
        raise HTTPException(404, "track not found")
    hub.notify()
    return {"ok": True}


@app.post("/api/vote/{uid}")
def vote(uid: str, request: Request):
    cid = request.headers.get("x-donjon-cid", "web")
    return {"votes": autodj.vote(uid, cid)}


@app.post("/api/mode")
def set_mode(req: ModeReq):
    autodj.set_mode(req.mode)
    return {"ok": True}


@app.post("/api/vibe")
def set_vibe(req: VibeReq):
    autodj.set_vibe(req.vibe)
    return {"ok": True}


@app.post("/api/next")
def next_track():
    autodj.next()
    return {"ok": True}


@app.post("/api/toggle")
def toggle():
    autodj.toggle()
    return {"ok": True}


@app.post("/api/sampler")
def sampler(req: SamplerReq):
    autodj.sampler(req.n)
    return {"ok": True}


@app.post("/api/calibrate")
def calibrate():
    audio.beep()
    return {"ok": True}


@app.post("/api/deck")
def deck(req: DeckReq):
    if not autodj.deck_action(req.deck, req.action):
        raise HTTPException(400, "unknown deck action")
    return {"ok": True}


@app.get("/api/playlists")
def playlists():
    out = []
    for p in db.playlists():
        out.append({**p, "tracks": db.playlist_tracks(p["id"])})
    return {"playlists": out}


@app.post("/api/playlists")
def create_playlist(req: PlaylistReq):
    return db.create_playlist(req.name)


@app.put("/api/playlists/{pid}")
def rename_playlist(pid: int, req: PlaylistRenameReq):
    if not req.name.strip():
        raise HTTPException(400, "name required")
    db.rename_playlist(pid, req.name)
    return {"ok": True}


@app.delete("/api/playlists/{pid}")
def delete_playlist(pid: int):
    db.delete_playlist(pid)
    return {"ok": True}


@app.post("/api/playlists/{pid}/queue")
def playlist_queue(pid: int, priority: bool = False, replace: bool = False):
    return {"added": autodj.queue_playlist(pid, "Host", priority=priority,
                                           replace=replace)}


@app.post("/api/playlists/{pid}/tracks")
def playlist_add(pid: int, req: PlaylistTrackReq):
    db.add_to_playlist(pid, req.track_id)
    return {"ok": True}


@app.delete("/api/playlists/{pid}/tracks/{tid}")
def playlist_remove(pid: int, tid: int):
    db.remove_from_playlist(pid, tid)
    return {"ok": True}


# ------------------------------------------------------------------- WebSockets
@app.websocket("/ws/room/{code}")
async def ws_room(ws: WebSocket, code: str):
    await hub.handle(ws)


@app.websocket("/ws/audio/{code}")
async def ws_audio(ws: WebSocket, code: str):
    await ws.accept()
    if code.upper() != state.room_code:
        await ws.close()
        return
    client = audio.add_client(ws)

    async def sender():
        try:
            while True:
                frame = await client.queue.get()
                await ws.send_bytes(frame)
        except Exception:
            pass

    task = asyncio.create_task(sender())
    try:
        while True:
            # The browser doesn't need to send anything; this is just here to
            # notice the close promptly.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        task.cancel()
        audio.remove_client(client)


# ------------------------------------------------------------------- frontend
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/")
def index():
    p = FRONTEND / "index.html"
    if not p.exists():
        return JSONResponse({"error": "frontend not built"}, status_code=500)
    return FileResponse(p)


@app.get("/{path:path}")
def fallback(path: str):
    if path.startswith(("api/", "ws/")):
        raise HTTPException(404, "not found")
    p = FRONTEND / "index.html"
    if not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(p)
