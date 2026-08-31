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
from app.harness import executors as harness_exec, goals
from app.harness import queries as harness_queries
from app.harness.chat import responder as harness_responder
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
    register_complete,
    restaurar_current,
    set_current,
)
from app.llm import generate_playlist
from app.music import mark_failed, resolve_track, resolve_tracks
from app.player import MPVError

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("media-asistente")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    player.iniciar_observador(on_fail=mark_failed, on_eof=register_complete)
    # mpv sobrevive al restart del servicio; `_current` no. Sin esto, cada
    # reinicio con musica sonando pierde en silencio la señal de esa playlist.
    try:
        await restaurar_current()
    except Exception:
        logger.exception("no pude restaurar la playlist en curso")
    # El harness reusa los cancelables sin importar main (ciclo).
    harness_exec.set_hooks(cancel_fade=_cancel_fade,
                           cancel_resto=_cancel_resto,
                           crear_playlist=_harness_playlist,
                           lanzar_tracks=_harness_lanzar)
    yield
    await close_pool()


app = FastAPI(title="Media Asistente", version="0.4.0", lifespan=lifespan)

MAX_INTENTOS_CABEZA = 6   # candidatos que se caminan buscando uno que arranque


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
    sesgo: str | None = None          # contexto que inclina, no parte del pedido


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


class ChatRequest(BaseModel):
    text: str
    session_id: str = "anon"
    room_id: str = "main"


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

async def _bajar(t: dict) -> "tuple[str | None, str | None]":
    """Baja el archivo y lo registra para el observador.
    Devuelve (path, motivo): path es None si falla, y motivo trae el detalle
    real de la excepción (disco lleno, 403, timeout, POT caído...) para que
    quien llama pueda discriminar la causa en vez de recibir solo None."""
    yid = t.get("youtube_id")
    if not yid:
        logger.error("track sin youtube_id: %s - %s", t.get("artist"), t.get("title"))
        return None, "sin_youtube_id"
    try:
        path = await tracks.obtener_track(yid)
    except tracks.TrackNoDisponible as e:
        logger.error("no se pudo bajar %s - %s: %s", t["artist"], t["title"], e)
        await mark_failed(yid, str(e))
        return None, str(e)
    player.registrar_track(path, yid)
    return str(path), None


async def _encolar(t: dict) -> bool:
    path, _ = await _bajar(t)
    if not path:
        return False
    await player.enqueue_path(path)
    return True


def _clasificar_motivo(msg: "str | None") -> str:
    """Bucketiza el texto crudo de la falla para el detalle del 404.
    Aproximado (yt-dlp no da un código de error estructurado), pero alcanza
    para distinguir 'se llenó el disco' de '403' de 'no hay match'."""
    if not msg:
        return "descarga_fallida"
    m = msg.lower()
    if "no space" in m or "enospc" in m or "espacio" in m:
        return "disco_lleno"
    if "403" in m or "forbidden" in m:
        return "ytdlp_403"
    if "timeout" in m or "timed out" in m:
        return "timeout"
    if "pot" in m or "sabr" in m:
        return "pot_provider"
    if m == "sin_youtube_id":
        return "sin_youtube_id"
    return "descarga_fallida"


