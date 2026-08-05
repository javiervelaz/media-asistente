"""API REST: genera playlists con IA y controla mpv"""
import asyncio
import logging
import random
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app import local_search
from app.auth import verify_api_key
from app.config import settings
from app.curator import curate
from app.db import (
    close_pool,
    execute as db_execute,
    fetch as db_fetch,
    fetchrow as db_fetchrow,
    init_pool,
)
from app.history import (
    append_current,
    get_track_at,
    register_advance,
    set_current,
)
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


app = FastAPI(title="Media Asistente", version="0.3.0", lifespan=lifespan)

ARRANQUE = 5   # tracks a resolver antes de devolver respuesta


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
    use_local: bool | None = None     # None = usa el default de config


class DespertadorRequest(BaseModel):
    n_tracks: int = 15
    fade: bool = True
    fade_target: int = 55
    fade_seconds: int = 60
    room_id: str = "despertador"


class PromptRequest(BaseModel):
    prompt: str


class VolumeRequest(BaseModel):
    level: int | None = None    # absoluto
    delta: int | None = None    # relativo: +10 / -10


class VideoRequest(BaseModel):
    url: str


# === Tasks en background ===

_bg_tasks: set = set()
_fade_task: "asyncio.Task | None" = None
_resto_task: "asyncio.Task | None" = None


def _fire(coro) -> "asyncio.Task":
    """Lanza una task manteniendo referencia fuerte hasta que termina."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


def _cancel_resto() -> None:
    """Cancela la resolución en background de la playlist anterior."""
    global _resto_task
    if _resto_task and not _resto_task.done():
        _resto_task.cancel()
        logger.info("cancelada la resolución en background anterior")
    _resto_task = None


def _cancel_fade() -> None:
    """Cancela la rampa de volumen en curso, si hay una."""
    global _fade_task
    if _fade_task and not _fade_task.done():
        _fade_task.cancel()
    _fade_task = None


def _start_fade(target: int, seconds: int) -> None:
    global _fade_task
    _cancel_fade()
    _fade_task = _fire(_fade_in(target, seconds))


async def _fade_in(target: int, seconds: int = 30, steps: int = 30):
    """Rampa 0 -> target sin bloquear el loop. Cancelable por cualquier control."""
    target = max(0, min(100, target))
    seconds = max(1, seconds)
    steps = max(1, steps)
    try:
        for i in range(1, steps + 1):
            level = round(target * i / steps)
            await asyncio.to_thread(set_volume, level)
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


# === Persistencia del historial ===

def _uuid_o_none(v) -> uuid.UUID | None:
    """El curador puede devolver MBIDs basura. No rompemos la playlist por eso."""
    if not v:
        return None
    try:
        return uuid.UUID(str(v))
    except (ValueError, AttributeError, TypeError):
        return None


def _video_id(url: str | None) -> str | None:
    if not url or "v=" not in url:
        return None
    return url.rsplit("v=", 1)[-1] or None


# artist_mbid se deriva del recording: el curador no lo devuelve y no
# queremos que lo invente. Si el recording no está en el grafo, queda NULL.
INSERT_HISTORY = """
INSERT INTO play_history
    (playlist_id, position, artist, title, rationale,
     recording_mbid, artist_mbid, youtube_id, room_id)
SELECT $1, $2, $3, $4, $5, $6,
       (SELECT rl.artist_mbid
          FROM recordings r
          JOIN releases rl ON rl.mbid = r.release_mbid
         WHERE r.mbid = $6),
       $7, $8
