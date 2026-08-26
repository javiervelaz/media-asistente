"""API REST: genera playlists con IA y controla mpv"""
import asyncio
import logging
import random
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app import local_search, player, tracks
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
from app.music import mark_failed, resolve_tracks
from app.player import MPVError

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("media-asistente")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    player.iniciar_observador(on_fail=mark_failed)
    yield
    await close_pool()


app = FastAPI(title="Media Asistente", version="0.4.0", lifespan=lifespan)

ARRANQUE = 1   # tracks a resolver antes de devolver respuesta


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


def _set_resto(coro) -> "asyncio.Task":
    """Registra la task que produce la cola de la playlist en curso.

    Tiene que haber UNA sola por playlist: si hay dos encolando en paralelo
    las posiciones de play_history se pisan y el feedback de skips queda
    atribuido al track equivocado.
    """
    global _resto_task
    _resto_task = _fire(coro)
    return _resto_task


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
            await player.set_volume(level)
            await asyncio.sleep(seconds / steps)
        logger.info("fade-in completo a volumen %d", target)
    except asyncio.CancelledError:
        logger.info("fade-in cancelado por un control")
        raise
    except Exception:
        logger.exception("fade-in abortado por error en set_volume")


# === Descarga + encolado ===

async def _bajar(t: dict) -> "str | None":
    """Baja el archivo y lo registra para el observador. None si falla."""
    yid = t.get("youtube_id")
    if not yid:
        logger.error("track sin youtube_id: %s - %s", t.get("artist"), t.get("title"))
        return None
    try:
        path = await tracks.obtener_track(yid)
    except tracks.TrackNoDisponible as e:
        logger.error("no se pudo bajar %s - %s: %s", t["artist"], t["title"], e)
        await mark_failed(yid, str(e))
        return None
    player.registrar_track(path, yid)
    return str(path)


async def _encolar(t: dict) -> bool:
    path = await _bajar(t)
    if not path:
        return False
    await player.enqueue_path(path)
    return True


async def _start_playback(cabeza: list, fade: bool) -> list:
    """Baja el primero, arranca, y encola el resto de la cabeza.
    Devuelve los tracks que realmente entraron a la cola."""
    primero = None
    idx = 0
    for idx, t in enumerate(cabeza):
        primero = await _bajar(t)
        if primero:
            break
    if not primero:
        raise HTTPException(404, "ningún track de la cabeza se pudo descargar")

    await player.clear_playlist()
    await player.set_video(False)          # música = sin video
    if fade:
        await player.set_volume(0)         # silencio antes del primer track
    await player.play_path(primero, replace=True)

    encolados = [cabeza[idx]]
    for t in cabeza[idx + 1:]:
        if await _encolar(t):
            encolados.append(t)
    return encolados


# === Persistencia del historial ===

def _uuid_o_none(v) -> uuid.UUID | None:
    """El curador puede devolver MBIDs basura. No rompemos la playlist por eso."""
    if not v:
        return None
    try:
        return uuid.UUID(str(v))
    except (ValueError, AttributeError, TypeError):
        return None


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


async def _log_history(tracks_: list, room_id: str, playlist_id,
                       offset: int = 0) -> None:
    """Registra tracks YA RESUELTOS Y ENCOLADOS. `position` tiene que coincidir
    con el índice de mpv: por eso el offset cuando llega la cola en background."""
    try:
        for i, t in enumerate(tracks_, start=offset):
            await db_execute(
                INSERT_HISTORY,
                playlist_id,
                i,
                t["artist"],
                t["title"],
                t.get("rationale"),
                _uuid_o_none(t.get("recording_mbid")),
                t.get("youtube_id"),
                room_id,
            )
    except Exception:
        logger.exception("no pude registrar el historial")


async def _resolver_resto(pendientes: list, playlist_id, room_id: str,
                          offset: int) -> None:
    """Resuelve, baja y encola el resto mientras ya está sonando."""
    if not pendientes:
        return
    try:
        encolados = await _encolar_lote(pendientes, playlist_id, room_id, offset)
        logger.info("encolados %d/%d tracks adicionales",
                    len(encolados), len(pendientes))
    except asyncio.CancelledError:
        logger.info("resolución en background cancelada")
        raise
    except Exception:
        logger.exception("falló la resolución en background")


