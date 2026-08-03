"""Hidratación perezosa: la base crece por uso.

Si un artista no está localmente, se busca en MusicBrainz, se inserta en el
grafo (artists / releases / artist_relations) y además en ephemerides con
weight=3 para compatibilidad con el despertador viejo.
"""
import asyncio
import logging

from datetime import date
from app.db import execute, fetch, fetchrow
from app.musicbrainz import mb

logger = logging.getLogger(__name__)

# Qué releases guardamos
KEEP_PRIMARY = {"Album", "EP"}
SKIP_SECONDARY = {
    "Compilation", "Live", "Remix", "Soundtrack",
    "Interview", "Demo", "DJ-mix", "Mixtape/Street", "Spokenword",
}

# Qué relaciones nos interesan (clave = tipo de MB, valor = normalizado)
REL_TYPES = {
    "member of band": "member_of_band",
    "collaboration": "collaboration",
    "producer": "producer",
    "instrumental supporting musician": "supporting_musician",
    "vocal supporting musician": "supporting_musician",
}

# Procedencia
WEIGHT_COLECCION = 1   # vinilos
WEIGHT_CANON = 2       # los 808 originales
WEIGHT_DESCUBIERTO = 3 # traído por uso

_locks: dict[str, asyncio.Lock] = {}


def _lock_for(mbid: str) -> asyncio.Lock:
    """Evita hidratar el mismo artista dos veces en paralelo."""
    if mbid not in _locks:
        _locks[mbid] = asyncio.Lock()
    return _locks[mbid]


def _year(s: str | None) -> int | None:
    return int(s[:4]) if s and len(s) >= 4 and s[:4].isdigit() else None


def _norm_date(s: str | None) -> date | None:
    """MusicBrainz da '1979', '1979-08' o '1979-08-30'. Devolvemos date."""
    if not s:
        return None
    partes = s.split("-")
    if len(partes) == 1:
        iso = f"{partes[0]}-01-01"
    elif len(partes) == 2:
        iso = f"{s}-01"
    else:
        iso = s
    try:
        return date.fromisoformat(iso)
    except ValueError:
        logger.warning("fecha inválida de MB: %r", s)
        return None


def _month_day(raw: str | None) -> str | None:
    """month_day solo si la fecha original tenía día. Si no, NULL."""
    return raw[5:] if raw and len(raw) == 10 else None


# === Búsqueda ===

async def find_local(name: str) -> list[dict]:
    rows = await fetch(
        """
        SELECT mbid, name, country, begin_year, end_year, tags, crawled_at,
               similarity(name, $1) AS sim
        FROM artists
        WHERE name % $1 OR lower(name) = lower($1)
        ORDER BY (lower(name) = lower($1)) DESC, sim DESC
        LIMIT 5
        """,
        name,
    )
    return [dict(r) for r in rows]


async def search_mb(name: str) -> dict | None:
    """Busca en MusicBrainz. Devuelve None si el match no es confiable."""
    try:
        res = await mb.get("artist", query=name, limit=5)
    except Exception as e:
        logger.error("MB search falló para %r: %s", name, e)
        return None

    hits = res.get("artists", [])
    if not hits:
        return None

    exactos = [h for h in hits if h["name"].lower() == name.lower().strip()]
    best = exactos[0] if exactos else hits[0]

    if best.get("score", 0) < 85:
        logger.warning("match débil %r → %r (score %s)",
                       name, best["name"], best.get("score"))
        return None
    return best


# === Hidratación ===

async def hydrate_artist(mbid: str, weight: int = WEIGHT_DESCUBIERTO,
                         force: bool = False) -> dict:
    """Trae artista + discografía + relaciones. Idempotente."""
    async with _lock_for(mbid):
        if not force:
            row = await fetchrow("SELECT crawled_at FROM artists WHERE mbid = $1", mbid)
            if row and row["crawled_at"]:
                return {"mbid": mbid, "cached": True}

        data = await mb.get(f"artist/{mbid}", inc="artist-rels+release-groups+tags")

        life = data.get("life-span") or {}
        tags = [t["name"] for t in data.get("tags", []) if t.get("count", 0) > 0]

        await execute(
            """
            INSERT INTO artists (mbid, name, sort_name, country, type,
                                 begin_year, end_year, tags, crawled_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now())
            ON CONFLICT (mbid) DO UPDATE SET
              name       = EXCLUDED.name,
              sort_name  = EXCLUDED.sort_name,
              country    = EXCLUDED.country,
              type       = EXCLUDED.type,
              begin_year = EXCLUDED.begin_year,
              end_year   = EXCLUDED.end_year,
              tags       = EXCLUDED.tags,
              crawled_at = now()
            """,
            mbid, data["name"], data.get("sort-name"), data.get("country"),
            data.get("type"), _year(life.get("begin")), _year(life.get("end")), tags,
        )

        n_rel = await _insert_relations(mbid, data.get("relations", []))
        n_rg = await _insert_releases(mbid, data["name"],
                                      data.get("release-groups", []), weight)

        logger.info("hidratado %s (%s): %d releases, %d relaciones",
                    data["name"], mbid, n_rg, n_rel)
        return {"mbid": mbid, "name": data["name"],
                "releases": n_rg, "relations": n_rel, "cached": False}


