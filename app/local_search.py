"""Curación sin LLM: rankea el grafo ya hidratado con la señal de play_history.

El corpus es `recordings` (todo lo que el curador hidrató alguna vez desde
MusicBrainz). `play_history` no es el catálogo: es la señal que lo ordena.
"""
import logging

from app.db import fetch

logger = logging.getLogger(__name__)

MIN_TRACKS_HEAD = 5      # mínimo para arrancar sin esperar al curador
MIN_TRACKS_FULL = 15     # mínimo para saltear el curador por completo
SIM_THRESHOLD = 0.35     # umbral de similitud trigram contra artists.name
MAX_POR_ARTISTA = 3      # evita que un solo match devuelva 20 temas iguales


SQL = """
WITH semilla AS (
    -- Artistas que matchean el prompt por trigram
    SELECT mbid, similarity(name, $1) AS sim
    FROM artists
    WHERE name % $1 AND similarity(name, $1) >= $2
    ORDER BY sim DESC
    LIMIT 5
),
universo AS (
    -- Semilla + relacionados en ambas direcciones (miembros, side projects,
    -- colaboraciones). artist_relations guarda la relación una sola vez.
    SELECT mbid, max(sim) AS sim, max(afinidad) AS afinidad
    FROM (
        SELECT mbid, sim, 1.0 AS afinidad FROM semilla
        UNION ALL
        SELECT ar.target_mbid, s.sim, 0.5
        FROM artist_relations ar JOIN semilla s ON ar.source_mbid = s.mbid
        UNION ALL
        SELECT ar.source_mbid, s.sim, 0.5
        FROM artist_relations ar JOIN semilla s ON ar.target_mbid = s.mbid
    ) x
    GROUP BY mbid
),
peso AS (
    -- Agregado: un artista con N discos en ephemerides no debe multiplicar filas.
    -- weight 1 = colección de vinilos, 2 = canon, 3 = descubierto por uso.
    SELECT lower(artist) AS nombre, min(weight) AS weight
    FROM ephemerides
    GROUP BY 1
),
senal AS (
    SELECT recording_mbid,
           count(*) FILTER (WHERE skipped)                       AS skips,
           count(*) FILTER (WHERE NOT skipped AND played_ms > 0) AS completos,
           max(started_at)                                       AS ultimo
    FROM play_history
    WHERE recording_mbid IS NOT NULL
    GROUP BY recording_mbid
),
candidatos AS (
    -- Dedup por (artista, título): el mismo tema vive en N releases.
    -- El desempate elige QUÉ VERSIÓN, no qué tan buena es la canción.
    SELECT DISTINCT ON (a.mbid, lower(r.title))
        r.mbid      AS recording_mbid,
        a.mbid      AS artist_mbid,
        a.name      AS artist,
        r.title     AS title,
        r.length_ms AS length_ms,
        (tr.recording_mbid IS NOT NULL) AS cached,
        ( 3.0 * u.afinidad * u.sim
        + 1.0 * COALESCE(4 - e.weight, 0)
        + 1.5 * (tr.recording_mbid IS NOT NULL)::int
        + 0.8 * LEAST(COALESCE(s.completos, 0), 3)
        + 0.3 * LEAST(COALESCE(tr.play_count, 0), 5)
        - 2.0 * COALESCE(s.skips, 0)
        - 1.5 * (COALESCE(s.ultimo, '1970-01-01'::timestamptz)
                 > now() - interval '14 days')::int
        + random() * 0.6
        ) AS score
    FROM universo u
    JOIN artists    a  ON a.mbid = u.mbid
    JOIN releases   rl ON rl.artist_mbid = a.mbid
    JOIN recordings r  ON r.release_mbid = rl.mbid
    LEFT JOIN senal s  ON s.recording_mbid = r.mbid
    LEFT JOIN track_resolutions tr ON tr.recording_mbid = r.mbid
    LEFT JOIN peso  e  ON e.nombre = lower(a.name)
    WHERE COALESCE(s.skips, 0) < 2
      AND COALESCE(tr.fail_count, 0) < 2
      AND (r.length_ms IS NULL OR r.length_ms BETWEEN 60000 AND 900000)
      AND rl.primary_type = 'Album'
      AND NOT ('Compilation' = ANY(rl.secondary_types))
    ORDER BY a.mbid, lower(r.title),
             (tr.recording_mbid IS NOT NULL) DESC,
             COALESCE(tr.fail_count, 0) ASC,
             rl.first_release_date ASC NULLS LAST
),
topeado AS (
    SELECT *,
           row_number() OVER (PARTITION BY artist_mbid ORDER BY score DESC) AS rn
    FROM candidatos
)
SELECT recording_mbid, artist_mbid, artist, title, length_ms, cached
FROM topeado
WHERE rn <= $3
ORDER BY score DESC
LIMIT $4
"""


async def buscar(prompt: str, limite: int = 20) -> list[dict]:
    """Tracks rankeados desde el grafo local.

    Lista vacía = sin señal suficiente, que decida el curador.
    Nunca levanta: cualquier error cae al curador de forma silenciosa.
    """
    try:
        rows = await fetch(SQL, prompt, SIM_THRESHOLD, MAX_POR_ARTISTA, limite)
    except Exception:
        logger.exception("local_search falló, cae al curador")
        return []

    tracks = [{
        "artist": r["artist"],
        "title": r["title"],
        "recording_mbid": str(r["recording_mbid"]) if r["recording_mbid"] else None,
        "artist_mbid": str(r["artist_mbid"]) if r["artist_mbid"] else None,
        "length_ms": r["length_ms"],
        "rationale": f"De tu historial: {r['artist']}",
        "cached": bool(r["cached"]),
    } for r in rows]

    logger.info("local_search %r → %d tracks (%d ya resueltos)",
                prompt, len(tracks), sum(1 for t in tracks if t["cached"]))
    return tracks


def clasificar(tracks: list[dict]) -> str:
    """local | hybrid | curator según cuántos candidatos hay."""
    if len(tracks) >= MIN_TRACKS_FULL:
        return "local"
    if len(tracks) >= MIN_TRACKS_HEAD:
        return "hybrid"
    return "curator"