"""


async def _log_history(tracks: list, room_id: str, playlist_id,
                       offset: int = 0) -> None:
    """Registra tracks YA RESUELTOS. `position` tiene que coincidir con
    el índice de mpv: por eso el offset cuando llega la cola en background."""
    try:
        for i, t in enumerate(tracks, start=offset):
            await db_execute(
                INSERT_HISTORY,
                playlist_id,
                i,
                t["artist"],
                t["title"],
                t.get("rationale"),
                _uuid_o_none(t.get("recording_mbid")),
                _video_id(t.get("url")),
                room_id,
            )
    except Exception:
        logger.exception("no pude registrar el historial")


async def _resolver_resto(pendientes: list, playlist_id, room_id: str,
                          offset: int) -> None:
    """Resuelve y encola el resto mientras ya está sonando."""
    if not pendientes:
        return
    try:
        resto = await resolve_tracks(pendientes)
        for t in resto:
            await asyncio.to_thread(enqueue_url, t["url"])
        logger.info("encolados %d tracks adicionales", len(resto))

        append_current(resto)
        await _log_history(resto, room_id, playlist_id, offset=offset)
    except asyncio.CancelledError:
        logger.info("resolución en background cancelada")
        raise
    except Exception:
        logger.exception("falló la resolución en background")


# === Punto de entrada único de reproducción ===

async def _lanzar(propuestos: list[dict], titulo: str, room_id: str = "main",
                  source: str = "curator", prompt: str | None = None,
                  play_now: bool = True, fade: bool = False,
                  fade_target: int = 65, fade_seconds: int = 30) -> dict:
    """Resuelve la cabeza, arranca mpv y encola el resto en background.

    Todo lo que reproduce pasa por acá: /playlist, /despertador, /replay
    y /searchInHistory. `propuestos` son tracks sin resolver.
    """
    global _resto_task

    if not propuestos:
        raise HTTPException(404, "no hay tracks para reproducir")

    playlist_id = uuid.uuid4()

    try:
        await db_execute(
            """INSERT INTO playlists (id, title, prompt, room_id, source)
               VALUES ($1,$2,$3,$4,$5)""",
            playlist_id, titulo, prompt, room_id, source,
        )
    except Exception:
        logger.exception("no pude registrar la playlist (sigo igual)")

    cabeza = await resolve_tracks(propuestos[:ARRANQUE])
    if not cabeza:
        raise HTTPException(404, "No track could be resolved on YouTube")

    if play_now:
        _cancel_fade()
        _cancel_resto()
        try:
            await asyncio.to_thread(_start_playback, cabeza, fade)
        except MPVError as e:
            raise HTTPException(503, f"Player not available: {e}")

        if fade:
            _start_fade(fade_target, fade_seconds)

        set_current(playlist_id, cabeza)
        _fire(_log_history(cabeza, room_id, playlist_id, offset=0))
        _resto_task = _fire(
            _resolver_resto(propuestos[ARRANQUE:], playlist_id,
                            room_id, offset=len(cabeza))
        )

    return {
        "playlist_id": str(playlist_id),
        "title": titulo,
        "source": source,
        "queued": len(cabeza),
        "pending": max(0, len(propuestos) - ARRANQUE),
        "first_track": cabeza[0],
        "tracks": cabeza,
        "proposed": propuestos,
        "faded": fade and play_now,
    }


# === Endpoints públicos ===

@app.get("/health")
async def health():
    return {"status": "ok"}


# === Playlists ===

@app.post("/playlist", dependencies=[Depends(verify_api_key)])
async def create_playlist(req: PlaylistRequest):
    """Genera una playlist y la reproduce (solo audio).

    Ruteo: primero el grafo local (0 tokens). Si no alcanza, el curador.
    """
    t0 = time.monotonic()

    usar_local = (getattr(settings, "local_search_enabled", False)
                  if req.use_local is None else req.use_local)

    # --- Vía local ---
    if usar_local:
        candidatos = await local_search.buscar(req.prompt, limite=req.n_tracks)
        via = local_search.clasificar(candidatos)

        if via == "local":
            resp = await _lanzar(
                candidatos, req.prompt, req.room_id, source="local",
                prompt=req.prompt, play_now=req.play_now, fade=req.fade,
                fade_target=req.fade_target, fade_seconds=req.fade_seconds,
            )
            logger.info("timing — local:%.1fs total:%.1fs",
                        time.monotonic() - t0, time.monotonic() - t0)
            resp["concept"] = resp["narration"] = ""
            return resp

        if via == "hybrid":
            resp = await _lanzar(
                candidatos[:local_search.MIN_TRACKS_HEAD], req.prompt,
                req.room_id, source="hybrid", prompt=req.prompt,
                play_now=req.play_now, fade=req.fade,
                fade_target=req.fade_target, fade_seconds=req.fade_seconds,
            )
            if req.play_now:
                _fire(_completar_con_curador(
                    uuid.UUID(resp["playlist_id"]), req.prompt,
                    candidatos[:local_search.MIN_TRACKS_HEAD],
                    req.n_tracks, req.room_id, offset=resp["queued"],
                ))
            resp["concept"] = resp["narration"] = ""
            return resp

    # --- Vía curador (camino histórico, intacto) ---
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

    t_cur = time.monotonic()

    resp = await _lanzar(
        data["tracks"], data.get("title") or req.prompt, req.room_id,
        source="curator", prompt=req.prompt, play_now=req.play_now,
        fade=req.fade, fade_target=req.fade_target,
        fade_seconds=req.fade_seconds,
    )

    logger.info("timing — curador:%.1fs resto:%.1fs total:%.1fs",
                t_cur - t0, time.monotonic() - t_cur, time.monotonic() - t0)

    resp["concept"] = concept
    resp["narration"] = narration
    return resp


async def _completar_con_curador(playlist_id: uuid.UUID, prompt: str,
                                 ya_locales: list[dict], n_tracks: int,
                                 room_id: str, offset: int) -> None:
    """Modo hybrid: los locales ya están sonando, el curador completa la cola."""
    try:
        sonando = "\n".join(f"- {t['artist']} — {t['title']}" for t in ya_locales)
        prompt_ext = (
            f"{prompt}\n\nYa están sonando estos tracks, NO los repitas "
            f"ni traigas otras versiones de los mismos temas:\n{sonando}"
        )
        data = await curate(prompt_ext, max(1, n_tracks - len(ya_locales)))
        resto = await resolve_tracks(data["tracks"])
        for t in resto:
            await asyncio.to_thread(enqueue_url, t["url"])

        append_current(resto)
        await _log_history(resto, room_id, playlist_id, offset=offset)
        logger.info("hybrid: el curador sumó %d tracks", len(resto))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("hybrid: el curador falló, sigue con los locales")


@app.get("/playlists", dependencies=[Depends(verify_api_key)])
async def listar_playlists(limit: int = 10):
    """Historial de playlists reproducibles. El JOIN excluye las que no sonaron."""
    limit = max(1, min(50, limit))
    rows = await db_fetch(
        """SELECT p.id::text AS id, p.title, p.source,
                  p.created_at, count(h.id) AS tracks
           FROM playlists p
           JOIN play_history h ON h.playlist_id = p.id
           GROUP BY p.id, p.title, p.source, p.created_at
           ORDER BY p.created_at DESC
           LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


