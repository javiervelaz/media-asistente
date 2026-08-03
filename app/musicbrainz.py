"""Cliente HTTP de MusicBrainz con rate limit estricto de 1 req/s"""
import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE = "https://musicbrainz.org/ws/2"


class MBClient:
    """Un solo cliente compartido. El lock serializa TODAS las requests:
    MusicBrainz permite 1 req/s y bloquea por IP si te pasás."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": settings.mb_user_agent},
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    async def get(self, path: str, retries: int = 2, **params) -> dict:
        params["fmt"] = "json"
        client = await self._get_client()

        for intento in range(retries + 1):
            async with self._lock:
                resp = await client.get(f"{BASE}/{path}", params=params)
                await asyncio.sleep(1.1)      # el sleep va DENTRO del lock

            if resp.status_code == 503:
                espera = 5 * (intento + 1)
                logger.warning("MB 503, reintento en %ds", espera)
                await asyncio.sleep(espera)
                continue
            if resp.status_code == 404:
                raise LookupError(f"MB 404: {path}")

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError(f"MB no respondió tras {retries + 1} intentos: {path}")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


mb = MBClient()