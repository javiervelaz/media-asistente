"""Resolución de tracks en YouTube Music, con cache en Neon"""
import asyncio
import hashlib
import logging

from ytmusicapi import YTMusic

from app.db import execute, fetchrow

logger = logging.getLogger(__name__)

yt = YTMusic()   # sin auth: solo búsqueda pública

# Palabras que descartan un resultado salvo que se pidan explícitamente
RUIDO = ("live", "en vivo", "cover", "remix", "karaoke",
         "instrumental", "reaction", "tribute", "remaster")

MAX_FAILS = 3

# Tolerancia contra la duración de MusicBrainz. Un delta mayor no es "el mismo
# tema con otro fade": es un vivo, un remaster extendido o directamente otra
# canción. La duración es la verificación más fuerte que tenemos.
DELTA_MAX_MS = 20_000


def _hash(artist: str, title: str) -> str:
    key = f"{artist.strip().lower()}|{title.strip().lower()}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _url(video_id: str) -> str:
    """Solo para mostrar/loguear. La reproducción va por tracks.obtener_track()."""
    return f"https://www.youtube.com/watch?v={video_id}"


def _elegir(resultados: list, expected_ms: int | None) -> dict | None:
    """Con duración esperada: descarta lo incompatible y gana el más cercano.
    Sin ella: penaliza ruido.

    Antes esto detectaba el delta grande, escribía un warning y devolvía el
    track igual: por ahí entraban los vivos y los covers pese al filtro RUIDO,
    y encima quedaban cacheados en text_resolutions para siempre.
    """
    candidatos = [r for r in resultados if r.get("videoId")]
    if not candidatos:
        return None

    if expected_ms:
        viables = [
            r for r in candidatos
            if not r.get("duration_seconds")          # sin dato, no lo castigamos
            or abs(r["duration_seconds"] * 1000 - expected_ms) <= DELTA_MAX_MS
        ]
        if not viables:
            cercano = min(
                (r for r in candidatos if r.get("duration_seconds")),
                key=lambda r: abs(r["duration_seconds"] * 1000 - expected_ms),
                default=None)
            delta = (abs(cercano["duration_seconds"] * 1000 - expected_ms) // 1000
                     if cercano else None)
            logger.info("descartado: ningún candidato compatible con %ds "
                        "(el más cercano difería en %ss)",
                        expected_ms // 1000, delta)
            return None       # sin track es mejor que con el track equivocado
        candidatos = viables

    def score(r):
        s = 0.0
        t = (r.get("title") or "").lower()
        if any(w in t for w in RUIDO):
            s += 100
        if expected_ms and r.get("duration_seconds"):
            s += abs(r["duration_seconds"] * 1000 - expected_ms) / 1000
        return s

    return min(candidatos, key=score)


def _buscar_sync(artist: str, title: str) -> list:
    query = f"{artist} {title}"
    try:
        res = yt.search(query, filter="songs", limit=5)
    except Exception as e:
        logger.warning("error buscando %r: %s", query, e)
        return []
    if not res:
        try:
            res = yt.search(query, filter="videos", limit=3)
        except Exception:
            res = []
    return res


def _resultado(artist: str, title: str, yid: str, cached: bool) -> dict:
    return {
        "artist": artist,
        "title": title,
        "youtube_id": yid,
        "url": _url(yid),      # derivado, no usar para reproducir
        "cached": cached,
    }


async def resolve_track(artist: str, title: str,
                        recording_mbid: str | None = None,
                        expected_ms: int | None = None) -> dict | None:
    """Devuelve {artist, title, youtube_id, url, cached} o None."""

    # 1) Cache por MBID
    if recording_mbid:
        row = await fetchrow(
            "SELECT youtube_id FROM track_resolutions "
            "WHERE recording_mbid = $1 AND fail_count < $2",
            recording_mbid, MAX_FAILS)
        if row:
            await execute(
                "UPDATE track_resolutions SET play_count = play_count + 1 "
                "WHERE recording_mbid = $1", recording_mbid)
            return _resultado(artist, title, row["youtube_id"], True)

    # 2) Cache por texto
    qh = _hash(artist, title)
    row = await fetchrow(
        "SELECT youtube_id FROM text_resolutions "
        "WHERE query_hash = $1 AND fail_count < $2", qh, MAX_FAILS)
    if row:
        await execute(
            "UPDATE text_resolutions SET play_count = play_count + 1 "
            "WHERE query_hash = $1", qh)
        return _resultado(artist, title, row["youtube_id"], True)

    # 3) Búsqueda real (bloqueante → fuera del event loop)
    resultados = await asyncio.to_thread(_buscar_sync, artist, title)
    best = _elegir(resultados, expected_ms)
    if not best:
        logger.info("sin match: %s - %s", artist, title)
        return None

    vid = best["videoId"]
    dur = best.get("duration_seconds")

    # fail_count solo se resetea si cambió el youtube_id.
    # Si es el mismo id que ya venía fallando, el contador se preserva.
    await execute(
        """
        INSERT INTO text_resolutions (query_hash, artist, title, youtube_id, duration_s)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (query_hash) DO UPDATE SET
          youtube_id  = EXCLUDED.youtube_id,
          duration_s  = EXCLUDED.duration_s,
          verified_at = now(),
          fail_count  = CASE
            WHEN text_resolutions.youtube_id IS DISTINCT FROM EXCLUDED.youtube_id
            THEN 0 ELSE text_resolutions.fail_count END
        """,
        qh, artist, title, vid, dur,
    )

    if recording_mbid:
        delta = abs(dur * 1000 - expected_ms) if (dur and expected_ms) else None
        # Solo si el recording existe en la tabla (FK)
        existe = await fetchrow("SELECT 1 FROM recordings WHERE mbid = $1", recording_mbid)
        if existe:
            await execute(
                """
                INSERT INTO track_resolutions (recording_mbid, youtube_id, duration_delta)
                VALUES ($1,$2,$3)
                ON CONFLICT (recording_mbid) DO UPDATE SET
                  youtube_id     = EXCLUDED.youtube_id,
                  duration_delta = EXCLUDED.duration_delta,
                  verified_at    = now(),
                  fail_count     = CASE
                    WHEN track_resolutions.youtube_id IS DISTINCT FROM EXCLUDED.youtube_id
                    THEN 0 ELSE track_resolutions.fail_count END
                """,
                recording_mbid, vid, delta,
            )

    return _resultado(artist, title, vid, False)


async def resolve_tracks(tracks: list[dict]) -> list[dict]:
    """[{artist, title, recording_mbid?, length_ms?}, ...] → agrega 'youtube_id'.
    Concurrencia limitada: el Pi 3B no aguanta 20 búsquedas en paralelo."""
    sem = asyncio.Semaphore(4)

    async def uno(t: dict):
        async with sem:
            return await resolve_track(
                t["artist"], t["title"],
                recording_mbid=t.get("recording_mbid"),
                expected_ms=t.get("length_ms"),
            )

    resultados = await asyncio.gather(*[uno(t) for t in tracks],
                                      return_exceptions=True)

    out, cached = [], 0
    for t, r in zip(tracks, resultados):
        if isinstance(r, Exception):
            logger.error("error resolviendo %s - %s: %s", t["artist"], t["title"], r)
            continue
        if r:
            if r.pop("cached", False):
                cached += 1
            out.append({**t, **r})

    logger.info("resueltos %d/%d tracks (%d desde cache)",
                len(out), len(tracks), cached)
    return out


async def mark_failed(youtube_id: str, motivo: str = "") -> None:
    """Llamar cuando la descarga o mpv fallaron.
    A los MAX_FAILS el cache lo ignora y fuerza re-resolución."""
    logger.warning("marcando fallo en %s: %s", youtube_id, motivo or "sin detalle")
    await execute("UPDATE text_resolutions SET fail_count = fail_count + 1 "
                  "WHERE youtube_id = $1", youtube_id)
    await execute("UPDATE track_resolutions SET fail_count = fail_count + 1 "
                  "WHERE youtube_id = $1", youtube_id)