@app.post("/playlist/{playlist_id}/replay", dependencies=[Depends(verify_api_key)])
async def replay(playlist_id: uuid.UUID, sin_skips: bool = True,
                 shuffle: bool = False, fade: bool = False,
                 fade_target: int = 65, fade_seconds: int = 30):
    """Repite una playlist del historial. No llama al curador."""
    rows = await db_fetch(
        """SELECT artist, title, rationale, recording_mbid
           FROM play_history
           WHERE playlist_id = $1
             AND ($2 = false OR skipped IS DISTINCT FROM true)
           ORDER BY position NULLS LAST, id""",
        playlist_id, sin_skips,
    )
    if not rows:
        raise HTTPException(404, "esa playlist no existe o quedó vacía")

    orig = await db_fetchrow(
        "SELECT title, room_id FROM playlists WHERE id = $1", playlist_id)

    tracks = [{
        "artist": r["artist"],
        "title": r["title"],
        "rationale": r["rationale"],
        "recording_mbid": str(r["recording_mbid"]) if r["recording_mbid"] else None,
    } for r in rows]

    if shuffle:
        random.shuffle(tracks)

    titulo = f"{orig['title'] if orig else 'Playlist'} (repetida)"
    room_id = (orig["room_id"] if orig and orig["room_id"] else "main")

    return await _lanzar(tracks, titulo, room_id, source="replay",
                         fade=fade, fade_target=fade_target,
                         fade_seconds=fade_seconds)


