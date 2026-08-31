"""Señal de feedback: qué se salteó y qué se escuchó entero"""
import logging

from pathlib import Path

from app import player
from app.db import execute, fetch

logger = logging.getLogger(__name__)

SKIP_THRESHOLD_S = 30

# Playlist en curso. Se pisa en cada /playlist nuevo.
# El índice de esta lista tiene que coincidir con playlist-pos de mpv
# y con play_history.position: solo entran tracks RESUELTOS y encolados.
_current: dict = {"playlist_id": None, "tracks": []}


def set_current(playlist_id, tracks: list[dict]) -> None:
    """Se llama al arrancar cada playlist nueva y al encolar el resto."""
    _current["playlist_id"] = playlist_id
    _current["tracks"] = list(tracks)


def append_current(tracks: list[dict]) -> None:
    """Suma los tracks que llegaron por la resolución en background."""
    _current["tracks"].extend(tracks)


def get_current() -> dict:
    return _current


def find_by_youtube_id(yid: str) -> dict | None:
    """Track de la playlist en curso por youtube_id. Usado por el eof, que
    no puede confiar en playlist-pos: el evento llega antes de que avance."""
    for t in _current.get("tracks") or []:
        if t.get("youtube_id") == yid:
            return t
    return None


def get_track_at(pos: int | None) -> dict | None:
    """Track de la playlist en curso según la posición de mpv."""
    if pos is None or pos < 0:
        return None
    tracks = _current.get("tracks") or []
    return tracks[pos] if pos < len(tracks) else None


async def register_complete(youtube_id: str) -> None:
    """El track llego al final: senal positiva.

    Se dispara desde el observador de mpv con reason=eof, que antes se
    descartaba. Matchea por youtube_id y no por posicion porque el evento
    llega antes de que mpv avance playlist-pos. Si el mismo track aparece
    dos veces en la playlist marca la primera ocurrencia sin marcar.
    """
    if not _current["playlist_id"] or not youtube_id:
        return

    t = find_by_youtube_id(youtube_id)
    length_ms = (t or {}).get("length_ms")

    try:
        marcadas = await execute(
            """
            UPDATE play_history
            SET completed = true,
                skipped   = false,
                played_ms = COALESCE(played_ms, $3)
            WHERE id = (
                SELECT id FROM play_history
                WHERE playlist_id = $1
                  AND youtube_id = $2
                  AND NOT completed
                  AND skipped IS DISTINCT FROM true
                ORDER BY position NULLS LAST, id
                LIMIT 1
            )
            """,
            _current["playlist_id"], youtube_id, length_ms,
        )
        if marcadas.endswith("0"):
            logger.debug("eof sin fila que marcar: %s", youtube_id)
        elif t:
            logger.info("completo: %s - %s", t.get("artist"), t.get("title"))
    except Exception:
        logger.exception("no pude registrar la reproduccion completa")


async def register_advance(motivo: str = "next") -> None:
    """Llamar ANTES de saltar de track. Una sola lectura del socket.

    Matchea por (playlist_id, position). Matchear por (artist, title)
    pisaba las filas duplicadas cuando un tema aparecía dos veces.
    """
    if not _current["playlist_id"]:
        return

    st = await player.get_status()
    if not st.get("mpv_ok"):
        logger.debug("mpv no disponible para el feedback")
        return

    pos = st.get("playlist_pos")
    elapsed = st.get("position_sec") or 0

    if pos is None or pos < 0 or pos >= len(_current["tracks"]):
        return

    t = _current["tracks"][pos]
    skipped = motivo == "next" and elapsed < SKIP_THRESHOLD_S

    try:
        await execute(
            """
            UPDATE play_history
            SET played_ms = $1, skipped = $2
            WHERE playlist_id = $3 AND position = $4
            """,
            int(elapsed * 1000), skipped,
            _current["playlist_id"], int(pos),
        )
        if skipped:
            logger.info("skip: %s - %s (%.0fs)",
                        t.get("artist"), t.get("title"), elapsed)
    except Exception:
        logger.exception("no pude registrar el feedback")


async def restaurar_current() -> int:
    """Reconstruye `_current` desde play_history al arrancar el servicio.

    mpv sobrevive a un `systemctl restart`, el proceso de Python no. Sin esto,
    reiniciar con musica sonando deja `_current` vacio y **toda la senal de
    esa playlist se pierde en silencio**: `register_advance` no encuentra el
    track y el skip no se registra, `register_complete` tampoco, y `/status`
    devuelve el nombre del archivo (`dGsHLKyZ8H8.webm`) en vez del tema.

    El ORDEN lo manda mpv —es la unica fuente confiable despues del
    reinicio— y la metadata sale de play_history, matcheando por youtube_id.
    Si mpv no tiene cola o ninguna fila matchea, no restaura nada: es mejor
    quedarse sin historial que atribuir escuchas al track equivocado.

    Devuelve cuantos tracks se restauraron.
    """
    try:
        paths = await player.get_playlist()
    except Exception:
        logger.exception("no pude leer la cola de mpv")
        return 0
    if not paths:
        return 0

    # Los archivos se guardan como <youtube_id>.<ext> en el cache.
    yids = [Path(p).stem for p in paths if p]
    if not yids:
        return 0

    try:
        rows = await fetch(
            """
            SELECT DISTINCT ON (youtube_id)
                   youtube_id, playlist_id, artist, title, rationale,
                   recording_mbid, artist_mbid
            FROM play_history
            WHERE youtube_id = ANY($1::text[])
            ORDER BY youtube_id, started_at DESC NULLS LAST, id DESC
            """, yids)
    except Exception:
        logger.exception("no pude leer play_history para restaurar")
        return 0

    por_yid = {r["youtube_id"]: dict(r) for r in rows}
    if not por_yid:
        logger.info("cola de mpv sin correspondencia en play_history "
                    "(%d entradas); no restauro", len(yids))
        return 0

    # La playlist_id de la mayoria: mpv puede tener restos de una anterior.
    conteo: dict = {}
    for r in por_yid.values():
        pid = r["playlist_id"]
        conteo[pid] = conteo.get(pid, 0) + 1
    playlist_id = max(conteo, key=conteo.get)

    tracks, restaurados = [], 0
    for path, yid in zip(paths, yids):
        r = por_yid.get(yid)
        if r and r["playlist_id"] == playlist_id:
            tracks.append({
                "artist": r["artist"], "title": r["title"],
                "rationale": r["rationale"],
                "recording_mbid": str(r["recording_mbid"]) if r["recording_mbid"] else None,
                "artist_mbid": str(r["artist_mbid"]) if r["artist_mbid"] else None,
                "youtube_id": yid,
            })
            # Sin esto el observador no puede atribuir el eof y la senal
            # positiva se pierde igual.
            player.registrar_track(path, yid)
            restaurados += 1
        else:
            # Un hueco romperia la correspondencia con playlist-pos, asi que
            # se rellena con un placeholder en vez de saltearlo.
            tracks.append({"artist": None, "title": Path(path).name,
                           "youtube_id": yid})

    set_current(playlist_id, tracks)
    logger.info("_current restaurado: %d de %d tracks de la playlist %s",
                restaurados, len(tracks), playlist_id)
    return restaurados
