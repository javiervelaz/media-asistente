"""Curación sin LLM: rankea el grafo ya hidratado con la señal de play_history.

El corpus es `recordings` (todo lo que el curador hidrató alguna vez desde
MusicBrainz). `play_history` no es el catálogo: es la señal que lo ordena.
"""
import logging

from app.db import fetch, fetchrow

logger = logging.getLogger(__name__)

MIN_TRACKS_HEAD = 5      # mínimo para arrancar sin esperar al curador
MIN_TRACKS_FULL = 8    # mínimo para saltear el curador por completo
SIM_THRESHOLD = 0.5    # umbral de similitud trigram contra artists.name
MAX_POR_ARTISTA = 7      # evita que un solo match devuelva 20 temas iguales

#: Si el prompt es practicamente el nombre de un artista, el pedido es
#: MONOGRAFICO: "pone the Beatles" pide Beatles, no su escena. Ahi la
#: expansion por grafo esta de mas y hace dano — el scoring premia
#: procedencia (hasta 4.5) y cache (1.5) mucho mas que la relevancia al
#: pedido (3.0 * afinidad), asi que un disco de John Lennon que tenes en
#: vinilo y ya cacheado le gana a cualquier Beatle. Pediste Beatles.
MONOGRAFICO_SIM = 0.7


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
        WHERE NOT $5
        UNION ALL
        SELECT ar.source_mbid, s.sim, 0.5
        FROM artist_relations ar JOIN semilla s ON ar.target_mbid = s.mbid
        WHERE NOT $5
    ) x
    GROUP BY mbid
),
peso AS (
    SELECT mbid::uuid AS release_mbid, min(weight) AS weight
    FROM ephemerides WHERE mbid IS NOT NULL GROUP BY 1
),
senal AS (
    SELECT recording_mbid,
           count(*) FILTER (WHERE skipped)   AS skips,
           count(*) FILTER (WHERE completed) AS completos,
           max(started_at)                   AS ultimo
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
        u.afinidad AS afinidad,
        u.sim AS sim,
        ( 3.0 * u.afinidad * u.sim
        + 1.5 * COALESCE(4 - e.weight, 0)
        + 1.2 * (r.position IS NOT NULL AND r.position <= 5)::int
        + 1.5 * (tr.recording_mbid IS NOT NULL)::int
        + 0.8 * LEAST(COALESCE(s.completos, 0), 3)
        + 0.3 * LEAST(COALESCE(tr.play_count, 0), 5)
        - 2.0 * COALESCE(s.skips, 0)
        - 1.5 * (COALESCE(s.ultimo, '1970-01-01'::timestamptz)
                 > now() - interval '14 days')::int
        + random() * 0.4
        ) AS score
    FROM universo u
    JOIN artists    a  ON a.mbid = u.mbid
    JOIN releases   rl ON rl.artist_mbid = a.mbid
    JOIN recordings r  ON r.release_mbid = rl.mbid
    LEFT JOIN senal s  ON s.recording_mbid = r.mbid
    LEFT JOIN track_resolutions tr ON tr.recording_mbid = r.mbid
    LEFT JOIN peso e ON e.release_mbid = rl.mbid
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
WHERE rn <= CASE WHEN afinidad >= 1.0 AND sim >= 0.6 THEN $3
                 WHEN afinidad >= 1.0 THEN 2
                 ELSE 2 END
ORDER BY score DESC
LIMIT $4
"""


async def es_monografico(prompt: str) -> bool:
    """¿El prompt es el nombre de un artista y nada más?

    "the Beatles" sí; "post-punk británico de 1980" no. Decide si se expande
    por el grafo o no, que es la diferencia entre curar una escena y
    responder lo que se pidió.
    """
    if not prompt or len(prompt.strip()) < 3:
        return False
    try:
        row = await fetchrow(
            "SELECT similarity(name, $1) AS sim FROM artists "
            "WHERE name % $1 ORDER BY sim DESC LIMIT 1", prompt.strip())
    except Exception:
        logger.exception("no pude decidir si el pedido es monográfico")
        return False
    return bool(row and (row["sim"] or 0) >= MONOGRAFICO_SIM)


async def buscar(prompt: str, limite: int = 20,
                 monografico: bool | None = None) -> list[dict]:
    """Tracks rankeados desde el grafo local.

    `monografico=None` autodetecta. En modo monográfico no se expande por
    `artist_relations`: pediste un artista, no su árbol genealógico.

    Lista vacía = sin señal suficiente, que decida el curador.
    Nunca levanta: cualquier error cae al curador de forma silenciosa.
    """
    if monografico is None:
        monografico = await es_monografico(prompt)
    # En un pedido monográfico el tope por artista sobra: que sean todos del
    # mismo es exactamente lo que se pidió.
    tope = limite if monografico else MAX_POR_ARTISTA
    try:
        rows = await fetch(SQL, prompt, SIM_THRESHOLD, tope, limite, monografico)
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

    logger.info("local_search %r → %d tracks (%d ya resueltos)%s",
                prompt, len(tracks), sum(1 for t in tracks if t["cached"]),
                " [monográfico]" if monografico else "")
    return tracks


def clasificar(tracks: list[dict]) -> str:
    """local | hybrid | curator según cuántos candidatos hay."""
    if len(tracks) >= MIN_TRACKS_FULL:
        return "local"
    if len(tracks) >= MIN_TRACKS_HEAD:
        return "hybrid"
    return "curator"