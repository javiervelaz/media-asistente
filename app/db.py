"""Pool asyncpg contra Neon"""
import logging
import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=0,
            max_size=4,                # Pi 3B: no te pases
            command_timeout=30,
            statement_cache_size=0,    # obligatorio con el pooler de Neon
        )
        logger.info("pool de Neon inicializado")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("pool cerrado")


async def fetch(query: str, *args):
    pool = await init_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args):
    pool = await init_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args):
    pool = await init_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args) -> str:
    pool = await init_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)