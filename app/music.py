"""Búsqueda de tracks en YouTube Music"""
import logging
import asyncio

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

async def ramp_volume(target: int, seconds: float, steps: int = 30):
    """Rampa lineal de volumen vía IPC mpv."""
    try:
        cur = await mpv_get_property("volume")  # tu helper IPC actual
    except Exception:
        cur = 0
    cur = int(cur or 0)
    delta = (target - cur) / steps
    for i in range(1, steps + 1):
        vol = int(cur + delta * i)
        await mpv_command(["set_property", "volume", max(0, min(100, vol))])
        await asyncio.sleep(seconds / steps)


async def start_playlist(tracks: list[str], fade: bool = False,
                         target_vol: int = 70):
    if not tracks:
        return

    if fade:
        # ¿hay algo sonando? Si sí, lo bajamos antes de pisar la cola.
        try:
            idle = await mpv_get_property("core-idle")  # True = no reproduce
        except Exception:
            idle = True
        if not idle:
            await ramp_volume(0, seconds=3)

        await mpv_command(["set_property", "volume", 0])  # arrancar en silencio

    # carga de cola (tu lógica de siempre)
    await mpv_command(["loadfile", tracks[0], "replace"])
    for t in tracks[1:]:
        await mpv_command(["loadfile", t, "append"])
    await mpv_command(["set_property", "pause", False])   # quirk conocido tuyo

    if fade:
        await ramp_volume(target_vol, seconds=30)         # fade-in lento de despertador
    else:
        await mpv_command(["set_property", "volume", target_vol])