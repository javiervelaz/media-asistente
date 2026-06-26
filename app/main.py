"""API REST: genera playlists con IA y controla mpv"""
import asyncio
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
    set_video,
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
    fade: bool = False              # fade-in suave al arrancar (despertador)
    fade_target: int = 65           # volumen final de la rampa
    fade_seconds: int = 30          # duración de la rampa


class VolumeRequest(BaseModel):
    level: int


class VideoRequest(BaseModel):
    url: str


# === Fade: tarea única, rastreada y cancelable ===

_fade_task: "asyncio.Task | None" = None


def _cancel_fade() -> None:
    """Cancela la rampa de volumen en curso, si hay una."""
    global _fade_task
    if _fade_task and not _fade_task.done():
        _fade_task.cancel()
    _fade_task = None


def _start_fade(target: int, seconds: int) -> None:
    global _fade_task
    _cancel_fade()
    _fade_task = asyncio.create_task(_fade_in(target, seconds))


async def _fade_in(target: int, seconds: int = 30, steps: int = 30):
    """Rampa 0 -> target sin bloquear el loop. Cancelable por cualquier control."""
    target = max(0, min(100, target))
    seconds = max(1, seconds)
    steps = max(1, steps)
    try:
        for i in range(1, steps + 1):
            level = round(target * i / steps)
            await asyncio.to_thread(set_volume, level)   # I/O de mpv fuera del loop
            await asyncio.sleep(seconds / steps)
        logger.info("fade-in completo a volumen %d", target)
    except asyncio.CancelledError:
        logger.info("fade-in cancelado por un control")
        raise
    except Exception:
        logger.exception("fade-in abortado por error en set_volume")


def _start_playback(tracks: list, fade: bool) -> None:
    """Secuencia de carga (sincrónica). Se invoca vía asyncio.to_thread."""
    clear_playlist()
    set_video(False)                 # música = sin video
    if fade:
        set_volume(0)                # silencio antes de soltar el primer track
    play_url(tracks[0]["url"], replace=True)
    for t in tracks[1:]:
        enqueue_url(t["url"])


# === Endpoints públicos ===

@app.get("/health")
async def health():
    return {"status": "ok"}


# === Endpoints protegidos ===

@app.post("/playlist", dependencies=[Depends(verify_api_key)])
async def create_playlist(req: PlaylistRequest):
    """Genera una playlist con IA y la reproduce (solo audio)"""
    try:
        data = await asyncio.to_thread(generate_playlist, req.prompt)
    except Exception as e:
        logger.exception("LLM error")
        raise HTTPException(500, f"LLM error: {e}")

    tracks = await asyncio.to_thread(resolve_tracks, data["tracks"])
    if not tracks:
        raise HTTPException(404, "No track could be resolved on YouTube")

    if req.play_now:
        _cancel_fade()               # cualquier playlist nueva mata un fade previo
        try:
            await asyncio.to_thread(_start_playback, tracks, req.fade)
        except MPVError as e:
            raise HTTPException(503, f"Player not available: {e}")

        if req.fade:
            _start_fade(req.fade_target, req.fade_seconds)

    return {
        "title": data.get("title"),
        "queued": len(tracks),
        "first_track": tracks[0],
        "tracks": tracks,
        "faded": req.fade and req.play_now,
    }


@app.post("/control/play", dependencies=[Depends(verify_api_key)])
async def ctl_play():
    try:
        await asyncio.to_thread(resume)
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/pause", dependencies=[Depends(verify_api_key)])
async def ctl_pause():
    _cancel_fade()
    try:
        await asyncio.to_thread(pause)
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/next", dependencies=[Depends(verify_api_key)])
async def ctl_next():
    _cancel_fade()
    try:
        await asyncio.to_thread(next_track)
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/prev", dependencies=[Depends(verify_api_key)])
async def ctl_prev():
    _cancel_fade()
    try:
        await asyncio.to_thread(prev_track)
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/stop", dependencies=[Depends(verify_api_key)])
async def ctl_stop():
    _cancel_fade()                   # primero matás la rampa, después parás
    try:
        await asyncio.to_thread(stop)
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/volume", dependencies=[Depends(verify_api_key)])
async def ctl_volume(req: VolumeRequest):
    _cancel_fade()                   # si el usuario toca volumen, el fade cede
    try:
        await asyncio.to_thread(set_volume, req.level)
        return {"ok": True, "level": req.level}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.get("/status", dependencies=[Depends(verify_api_key)])
async def status():
    try:
        return await asyncio.to_thread(get_status)
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/play_video", dependencies=[Depends(verify_api_key)])
async def play_video(req: VideoRequest):
    """Reproduce un video con audio + imagen en HDMI"""
    _cancel_fade()
    try:
        await asyncio.to_thread(_play_video_sync, req.url)
        return {"ok": True, "playing": req.url}
    except MPVError as e:
        raise HTTPException(503, str(e))


def _play_video_sync(url: str) -> None:
    clear_playlist()
    set_video(True)
    play_url(url, replace=True)