async def _insert_relations(mbid: str, relations: list) -> int:
    n = 0
    for rel in relations:
        target = rel.get("artist")
        if not target:
            continue
        rt = REL_TYPES.get(rel.get("type", ""))
        if not rt:
            continue

        # Nodo pelado: solo mbid + nombre. Se hidrata si alguien lo pide.
        await execute(
            "INSERT INTO artists (mbid, name) VALUES ($1,$2) ON CONFLICT (mbid) DO NOTHING",
            target["id"], target["name"],
        )
        await execute(
            """
            INSERT INTO artist_relations
              (source_mbid, target_mbid, rel_type, begin_year, end_year, attributes)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (source_mbid, target_mbid, rel_type) DO NOTHING
            """,
            mbid, target["id"], rt,
            _year(rel.get("begin")), _year(rel.get("end")),
            rel.get("attributes", []),
        )
        n += 1
    return n


async def _insert_releases(artist_mbid: str, artist_name: str,
                           groups: list, weight: int) -> int:
    n = 0
    for rg in groups:
        if rg.get("primary-type") not in KEEP_PRIMARY:
            continue
        if set(rg.get("secondary-types", [])) & SKIP_SECONDARY:
            continue

        raw_date = rg.get("first-release-date")
        fecha = _norm_date(raw_date)
        if not fecha:
            continue

        await execute(
            """
            INSERT INTO releases (mbid, release_group_mbid, artist_mbid, title,
                                  first_release_date, primary_type, secondary_types)
            VALUES ($1,$1,$2,$3,$4,$5,$6)
            ON CONFLICT (mbid) DO UPDATE SET
              first_release_date = EXCLUDED.first_release_date,
              artist_mbid        = EXCLUDED.artist_mbid,
              title              = EXCLUDED.title
            """,
            rg["id"], artist_mbid, rg["title"], fecha,
            rg.get("primary-type"), rg.get("secondary-types", []),
        )

        # Compat con el despertador viejo. UNIQUE es (artist, album).
        await execute(
            """
            INSERT INTO ephemerides (artist, album, release_date, month_day, mbid, weight)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (artist, album) DO UPDATE SET
              release_date = EXCLUDED.release_date,
              month_day    = EXCLUDED.month_day,
              mbid         = EXCLUDED.mbid,
              weight       = LEAST(ephemerides.weight, EXCLUDED.weight)
            """,
            artist_name, rg["title"], fecha.isoformat(),
            _month_day(raw_date), rg["id"], weight,
        )
        n += 1
    return n


async def hydrate_recordings(release_group_mbid: str) -> list[dict]:
    """Tracklist on-demand. Un release-group agrupa ediciones: elegimos la
    más antigua que tenga tracks cargados."""
    try:
        res = await mb.get("release", **{
            "release-group": release_group_mbid,
            "inc": "recordings",
            "limit": 25,
        })
    except Exception as e:
        logger.error("recordings %s: %s", release_group_mbid, e)
        return []

    releases = [r for r in res.get("releases", []) if r.get("media")]
    if not releases:
        return []

    releases.sort(key=lambda r: r.get("date") or "9999")
    chosen = releases[0]

    out = []
    for medium in chosen["media"]:
        for tr in medium.get("tracks", []):
            rec = tr.get("recording") or {}
            if not rec.get("id"):
                continue
            titulo = rec.get("title") or tr.get("title")
            await execute(
                """
                INSERT INTO recordings (mbid, release_mbid, title, position, length_ms)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (mbid) DO NOTHING
                """,
                rec["id"], release_group_mbid, titulo,
                tr.get("position"), rec.get("length"),
            )
            out.append({
                "mbid": rec["id"], "title": titulo,
                "position": tr.get("position"), "length_ms": rec.get("length"),
            })
    return out


# === Punto de entrada único ===

async def resolve_artist(name: str,
                         weight: int = WEIGHT_DESCUBIERTO) -> dict | None:
    """Local → MusicBrainz → hidrata → devuelve. Es lo que llama la tool."""
    locales = await find_local(name)

    # Ya hidratado localmente
    for row in locales:
        exacto = row["name"].lower() == name.lower().strip()
        if row["crawled_at"] and (exacto or row["sim"] > 0.75):
            return row

    # Nodo pelado: tenemos el mbid, falta la discografía
    for row in locales:
        exacto = row["name"].lower() == name.lower().strip()
        if not row["crawled_at"] and (exacto or row["sim"] > 0.75):
            await hydrate_artist(row["mbid"], weight)
            r = await fetchrow(
                "SELECT mbid, name, country, begin_year, end_year, tags "
                "FROM artists WHERE mbid = $1", row["mbid"])
            return dict(r) if r else None

    # No está en ningún lado: a MusicBrainz
    hit = await search_mb(name)
    if not hit:
        return None

    await hydrate_artist(hit["id"], weight)
    r = await fetchrow(
        "SELECT mbid, name, country, begin_year, end_year, tags "
        "FROM artists WHERE mbid = $1", hit["id"])
    return dict(r) if r else None