# === Punto de entrada único de reproducción ===

async def _lanzar(propuestos: list[dict], titulo: str, room_id: str = "main",
                  source: str = "curator", prompt: str | None = None,
                  play_now: bool = True, fade: bool = False,
                  fade_target: int = 65, fade_seconds: int = 30,
                  resolver_resto: bool = True) -> dict:
    """Resuelve la cabeza, arranca mpv y encola el resto en background.

    Todo lo que reproduce pasa por acá: /playlist, /despertador, /replay
    y /searchInHistory. `propuestos` son tracks sin resolver.

    resolver_resto=False deja la cola en manos del que llama: lo usa el modo
    hybrid, donde el curador es el único productor y encola primero el resto
    de los locales. Dos productores en paralelo pisan las posiciones.
    """

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

    encolados = cabeza
    if play_now:
        _cancel_fade()
        _cancel_resto()
        try:
            encolados = await _start_playback(cabeza, fade)
        except MPVError as e:
            raise HTTPException(503, f"Player not available: {e}")

        if fade:
            _start_fade(fade_target, fade_seconds)

        set_current(playlist_id, encolados)
        _fire(_log_history(encolados, room_id, playlist_id, offset=0))
        if resolver_resto:
            _set_resto(_resolver_resto(propuestos[ARRANQUE:], playlist_id,
                                       room_id, offset=len(encolados)))

    return {
        "playlist_id": str(playlist_id),
        "title": titulo,
        "source": source,
        "queued": len(encolados),
        "pending": max(0, len(propuestos) - ARRANQUE),
        "first_track": encolados[0],
        "tracks": encolados,
        "proposed": propuestos,
        "faded": fade and play_now,
    }


# === Endpoints públicos ===