async def _resolver_cabeza(propuestos: list[dict], descargar: bool,
                           max_intentos: int = MAX_INTENTOS_CABEZA):
    """Camina `propuestos` hasta encontrar uno que resuelva (y, si
    `descargar`, que además baje). El primero que arranca manda.

    Antes bastaba con que el primer candidato fallara -- por lo que sea, sin
    match en YouTube, disco lleno, 403 -- para tirar 404 aunque hubiera 20
    tracks viables detrás. Los saltados no se descartan: vuelven al frente
    de la cola de background (`resto`) para reintento; un track que falló
    por un timeout de red no está roto para siempre.

    Devuelve (track_resuelto, path, resto, motivos). Si nada sirvió,
    track_resuelto y path son None y `resto` trae todo lo saltado.
    """
    motivos: dict[str, int] = {}
    saltados: list[dict] = []

    for i, t in enumerate(propuestos[:max_intentos]):
        try:
            resuelto = await resolve_track(
                t["artist"], t["title"],
                recording_mbid=t.get("recording_mbid"),
                expected_ms=t.get("length_ms"),
            )
        except Exception:
            logger.warning("cabeza: error resolviendo %s - %s",
                           t.get("artist"), t.get("title"), exc_info=True)
            motivos["error_resolucion"] = motivos.get("error_resolucion", 0) + 1
            saltados.append(t)
            continue

        if not resuelto:
            motivos["sin_match"] = motivos.get("sin_match", 0) + 1
            saltados.append(t)
            continue

        resuelto.pop("cached", None)
        candidato = {**t, **resuelto}

        if not descargar:
            return candidato, None, saltados + propuestos[i + 1:], motivos

        try:
            path, motivo_falla = await _bajar(candidato)
        except Exception:
            logger.warning("cabeza: excepción bajando %s - %s",
                           t.get("artist"), t.get("title"), exc_info=True)
            motivos["error_descarga"] = motivos.get("error_descarga", 0) + 1
            saltados.append(t)
            continue

        if not path:
            clave = _clasificar_motivo(motivo_falla)
            motivos[clave] = motivos.get(clave, 0) + 1
            saltados.append(t)
            continue

        return candidato, path, saltados + propuestos[i + 1:], motivos

    return None, None, saltados, motivos


