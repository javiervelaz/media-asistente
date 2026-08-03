"""API REST: genera playlists con IA y controla mpv"""
import asyncio
import logging
import uuid
from app.history import register_advance, set_current
from app.db import execute as db_execute
from app.curator import curate

from contextlib import asynccontextmanager
from app.db import init_pool, close_pool

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(title="Media Asistente", version="0.2.0", lifespan=lifespan)


# === Modelos ===

class PlaylistRequest(BaseModel):
    prompt: str
    play_now: bool = True
    fade: bool = False
    fade_target: int = 65
    fade_seconds: int = 30
    n_tracks: int = 20
    room_id: str = "main"
    use_curator: bool | None = None   # None = usa el default de config


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
    if req.use_curator is None:
        usar_curador = settings.curator_enabled and _necesita_curador(req.prompt)
    else:
        usar_curador = req.use_curator
        
    usar_curador = (settings.curator_enabled
                    if req.use_curator is None else req.use_curator)

    concept = narration = ""

    if usar_curador:
        try:
            data = await curate(req.prompt, req.n_tracks)
            concept = data.get("concept", "")
            narration = data.get("narration", "")
        except Exception:
            logger.exception("curador falló, cayendo al generador simple")
            data = await asyncio.to_thread(generate_playlist, req.prompt)
    else:
        try:
            data = await asyncio.to_thread(generate_playlist, req.prompt)
        except Exception as e:
            logger.exception("LLM error")
            raise HTTPException(500, f"LLM error: {e}")

    tracks = await resolve_tracks(data["tracks"])
    if not tracks:
        raise HTTPException(404, "No track could be resolved on YouTube")

    playlist_id = uuid.uuid4()

    if req.play_now:
        _cancel_fade()
        try:
            await asyncio.to_thread(_start_playback, tracks, req.fade)
        except MPVError as e:
            raise HTTPException(503, f"Player not available: {e}")

        if req.fade:
            _start_fade(req.fade_target, req.fade_seconds)

        asyncio.create_task(_log_history(tracks, req.room_id, playlist_id))
        set_current(playlist_id, tracks)        # ← nueva línea

    return {
        "title": data.get("title"),
        "concept": concept,
        "narration": narration,
        "queued": len(tracks),
        "first_track": tracks[0],
        "tracks": tracks,
        "faded": req.fade and req.play_now,
        "playlist_id": str(playlist_id),
    }

async def _log_history(tracks: list, room_id: str, playlist_id) -> None:
    """Registra la playlist. No bloquea la respuesta ni la rompe si falla."""
    try:
        for t in tracks:
            await db_execute(
                """
                INSERT INTO play_history (recording_mbid, artist, title,
                                          youtube_id, room_id, playlist_id)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                t.get("recording_mbid"), t["artist"], t["title"],
                t["url"].rsplit("v=", 1)[-1], room_id, playlist_id,
            )
    except Exception:
        logger.exception("no pude registrar el historial")


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
    await register_advance("next")  
    try:
        await asyncio.to_thread(next_track)
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/prev", dependencies=[Depends(verify_api_key)])
async def ctl_prev():
    _cancel_fade()
    await register_advance("prev") 
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

CONTEXT_SQL = """
SELECT a.name AS artist, r.title, r.first_release_date::text AS fecha,
       EXTRACT(YEAR FROM r.first_release_date)::int AS anio,
       (EXTRACT(YEAR FROM CURRENT_DATE)
        - EXTRACT(YEAR FROM r.first_release_date))::int AS aniversario,
       CASE WHEN to_char(r.first_release_date,'MM-DD') = to_char(CURRENT_DATE,'MM-DD')
            THEN 'exacto' ELSE 'cercano' END AS match_type,
       COALESCE(e.weight, 9) AS weight
FROM releases r
JOIN artists a ON a.mbid = r.artist_mbid
LEFT JOIN ephemerides e ON e.mbid = r.mbid::text
WHERE r.primary_type = 'Album'
  AND NOT ('Compilation' = ANY(r.secondary_types))
  AND ABS(((EXTRACT(DOY FROM r.first_release_date)
          - EXTRACT(DOY FROM CURRENT_DATE) + 183)::int % 365) - 183) <= 7
ORDER BY (match_type = 'exacto') DESC,
         (aniversario % 10 = 0) DESC,
         COALESCE(e.weight, 9),
         r.first_release_date
LIMIT 25;
"""


@app.post("/despertador", dependencies=[Depends(verify_api_key)])
async def despertador(req: DespertadorRequest):
    filas = await db_fetch(CONTEXT_SQL)
    if not filas:
        raise HTTPException(404, "sin efemérides en la ventana")

    contexto = "\n".join(
        f"- {r['artist']} — {r['title']} ({r['fecha']}), "
        f"{r['aniversario']} años, coincidencia {r['match_type']}"
        for r in filas)

    prompt = f"""Es la mañana temprano. Armá la playlist del despertador.

Álbumes con aniversario esta semana:
{contexto}

Elegí UN álbum como eje. Priorizá aniversarios redondos y coincidencias exactas de fecha. Arrancá con temas de ese disco y expandí hacia la escena y el momento en que salió. La narración tiene que contar por qué hoy es ese disco: qué pasaba alrededor, quién estaba en esa banda. Empezá suave, es temprano."""

    data = await curate(prompt, req.n_tracks)
    tracks = await resolve_tracks(data["tracks"])
    ...  # mismo bloque de reproducción que /playlist, con fade=True

SEÑALES_COMPLEJAS = ("parecido", "similar", "tipo", "onda", "genealog",
                     "relacionado", "influenc", "escena", "sello", "época",
                     "década", "año", "aniversario", "conexión", "derivado")

def _necesita_curador(prompt: str) -> bool:
    p = prompt.lower()
    return any(s in p for s in SEÑALES_COMPLEJAS) or len(p.split()) > 8