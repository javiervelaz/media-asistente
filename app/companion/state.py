"""Arma el view state: lo que la pantalla tiene que mostrar, ya digerido.

La caja no sabe de mpv, de Neon ni de MusicBrainz. Recibe un JSON chico con
todo resuelto (titulo, tapa, humor) y lo dibuja. Asi el firmware queda tonto
y barato, y toda la logica se itera aca con un `git pull`.

Fuentes, en orden de confianza:
  1. mpv (`player.get_status` + el `path` que sigue el observador)
  2. la playlist en memoria (`history.get_track_at`) — artista/titulo/rationale
  3. el grafo en Neon via `track_resolutions` — album, año, tapa, vinilo
  4. si el track no tiene mbid (vino por texto), la miniatura de YouTube
"""
import logging
from datetime import datetime

from app import history, player
from app.db import fetchrow

logger = logging.getLogger(__name__)

#: Tapa del release-group en Cover Art Archive. Se usa el release-group y no
#: el release porque tiene mucha mas cobertura: cualquier edicion con tapa
#: sirve. 500 px alcanza para 480x480; la placa la achica en A1.
CAA_URL = "https://coverartarchive.org/release-group/{rg}/front-500"
YT_THUMB = "https://i.ytimg.com/vi/{yid}/hqdefault.jpg"

SQL_META = """
SELECT rl.title                                      AS album,
       rl.release_group_mbid::text                   AS rg,
       EXTRACT(YEAR FROM rl.first_release_date)::int AS anio,
       a.name                                        AS artista,
       r.title                                       AS titulo,
       EXISTS (SELECT 1 FROM ephemerides e
               WHERE e.mbid = rl.mbid::text AND e.weight = 1)          AS de_vinilo,
       EXISTS (SELECT 1 FROM ephemerides e
               WHERE e.weight = 1 AND lower(e.artist) = lower(a.name)) AS artista_coleccion
FROM track_resolutions tr
JOIN recordings r ON r.mbid = tr.recording_mbid
JOIN releases  rl ON rl.mbid = r.release_mbid
JOIN artists    a ON a.mbid = rl.artist_mbid
WHERE tr.youtube_id = $1
LIMIT 1
"""

#: youtube_id -> metadata (o {} si no hay nada en el grafo). La Pi 3B tiene
#: 1 GB: con un techo bajo alcanza, porque solo importa el track en curso y
#: los que van a volver a sonar en la misma playlist.
_cache: dict[str, dict] = {}
MAX_CACHE = 200


async def metadata(yid: str) -> dict:
    """Album, año, tapa y procedencia de un youtube_id. Nunca levanta: si la
    base falla, la pantalla muestra menos, pero se muestra."""
    if yid in _cache:
        return _cache[yid]
    meta: dict = {}
    try:
        row = await fetchrow(SQL_META, yid)
    except Exception as e:      # Neon dormido, pooler caido: no es motivo de crash
        logger.warning("companion: no pude leer metadata de %s: %s", yid, e)
        return {}               # sin cachear: el proximo poll reintenta
    if row:
        meta = dict(row)
    _cache[yid] = meta
    while len(_cache) > MAX_CACHE:
        _cache.pop(next(iter(_cache)))
    return meta


def _tapa(meta: dict, yid: str | None) -> tuple[str | None, str | None]:
    if meta.get("rg"):
        return CAA_URL.format(rg=meta["rg"]), "caa"
    if yid:
        return YT_THUMB.format(yid=yid), "youtube"
    return None, None


async def leer() -> dict:
    """Snapshot crudo del reproductor + metadata. Sin humor: eso lo agrega el
    hub, que es el que lleva la memoria (desde cuando hay silencio)."""
    st = await player.get_status()
    if not st.get("mpv_ok"):
        return {"mode": "offline", "track": None, "playback": None}

    pos = st.get("playlist_pos")
    hay_track = (pos is not None and pos >= 0
                 and (st.get("playlist_count") or 0) > 0)
    if not hay_track:
        return {"mode": "idle", "track": None, "playback": None}

    path = player.track_en_curso().get("path") or ""
    yid = player._yid_de_path(path)
    t = history.get_track_at(pos) or {}
    # El path del observador puede ir un evento atrasado respecto de
    # playlist-pos; si la playlist en memoria dice otra cosa, gana ella.
    if t.get("youtube_id"):
        yid = t["youtube_id"]

    meta = await metadata(yid) if yid else {}
    tapa, fuente = _tapa(meta, yid)

    track = {
        "youtube_id": yid,
        "title": meta.get("titulo") or t.get("title") or st.get("title"),
        "artist": meta.get("artista") or t.get("artist"),
        "album": meta.get("album"),
        "year": meta.get("anio"),
        "cover": tapa,
        "cover_source": fuente,
        "from_vinyl": bool(meta.get("de_vinilo")),
        "artist_in_collection": bool(meta.get("artista_coleccion")),
        "rationale": t.get("rationale"),
    }
    playback = {
        "paused": bool(st.get("paused")),
        "position_sec": st.get("position_sec"),
        "duration_sec": st.get("duration_sec"),
        "volume": st.get("volume"),
    }
    return {"mode": "now_playing", "track": track, "playback": playback}


def ahora_local(tz) -> datetime:
    return datetime.now(tz)
