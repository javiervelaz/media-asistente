"""Descarga y cache local de tracks. Reemplaza el handoff de URLs a mpv."""
import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)

YTDLP = os.environ["YTDLP_BIN"]
RUNTIME = os.environ["YTDLP_JS_RUNTIME"]
CLIENT = os.environ.get("YT_PLAYER_CLIENT", "mweb")
POT = os.environ["POT_PROVIDER_URL"]
CACHE = Path(os.environ["TRACK_CACHE_DIR"])
MAX_BYTES = int(os.environ.get("TRACK_CACHE_GB", "4")) * 1024**3

AUDIO_CACHE = CACHE / "audio"
VIDEO_CACHE = CACHE / "video"
AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
VIDEO_CACHE.mkdir(parents=True, exist_ok=True)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_locks: dict[str, asyncio.Lock] = {}


class TrackNoDisponible(Exception):
    pass


def extraer_id(url: str) -> str | None:
    """Acepta watch?v=, youtu.be/ o el id pelado."""
    if ID_RE.match(url):
        return url
    p = urlparse(url)
    if p.netloc.endswith("youtu.be"):
        cand = p.path.lstrip("/")
    else:
        cand = (parse_qs(p.query).get("v") or [""])[0]
    return cand if ID_RE.match(cand) else None


async def pot_ok() -> bool:
    """El túnel al VPS es un punto de falla silencioso: exponelo en /health."""
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{POT}/ping")
            return r.status_code == 200
    except Exception:
        return False


def _existente(dest_dir: Path, yid: str) -> Path | None:
    """La extensión la decide yt-dlp según el códec: buscamos por prefijo."""
    for p in dest_dir.glob(f"{yid}.*"):
        if not p.name.startswith("."):
            return p
    return None


async def _descargar(yid: str, dest_dir: Path, formato: str,
                     timeout: int) -> Path:
    if not ID_RE.match(yid):
        raise TrackNoDisponible(f"youtube_id inválido: {yid!r}")

    ya = _existente(dest_dir, yid)
    if ya:
        os.utime(ya)
        return ya

    lock = _locks.setdefault(f"{dest_dir.name}:{yid}", asyncio.Lock())
    async with lock:
        ya = _existente(dest_dir, yid)
        if ya:
            return ya

        salida = dest_dir / f".{yid}.part.%(ext)s"
        proc = await asyncio.create_subprocess_exec(
            YTDLP, "--js-runtimes", RUNTIME,
            "-f", formato, "--no-playlist", "--no-progress",
            "--extractor-args", f"youtube:player_client={CLIENT}",
            "--extractor-args", f"youtubepot-bgutilhttp:base_url={POT}",
            "-o", str(salida),
            f"https://www.youtube.com/watch?v={yid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _limpiar(dest_dir, yid)
            raise TrackNoDisponible(f"{yid}: timeout tras {timeout}s")

        parciales = list(dest_dir.glob(f".{yid}.part.*"))
        if proc.returncode != 0 or not parciales:
            _limpiar(dest_dir, yid)
            raise TrackNoDisponible(f"{yid}: {out.decode()[-400:]}")

        origen = parciales[0]
        ext = origen.suffix
        final = dest_dir / f"{yid}{ext}"
        origen.rename(final)
        _limpiar(dest_dir, yid)
        logger.info("bajado %s (%.1f MB)", final.name,
                    final.stat().st_size / 1e6)

    asyncio.create_task(asyncio.to_thread(_purgar))
    return final


def _limpiar(dest_dir: Path, yid: str) -> None:
    for p in dest_dir.glob(f".{yid}.part*"):
        p.unlink(missing_ok=True)


def _purgar() -> None:
    """LRU por atime sobre ambos caches."""
    files = [p for d in (AUDIO_CACHE, VIDEO_CACHE)
             for p in d.iterdir()
             if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_atime)
    total = sum(f.stat().st_size for f in files)
    while total > MAX_BYTES and files:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)
        logger.info("purgado del cache: %s", f.name)


async def obtener_track(yid: str) -> Path:
    return await _descargar(yid, AUDIO_CACHE, "bestaudio", timeout=180)


async def obtener_video(yid: str) -> Path:
    return await _descargar(yid, VIDEO_CACHE,
                            "bestvideo[height<=720]+bestaudio/best",
                            timeout=600)