async def _start_playback(primero_track: dict, primero_path: str,
                          fade: bool) -> list:
    """El primer track ya está resuelto y bajado (lo hizo _resolver_cabeza).
    Solo prepara mpv y arranca."""
    await player.clear_playlist()
    await player.set_video(False)          # música = sin video
    if fade:
        await player.set_volume(0)         # silencio antes del primer track
    await player.play_path(primero_path, replace=True)
    return [primero_track]


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

    cabeza_track, primero_path, resto, motivos = await _resolver_cabeza(
        propuestos, descargar=play_now)
    if not cabeza_track:
        raise HTTPException(404, detail={
            "error": "ningún track de la cabeza se pudo resolver ni descargar",
            "intentados": min(len(propuestos), MAX_INTENTOS_CABEZA),
            "motivos": motivos,
        })

    encolados = [cabeza_track]
    if play_now:
        _cancel_fade()
        _cancel_resto()
        try:
            encolados = await _start_playback(cabeza_track, primero_path, fade)
        except MPVError as e:
            raise HTTPException(503, f"Player not available: {e}")

        if fade:
            _start_fade(fade_target, fade_seconds)

        set_current(playlist_id, encolados)
        _fire(_log_history(encolados, room_id, playlist_id, offset=0))
        if resolver_resto:
            _set_resto(_resolver_resto(resto, playlist_id,
                                       room_id, offset=len(encolados)))

    return {
        "playlist_id": str(playlist_id),
        "title": titulo,
        "source": source,
        "queued": len(encolados),
        "pending": len(resto),
        "first_track": encolados[0],
        "tracks": encolados,
        "proposed": propuestos,
        "resto_no_intentado": resto,
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
                    locales_pendientes=resp["resto_no_intentado"],
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
    metricas = None
    uso_llm = None

    if usar_curador:
        try:
            data = await curate(req.prompt, req.n_tracks,
                                nota=req.sesgo)
            concept = data.get("concept", "")
            narration = data.get("narration", "")
            metricas = data.get("metrics")
            uso_llm = data.get("usage")
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

    resp["usage"] = uso_llm

    logger.info("timing — curador:%.1fs resto:%.1fs total:%.1fs",
                t_cur - t0, time.monotonic() - t_cur, time.monotonic() - t0)

    resp["concept"] = concept
    resp["narration"] = narration
    if metricas:
        resp["verificacion"] = metricas
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


# === Objetivos ===

class ObjetivoRequest(BaseModel):
    room_id: str = "main"
    n_tracks: int = 14
    play_now: bool = True


@app.get("/objetivos", dependencies=[Depends(verify_api_key)])
async def listar_objetivos(room_id: str = "main"):
    return await goals.estado(room_id)


@app.post("/playlist/objetivo", dependencies=[Depends(verify_api_key)])
async def playlist_objetivo(req: ObjetivoRequest):
    """La playlist que más mueve el objetivo más atrasado. Cero tokens.

    Es el caso ideal del sistema: un objetivo declarado en lenguaje natural
    que se cumple con SQL puro, porque son MBIDs concretos que ya están en la
    base. No pasa por el curador ni por una búsqueda en YouTube.
    """
    e = await goals.mas_atrasado(req.room_id)
    if not e:
        raise HTTPException(404, "no hay objetivos pendientes")

    tracks_ = await harness_queries.tracks_para_objetivo(
        e["kind"], e.get("spec"), req.n_tracks)
    if not tracks_:
        raise HTTPException(422, f"sin material para el objetivo {e['kind']}")

    resp = await _lanzar(tracks_, f"Objetivo: {e['kind']}",
                         room_id=req.room_id, source="local",
                         play_now=req.play_now)
    resp["objetivo"] = {k: e[k] for k in ("kind", "actual", "target", "dias")}
    return resp


# === Harness conversacional ===

async def _harness_playlist(prompt: str, room_id: str = "main") -> dict:
    """Adaptador para que el harness reuse /playlist sin duplicar ruteo.

    Menos tracks que por API: en una conversacion la cola larga se vuelve
    ruido y cada track de mas es una descarga en un Pi 3B.
    """
    # El objetivo activo inclina la eleccion del curador. Una linea, no un
    # dump — y solo si hay muestra suficiente: sesgar con ruido es peor que
    # no sesgar. Es una preferencia, no una cuota: si el curador arma
    # playlists peores para cumplir la metrica, se saltean, y los skips
    # envenenan la senal que alimenta local_search.
    sesgo = None
    try:
        e = await goals.mas_atrasado(room_id)
        sesgo = goals.linea_para_curador(e) if e else None
        if sesgo:
            logger.info("sesgo por objetivo: %s", sesgo)
    except Exception:
        logger.exception("no pude leer los objetivos; sigo sin sesgo")

    return await create_playlist(PlaylistRequest(
        prompt=prompt, room_id=room_id,
        n_tracks=settings.harness_n_tracks, sesgo=sesgo))


async def _harness_lanzar(tracks_: list[dict], titulo: str,
                          room_id: str = "main") -> dict:
    """Reproduce tracks que YA salieron de la base, sin pasar por el curador.

    Lo usa `reproducir_historial`: los tracks vienen de play_history con su
    recording_mbid ya resuelto, asi que resolve_tracks pega en la cache y
    arranca al instante en vez de bajar cada tema con yt-dlp.

    source="replay" — es literalmente eso, y `playlists.source` tiene un
    CHECK constraint: no se pueden inventar valores nuevos desde acá.
    """
    return await _lanzar(tracks_, titulo, room_id=room_id, source="replay")



@app.post("/chat", dependencies=[Depends(verify_api_key)])
async def chat(req: ChatRequest):
    """Un turno de conversacion. El transporte (n8n/Telegram) manda el texto
    crudo y no sabe nada de intents; el harness no sabe nada de Telegram.

    Determinista primero: hoy el 100% de los turnos que resuelve son gratis.
    `free: false` en la respuesta es la senal de que un turno gasto tokens.
    """
    texto = (req.text or "").strip()
    if not texto:
        raise HTTPException(400, "mandá 'text'")
    return await harness_responder(texto, req.session_id, req.room_id)


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