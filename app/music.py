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


def _hash(artist: str, title: str) -> str:
    key = f"{artist.strip().lower()}|{title.strip().lower()}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _elegir(resultados: list, expected_ms: int | None) -> dict | None:
    """Con duración esperada: gana el más cercano. Sin ella: penaliza ruido."""
    candidatos = [r for r in resultados if r.get("videoId")]
    if not candidatos:
        return None

    def score(r):
        s = 0.0
        t = (r.get("title") or "").lower()
        if any(w in t for w in RUIDO):
            s += 100
        if expected_ms and r.get("duration_seconds"):
            s += abs(r["duration_seconds"] * 1000 - expected_ms) / 1000
        return s

    best = min(candidatos, key=score)
    if expected_ms and best.get("duration_seconds"):
        delta = abs(best["duration_seconds"] * 1000 - expected_ms)
        if delta > 25_000:
            logger.warning("duración sospechosa: %r (delta %ds)",
                           best.get("title"), delta // 1000)
    return best


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


async def resolve_track(artist: str, title: str,
                        recording_mbid: str | None = None,
                        expected_ms: int | None = None) -> dict | None:
    """Devuelve {artist, title, url, cached} o None."""

    # 1) Cache por MBID
    if recording_mbid:
        row = await fetchrow(
            "SELECT youtube_id FROM track_resolutions "
            "WHERE recording_mbid = $1 AND fail_count < 3", recording_mbid)
        if row:
            await execute(
                "UPDATE track_resolutions SET play_count = play_count + 1 "
                "WHERE recording_mbid = $1", recording_mbid)
            return {"artist": artist, "title": title,
                    "url": _url(row["youtube_id"]), "cached": True}

    # 2) Cache por texto
    qh = _hash(artist, title)
    row = await fetchrow(
        "SELECT youtube_id FROM text_resolutions "
        "WHERE query_hash = $1 AND fail_count < 3", qh)
    if row:
        await execute(
            "UPDATE text_resolutions SET play_count = play_count + 1 "
            "WHERE query_hash = $1", qh)
        return {"artist": artist, "title": title,
                "url": _url(row["youtube_id"]), "cached": True}

    # 3) Búsqueda real (bloqueante → fuera del event loop)
    resultados = await asyncio.to_thread(_buscar_sync, artist, title)
    best = _elegir(resultados, expected_ms)
    if not best:
        logger.info("sin match: %s - %s", artist, title)
        return None

    vid = best["videoId"]
    dur = best.get("duration_seconds")

    await execute(
        """
        INSERT INTO text_resolutions (query_hash, artist, title, youtube_id, duration_s)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (query_hash) DO UPDATE SET
          youtube_id  = EXCLUDED.youtube_id,
          duration_s  = EXCLUDED.duration_s,
          verified_at = now(),
          fail_count  = 0
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
                  fail_count     = 0
                """,
                recording_mbid, vid, delta,
            )

    return {"artist": artist, "title": title, "url": _url(vid), "cached": False}


async def resolve_tracks(tracks: list[dict]) -> list[dict]:
    """[{artist, title, recording_mbid?, length_ms?}, ...] → agrega 'url'.
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


async def mark_failed(url: str) -> None:
    """Llamar cuando mpv/yt-dlp no pudo reproducir. A los 3 fallos el cache lo ignora."""
    vid = url.rsplit("v=", 1)[-1]
    await execute("UPDATE text_resolutions SET fail_count = fail_count + 1 "
                  "WHERE youtube_id = $1", vid)
    await execute("UPDATE track_resolutions SET fail_count = fail_count + 1 "
                  "WHERE youtube_id = $1", vid)