@app.post("/searchInHistory", dependencies=[Depends(verify_api_key)])
async def search_in_history(req: PromptRequest, play: bool = False,
                            limite: int = 20):
    """Endpoint de medición del grafo local. Con play=false no reproduce."""
    tracks = await local_search.buscar(req.prompt, limite=limite)
    via = local_search.clasificar(tracks)

    logger.info("searchInHistory prompt=%r via=%s n=%d cached=%d",
                req.prompt, via, len(tracks),
                sum(1 for t in tracks if t["cached"]))

    if not play:
        return {"via": via, "count": len(tracks), "tracks": tracks}

    if via == "curator":
        raise HTTPException(422, "sin señal local suficiente")

    return await _lanzar(tracks, f"Historial: {req.prompt}",
                         source=via, prompt=req.prompt)


# === Despertador ===

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
    """Playlist de la mañana, anclada en aniversarios de esta semana."""
    filas = await db_fetch(CONTEXT_SQL)
    if not filas:
        raise HTTPException(404, "sin efemérides en la ventana")

    contexto = "\n".join(
        f"- {r['artist']} — {r['title']} ({r['fecha']}), "
        f"{r['aniversario']} años, coincidencia {r['match_type']}"
        for r in filas
    )

    prompt = f"""Es la mañana temprano. Armá la playlist del despertador.

    Álbumes con aniversario esta semana:
    {contexto}

    Elegí UN álbum como eje. Priorizá aniversarios redondos y coincidencias exactas de fecha. Arrancá con temas de ese disco y expandí hacia la escena y el momento en que salió. La narración tiene que contar por qué hoy es ese disco: qué pasaba alrededor, quién estaba en esa banda. Empezá suave, es temprano."""

    try:
        data = await curate(prompt, req.n_tracks)
    except Exception as e:
        logger.exception("curador falló en el despertador")
        raise HTTPException(502, f"curador: {e}")

    resp = await _lanzar(
        data["tracks"], data.get("title") or "Despertador", req.room_id,
        source="curator", prompt="despertador", fade=req.fade,
        fade_target=req.fade_target, fade_seconds=req.fade_seconds,
    )
    resp["concept"] = data.get("concept", "")
    resp["narration"] = data.get("narration", "")
    return resp


# === Controles ===

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
    await register_advance("next")          # antes de saltar
    try:
        await asyncio.to_thread(next_track)
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/prev", dependencies=[Depends(verify_api_key)])
async def ctl_prev():
    _cancel_fade()
    await register_advance("prev")          # volver atrás no es skip
    try:
        await asyncio.to_thread(prev_track)
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/stop", dependencies=[Depends(verify_api_key)])
async def ctl_stop():
    _cancel_fade()
    _cancel_resto()
    try:
        await asyncio.to_thread(stop)
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/volume", dependencies=[Depends(verify_api_key)])
async def ctl_volume(req: VolumeRequest | None = None):
    _cancel_fade()

    if req is None or (req.level is None and req.delta is None):
        raise HTTPException(400, "mandá 'level' (absoluto) o 'delta' (relativo)")

    try:
        if req.delta is not None:
            st = await asyncio.to_thread(get_status)
            actual = int(st.get("volume") or 0)
            nuevo = actual + req.delta
        else:
            nuevo = req.level

        nuevo = max(0, min(100, nuevo))
        await asyncio.to_thread(set_volume, nuevo)
        return {"ok": True, "level": nuevo}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.get("/status", dependencies=[Depends(verify_api_key)])
async def status():
    try:
        st = await asyncio.to_thread(get_status)
    except MPVError as e:
        raise HTTPException(503, str(e))

    t = get_track_at(st.get("playlist_pos"))
    if t:
        st["artist"] = t["artist"]
        st["track"] = t["title"]
        st["now_playing"] = f"{t['artist']} - {t['title']}"
        st["rationale"] = t.get("rationale")
    else:
        # Sin playlist en memoria (reinicio del servicio, playlist vieja)
        st["artist"] = None
        st["track"] = st.get("title")
        st["now_playing"] = st.get("title") or "—"

    return st


@app.post("/play_video", dependencies=[Depends(verify_api_key)])
async def play_video(req: VideoRequest):
    """Reproduce un video con audio + imagen en HDMI"""
    _cancel_fade()
    _cancel_resto()
    try:
        await asyncio.to_thread(_play_video_sync, req.url)
        return {"ok": True, "playing": req.url}
    except MPVError as e:
        raise HTTPException(503, str(e))


def _play_video_sync(url: str) -> None:
    clear_playlist()
    set_video(True)
    play_url(url, replace=True)