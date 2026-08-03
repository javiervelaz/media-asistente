"""Herramientas que el curador ejecuta contra el grafo en Neon.

search_artist y get_recordings hidratan desde MusicBrainz si falta el dato:
la base crece con el uso.
"""
import logging

from app.db import fetch, fetchrow
from app.hydrator import hydrate_recordings, resolve_artist

logger = logging.getLogger(__name__)


async def search_artist(name: str) -> dict:
    a = await resolve_artist(name)
    if not a:
        return {"found": False,
                "message": f"No encontré '{name}' en MusicBrainz. "
                           "Probá otra grafía o el nombre original de la banda."}

    stats = await fetchrow(
        """
        SELECT count(*) AS releases,
               min(EXTRACT(YEAR FROM first_release_date))::int AS desde,
               max(EXTRACT(YEAR FROM first_release_date))::int AS hasta
        FROM releases WHERE artist_mbid = $1
        """, a["mbid"])

    return {
        "found": True,
        "mbid": str(a["mbid"]),
        "name": a["name"],
        "country": a.get("country"),
        "begin_year": a.get("begin_year"),
        "end_year": a.get("end_year"),
        "tags": a.get("tags") or [],
        "releases_en_base": stats["releases"],
        "rango_anios": [stats["desde"], stats["hasta"]],
    }


async def get_artist_graph(mbid: str, rel_types: list[str] | None = None,
                           max_hops: int = 1) -> dict:
    tipos = rel_types or ["member_of_band", "collaboration",
                          "producer", "supporting_musician"]
    rows = await fetch(
        """
        WITH RECURSIVE walk(mbid, hop) AS (
          SELECT $1::uuid, 0
          UNION
          SELECT CASE WHEN ar.source_mbid = w.mbid THEN ar.target_mbid
                      ELSE ar.source_mbid END, w.hop + 1
          FROM walk w
          JOIN artist_relations ar
            ON (ar.source_mbid = w.mbid OR ar.target_mbid = w.mbid)
          WHERE w.hop < $2 AND ar.rel_type = ANY($3)
        )
        SELECT DISTINCT a.mbid, a.name, a.country, a.begin_year, a.end_year,
               w.hop, (a.crawled_at IS NOT NULL) AS hidratado,
               (SELECT count(*) FROM releases r WHERE r.artist_mbid = a.mbid) AS releases
        FROM walk w JOIN artists a ON a.mbid = w.mbid
        WHERE w.hop > 0
        ORDER BY w.hop, a.name
        LIMIT 60
        """,
        mbid, max_hops, tipos)

    out = [{**dict(r), "mbid": str(r["mbid"])} for r in rows]
    pendientes = [r["name"] for r in out if not r["hidratado"]]

    return {
        "conectados": out,
        "nota": (f"{len(pendientes)} de estos artistas todavía no tienen "
                 "discografía cargada. Si alguno te interesa, llamá "
                 "search_artist con su nombre para traerla."
                 if pendientes else None),
    }


async def query_releases(artist_mbids: list[str] | None = None,
                         country: str | None = None,
                         year_from: int | None = None,
                         year_to: int | None = None,
                         tags: list[str] | None = None,
                         limit: int = 50) -> list[dict]:
    rows = await fetch(
        """
        SELECT r.mbid, r.title, r.first_release_date, r.primary_type,
               a.name AS artist, a.mbid AS artist_mbid, a.country, a.tags,
               e.weight
        FROM releases r
        JOIN artists a ON a.mbid = r.artist_mbid
        LEFT JOIN ephemerides e ON e.mbid = r.mbid::text
        WHERE ($1::uuid[] IS NULL OR r.artist_mbid = ANY($1))
          AND ($2::char(2) IS NULL OR a.country = $2)
          AND ($3::int IS NULL OR EXTRACT(YEAR FROM r.first_release_date) >= $3)
          AND ($4::int IS NULL OR EXTRACT(YEAR FROM r.first_release_date) <= $4)
          AND ($5::text[] IS NULL OR a.tags && $5)
          AND NOT ('Compilation' = ANY(r.secondary_types))
          AND r.first_release_date IS NOT NULL
        ORDER BY COALESCE(e.weight, 9), r.first_release_date
        LIMIT $6
        """,
        artist_mbids, country, year_from, year_to, tags, limit)

    return [{**dict(r),
             "mbid": str(r["mbid"]),
             "artist_mbid": str(r["artist_mbid"]),
             "first_release_date": str(r["first_release_date"])}
            for r in rows]


async def get_recordings(release_mbid: str) -> list[dict]:
    rows = await fetch(
        "SELECT mbid, title, position, length_ms FROM recordings "
        "WHERE release_mbid = $1 ORDER BY position", release_mbid)
    if rows:
        return [{**dict(r), "mbid": str(r["mbid"])} for r in rows]
    return await hydrate_recordings(release_mbid)


async def get_play_history(days: int = 30) -> list[dict]:
    rows = await fetch(
        """
        SELECT artist, title, count(*) AS plays,
               sum(CASE WHEN skipped THEN 1 ELSE 0 END) AS skips,
               max(started_at)::date::text AS ultima
        FROM play_history
        WHERE started_at > now() - ($1 || ' days')::interval
        GROUP BY artist, title
        ORDER BY max(started_at) DESC
        LIMIT 40
        """, days)
    return [dict(r) for r in rows]


TOOL_IMPL = {
    "search_artist": search_artist,
    "get_artist_graph": get_artist_graph,
    "query_releases": query_releases,
    "get_recordings": get_recordings,
    "get_play_history": get_play_history,
}