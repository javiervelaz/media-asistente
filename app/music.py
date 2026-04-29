"""Búsqueda de tracks en YouTube Music"""
import logging

from ytmusicapi import YTMusic

logger = logging.getLogger(__name__)

# Sin auth: solo búsqueda pública
yt = YTMusic()


def find_youtube_url(artist: str, title: str) -> str | None:
    """Busca un track y devuelve URL de YouTube reproducible, o None si no encuentra"""
    query = f"{artist} {title}"
    try:
        results = yt.search(query, filter="songs", limit=3)
    except Exception as e:
        logger.warning(f"Error buscando {query!r}: {e}")
        return None

    if not results:
        # Fallback: probar sin filter songs (a veces matchea videos)
        try:
            results = yt.search(query, filter="videos", limit=3)
        except Exception:
            results = []

    if not results:
        logger.warning(f"No encontré {query!r} en YouTube")
        return None

    video_id = results[0].get("videoId")
    if not video_id:
        return None

    url = f"https://www.youtube.com/watch?v={video_id}"
    logger.debug(f"Match: {query!r} → {url}")
    return url


def resolve_tracks(tracks: list[dict]) -> list[dict]:
    """Toma [{artist, title}, ...] y agrega 'url' a los que se pudieron resolver"""
    resolved = []
    for t in tracks:
        url = find_youtube_url(t["artist"], t["title"])
        if url:
            resolved.append({**t, "url": url})
        else:
            logger.info(f"Skip: {t['artist']} - {t['title']}")
    logger.info(f"Resueltos {len(resolved)}/{len(tracks)} tracks")
    return resolved