"""API REST: genera playlists con IA y controla mpv"""
import asyncio
import logging

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
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


# === Helpers ===

async def _fade_in(target: int, seconds: int = 30, steps: int = 30):
    """Rampa de volumen 0 -> target, sin bloquear el event loop.

    Corre como BackgroundTask: la respuesta HTTP ya salió y la música
    suena en volumen 0 mientras esta corrutina la sube de a poco.
    """
    target = max(0, min(100, target))
    steps = max(1, steps)
    for i in range(1, steps + 1):
        level = round(target * i / steps)
        try:
            set_volume(level)
        except MPVError:
            logger.warning("fade-in: mpv no disponible, corto la rampa")
            return
        await asyncio.sleep(seconds / steps)


# === Endpoints públicos (sin auth, solo health) ===

@app.get("/health")
async def health():
    return {"status": "ok"}


# === Endpoints protegidos ===

@app.post("/playlist", dependencies=[Depends(verify_api_key)])
async def create_playlist(req: PlaylistRequest, background: BackgroundTasks):
    """Genera una playlist con IA y la reproduce (solo audio)"""
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
            if req.fade:
                set_volume(0)  # arrancar en silencio antes de cargar
            clear_playlist()
            set_video(False)  # música = sin video
            play_url(tracks[0]["url"], replace=True)
            for t in tracks[1:]:
                enqueue_url(t["url"])
        except MPVError as e:
            raise HTTPException(503, f"Player not available: {e}")

        # La rampa sube el volumen DESPUÉS de responder, sin bloquear.
        if req.fade:
            background.add_task(_fade_in, req.fade_target, req.fade_seconds)

    return {
        "title": data.get("title"),
        "queued": len(tracks),
        "first_track": tracks[0],
        "tracks": tracks,
        "faded": req.fade and req.play_now,
    }


def _process_playlist_background(prompt: str):
    try:
        data = generate_playlist(prompt)
        tracks = resolve_tracks(data["tracks"])
        if not tracks:
            logger.warning(f"No tracks resueltos para: {prompt}")
            return
        # 1) limpio cola actual
        clear_playlist()
        # 2) sin video para música
        set_video(False)
        # 3) cargo el primero (esto arranca la reproducción)
        play_url(tracks[0]["url"], replace=True)
        # 4) encolo el resto
        for t in tracks[1:]:
            enqueue_url(t["url"])
        logger.info(f"Playlist '{data.get('title')}' lista, {len(tracks)} tracks")
    except Exception:
        logger.exception("Error en playlist background")


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


@app.post("/play_video", dependencies=[Depends(verify_api_key)])
async def play_video(req: VideoRequest):
    """Reproduce un video con audio + imagen en HDMI"""
    try:
        clear_playlist()
        set_video(True)
        play_url(req.url, replace=True)
        return {"ok": True, "playing": req.url}
    except MPVError as e:
        raise HTTPException(503, str(e))