@app.get("/health")
async def health():
    """Incluye el estado del POT provider: sin él, las descargas fallan."""
    return {"status": "ok", "pot_provider": await tracks.pot_ok()}


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
            cabeza = candidatos[:local_search.MIN_TRACKS_HEAD]
            resp = await _lanzar(
                cabeza, req.prompt,
                req.room_id, source="hybrid", prompt=req.prompt,
                play_now=req.play_now, fade=req.fade,
                fade_target=req.fade_target, fade_seconds=req.fade_seconds,
                resolver_resto=False,   # el completador es el único productor
            )
            if req.play_now:
                _set_resto(_completar_cola_hybrid(
                    uuid.UUID(resp["playlist_id"]), req.prompt,
                    locales_pendientes=cabeza[ARRANQUE:],
                    ya_sonando=resp["tracks"],
                    n_tracks=req.n_tracks, room_id=req.room_id,
                    offset=resp["queued"],
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


async def _encolar_lote(propuestos: list[dict], playlist_id: uuid.UUID,
                        room_id: str, offset: int) -> list[dict]:
    """Resuelve, baja y encola un lote. Devuelve lo que entró a la cola.

    `offset` es la posición de mpv del primer track del lote: tiene que ser
    la cantidad de tracks ya encolados, no un número calculado por el que llama.
    """
    if not propuestos:
        return []
    resueltos = await resolve_tracks(propuestos)
    encolados = []
    for t in resueltos:
        if await _encolar(t):
            encolados.append(t)
    if encolados:
        append_current(encolados)
        await _log_history(encolados, room_id, playlist_id, offset=offset)
    return encolados


async def _completar_cola_hybrid(playlist_id: uuid.UUID, prompt: str,
                                 locales_pendientes: list[dict],
                                 ya_sonando: list[dict], n_tracks: int,
                                 room_id: str, offset: int) -> None:
    """Único productor de cola en modo hybrid.

    Encola en dos etapas SECUENCIALES —primero el resto de los locales,
    después lo que traiga el curador— para que `position` en play_history
    siga siendo el índice real de mpv. Antes esto corría en paralelo con
    _resolver_resto y las dos series de posiciones se pisaban.
    """
    try:
        pos = offset
        locales = await _encolar_lote(locales_pendientes, playlist_id,
                                      room_id, offset=pos)
        pos += len(locales)
        logger.info("hybrid: %d locales encolados, cola en %d", len(locales), pos)

        ya = ya_sonando + locales
        faltan = max(1, n_tracks - len(ya))
        sonando = "\n".join(f"- {t['artist']} — {t['title']}" for t in ya)
        prompt_ext = (
            f"{prompt}\n\nYa están sonando estos tracks, NO los repitas "
            f"ni traigas otras versiones de los mismos temas:\n{sonando}"
        )
        data = await curate(prompt_ext, faltan)
        encolados = await _encolar_lote(data["tracks"], playlist_id,
                                        room_id, offset=pos)
        logger.info("hybrid: el curador sumó %d tracks", len(encolados))
    except asyncio.CancelledError:
        logger.info("hybrid: cola cancelada por una playlist nueva")
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

    lista = [{
        "artist": r["artist"],
        "title": r["title"],
        "rationale": r["rationale"],
        "recording_mbid": str(r["recording_mbid"]) if r["recording_mbid"] else None,
    } for r in rows]

    if shuffle:
        random.shuffle(lista)

    titulo = f"{orig['title'] if orig else 'Playlist'} (repetida)"
    room_id = (orig["room_id"] if orig and orig["room_id"] else "main")

    return await _lanzar(lista, titulo, room_id, source="replay",
                         fade=fade, fade_target=fade_target,
                         fade_seconds=fade_seconds)


@app.post("/searchInHistory", dependencies=[Depends(verify_api_key)])
async def search_in_history(req: PromptRequest, play: bool = False,
                            limite: int = 20):
    """Endpoint de medición del grafo local. Con play=false no reproduce."""
    encontrados = await local_search.buscar(req.prompt, limite=limite)
    via = local_search.clasificar(encontrados)

    logger.info("searchInHistory prompt=%r via=%s n=%d cached=%d",
                req.prompt, via, len(encontrados),
                sum(1 for t in encontrados if t["cached"]))

    if not play:
        return {"via": via, "count": len(encontrados), "tracks": encontrados}

    if via == "curator":
        raise HTTPException(422, "sin señal local suficiente")

    return await _lanzar(encontrados, f"Historial: {req.prompt}",
                         source=via, prompt=req.prompt)


# === Despertador ===

DESPERTADOR_SQL = """
WITH discos AS (
    SELECT r.mbid AS release_mbid, a.name AS artist, r.title AS album,
           EXTRACT(YEAR FROM r.first_release_date)::int AS anio,
           (EXTRACT(YEAR FROM CURRENT_DATE)
            - EXTRACT(YEAR FROM r.first_release_date))::int AS aniversario,
           row_number() OVER (
               ORDER BY (to_char(r.first_release_date,'MM-DD')
                         = to_char(CURRENT_DATE,'MM-DD')) DESC,
                        ((EXTRACT(YEAR FROM CURRENT_DATE)
                          - EXTRACT(YEAR FROM r.first_release_date))::int % 10 = 0) DESC,
                        COALESCE(e.weight, 9),
                        random()
           ) AS rank_disco
    FROM releases r
    JOIN artists a ON a.mbid = r.artist_mbid
    LEFT JOIN ephemerides e ON e.mbid = r.mbid::text
    WHERE r.primary_type = 'Album'
      AND NOT ('Compilation' = ANY(r.secondary_types))
      AND ABS(((EXTRACT(DOY FROM r.first_release_date)
              - EXTRACT(DOY FROM CURRENT_DATE) + 183)::int % 365) - 183) <= 7
      AND EXISTS (SELECT 1 FROM recordings rc WHERE rc.release_mbid = r.mbid)
),
elegidos AS (
    SELECT * FROM discos WHERE rank_disco <= 5
),
tracks AS (
    SELECT d.artist, d.album, d.anio, d.aniversario, d.rank_disco,
           rc.mbid::text AS recording_mbid, rc.title, rc.length_ms,
           row_number() OVER (
               PARTITION BY d.release_mbid ORDER BY rc.position NULLS LAST
           ) AS n_en_disco
    FROM elegidos d
    JOIN recordings rc ON rc.release_mbid = d.release_mbid
    LEFT JOIN track_resolutions tr ON tr.recording_mbid = rc.mbid
    WHERE COALESCE(tr.fail_count, 0) < 2
      AND (rc.length_ms IS NULL OR rc.length_ms BETWEEN 60000 AND 900000)
)
SELECT artist, album, anio, aniversario, recording_mbid, title, length_ms
FROM tracks
ORDER BY n_en_disco, rank_disco
LIMIT $1
"""


@app.post("/despertador", dependencies=[Depends(verify_api_key)])
async def despertador(req: DespertadorRequest):
    """Playlist de la mañana desde el grafo local. Cero tokens."""
    filas = await db_fetch(DESPERTADOR_SQL, req.n_tracks)
    if not filas:
        raise HTTPException(404, "sin álbumes con aniversario y tracklist cargado")

    # Un renglón por disco, en el orden en que aparecen
    discos = {}
    for r in filas:
        discos.setdefault(
            (r["artist"], r["album"]),
            f"{r['artist']} — {r['album']} ({r['anio']}, {r['aniversario']} años)")

    concepto = " · ".join(discos.values())
    titulo = f"Efemérides: {len(discos)} discos"

    lista = [{
        "artist": r["artist"],
        "title": r["title"],
        "recording_mbid": r["recording_mbid"],
        "length_ms": r["length_ms"],
        "rationale": f"{r['album']} ({r['anio']}) — {r['aniversario']} años hoy",
    } for r in filas]

    resp = await _lanzar(
        lista, titulo, req.room_id, source="local", prompt="despertador",
        fade=req.fade, fade_target=req.fade_target,
        fade_seconds=req.fade_seconds,
    )
    resp["concept"] = concepto
    resp["narration"] = ""
    return resp


# === Controles ===

@app.post("/control/play", dependencies=[Depends(verify_api_key)])
async def ctl_play():
    try:
        await player.resume()
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/pause", dependencies=[Depends(verify_api_key)])
async def ctl_pause():
    _cancel_fade()
    try:
        await player.pause()
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/next", dependencies=[Depends(verify_api_key)])
async def ctl_next():
    _cancel_fade()
    await register_advance("next")          # antes de saltar
    try:
        await player.next_track()
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/prev", dependencies=[Depends(verify_api_key)])
async def ctl_prev():
    _cancel_fade()
    await register_advance("prev")          # volver atrás no es skip
    try:
        await player.prev_track()
        return {"ok": True}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.post("/control/stop", dependencies=[Depends(verify_api_key)])
async def ctl_stop():
    _cancel_fade()
    _cancel_resto()
    try:
        await player.stop()
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
            st = await player.get_status()
            actual = int(st.get("volume") or 0)
            nuevo = actual + req.delta
        else:
            nuevo = req.level

        nuevo = max(0, min(100, nuevo))
        await player.set_volume(nuevo)
        return {"ok": True, "level": nuevo}
    except MPVError as e:
        raise HTTPException(503, str(e))


@app.get("/status", dependencies=[Depends(verify_api_key)])
async def status():
    st = await player.get_status()
    if not st.get("mpv_ok"):
        raise HTTPException(503, "mpv no responde")

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
    """Reproduce un video con audio + imagen en HDMI.

    mpv corre con --no-ytdl, así que el archivo se baja antes.
    Tarda bastante más que antes: un video de 4 min son ~30-60s en el Pi 3B.
    """
    _cancel_fade()
    _cancel_resto()

    yid = tracks.extraer_id(req.url)
    if not yid:
        raise HTTPException(400, "no pude extraer el youtube_id de esa URL")

    try:
        path = await tracks.obtener_video(yid)
    except tracks.TrackNoDisponible as e:
        raise HTTPException(502, f"no se pudo bajar el video: {e}")

    try:
        await player.clear_playlist()
        await player.set_video(True)
        await player.play_path(path, replace=True)
        return {"ok": True, "playing": req.url, "path": str(path)}
    except MPVError as e:
        raise HTTPException(503, str(e))