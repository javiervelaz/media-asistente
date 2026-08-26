"""Señal de feedback: qué se salteó y qué se escuchó entero"""
import logging

from app import player
from app.db import execute

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