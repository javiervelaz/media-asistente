"""Señal de feedback: qué se salteó y qué se escuchó entero"""
import asyncio
import logging

from app.db import execute
from app.player import MPVError, get_status

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


def get_track_at(pos: int | None) -> dict | None:
    """Track de la playlist en curso según la posición de mpv."""
    if pos is None or pos < 0:
        return None
    tracks = _current.get("tracks") or []
    return tracks[pos] if pos < len(tracks) else None


async def register_advance(motivo: str = "next") -> None:
    """Llamar ANTES de saltar de track. Una sola lectura del socket.

    Matchea por (playlist_id, position). Matchear por (artist, title)
    pisaba las filas duplicadas cuando un tema aparecía dos veces.
    """
    if not _current["playlist_id"]:
        return

    try:
        st = await asyncio.to_thread(get_status)
    except MPVError:
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