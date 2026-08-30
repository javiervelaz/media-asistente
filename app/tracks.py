"""Descarga y cache local de tracks. Reemplaza el handoff de URLs a mpv."""
import asyncio
import logging
import os
import re
import shutil
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
# Piso de espacio libre real del filesystem, no solo del tamaño del cache:
# el disco se puede llenar por algo ajeno (logs, Postgres local, el SO) y
# ahí MAX_BYTES por sí solo no garantiza que quede lugar para bajar algo.
MIN_FREE_BYTES = int(os.environ.get("TRACK_CACHE_MIN_FREE_MB", "500")) * 1024**2

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

        # Purga preventiva ANTES de intentar bajar. Antes _purgar() solo se
        # disparaba después de una descarga exitosa: con el disco ya lleno
        # eso es un círculo cerrado (hace falta espacio para bajar algo, pero
        # hace falta bajar algo con éxito para liberar espacio) y el sistema
        # no se recupera solo.
        await asyncio.to_thread(_purgar)

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
    """LRU por atime sobre ambos caches.

    Dos condiciones de corte, no una: el tamaño del cache contra MAX_BYTES
    (como antes) y el espacio libre real del filesystem contra
    MIN_FREE_BYTES. La primera no alcanza si el disco se llena por algo que
    no es el cache — ahí el cache puede estar "sano" según su propio límite
    y el disco de todas formas no tener lugar para una descarga nueva.
    """
    files = [p for d in (AUDIO_CACHE, VIDEO_CACHE)
             for p in d.iterdir()
             if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_atime)
    total = sum(f.stat().st_size for f in files)

    def _falta_purgar() -> bool:
        if total > MAX_BYTES:
            return True
        try:
            return shutil.disk_usage(CACHE).free < MIN_FREE_BYTES
        except OSError:
            return False

    while files and _falta_purgar():
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