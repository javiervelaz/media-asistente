"""API REST: genera playlists con IA y controla mpv"""
import logging

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app.auth import verify_api_key
from app.config import settings
from app.llm import generate_playlist
from app.music import resolve_tracks
from app.player import (
    MPVError,
    clear_playlist,
    enqueue_url,
    get_status,
    next_track,
    pause,
    play_url,
    prev_track,
    resume,
    set_volume,
    stop,
)

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("media-asistente")

app = FastAPI(title="Media Asistente", version="0.1.0")


# === Modelos ===

class PlaylistRequest(BaseModel):
    prompt: str
    play_now: bool = True


class VolumeRequest(BaseModel):
    level: int


# === Endpoints públicos (sin auth, solo health) ===

@app.get("/health")
async def health():
    return {"status": "ok"}


# === Endpoints protegidos ===

@app.post("/playlist", dependencies=[Depends(verify_api_key)])
async def create_playlist(req: PlaylistRequest):
    """Genera una playlist con IA y la reproduce"""
    try:
        data = generate_playlist(req.prompt)
    except Exception as e:
        logger.exception("LLM error")
        raise HTTPException(500, f"LLM error: {e}")

    tracks = resolve_tracks(data["tracks"])
    if not tracks:
        raise HTTPException(404, "No track could be resolved on YouTube")

    if req.play_now:
        try:
            clear_playlist()
            play_url(tracks[0]["url"], replace=True)
            for t in tracks[1:]:
                enqueue_url(t["url"])
        except MPVError as e:
            raise HTTPException(503, f"Player not available: {e}")

    return {
        "title": data.get("title"),
        "queued": len(tracks),
        "first_track": tracks[0],
        "tracks": tracks,
    }


@app.post("/control/play", dependencies=[Depends(verify_api_key)])
async def ctl_play():
    try:
        resume()
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/pause", dependencies=[Depends(verify_api_key)])
async def ctl_pause():
    try:
        pause()
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/next", dependencies=[Depends(verify_api_key)])
async def ctl_next():
    try:
        next_track()
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/prev", dependencies=[Depends(verify_api_key)])
async def ctl_prev():
    try:
        prev_track()
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/stop", dependencies=[Depends(verify_api_key)])
async def ctl_stop():
    try:
        stop()
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/volume", dependencies=[Depends(verify_api_key)])
async def ctl_volume(req: VolumeRequest):
    try:
        set_volume(req.level)
        return {"ok": True, "level": req.level}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.get("/status", dependencies=[Depends(verify_api_key)])
async def status():
    try:
        return get_status()
    except MPVError as e:
        raise HTTPException(503, str(e))