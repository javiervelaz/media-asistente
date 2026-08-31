"""Objetivos de escucha. Cero tokens, como todo el H2.

Regla de diseno: **es un sesgo, no una cuota.** Si el curador arma playlists
peores para cumplir una metrica, las salteas, y los skips envenenan la senal
que el Bloque B recien arreglo. El objetivo inclina, no obliga.

Regla de datos: **el progreso no se almacena, se deriva.** Un contador se
desincroniza de `play_history` en cuanto algo falla a mitad de camino y
despues no hay forma de saber cual de los dos tiene razon.
"""
import json
import logging

from app.db import execute, fetch, fetchrow

logger = logging.getLogger(__name__)

#: Con menos escuchas completas que esto en la ventana, el objetivo no se
#: inyecta al curador ni se muestra como porcentaje. Un ratio sobre 4 tracks
#: es ruido, y sesgar con ruido es peor que no sesgar.
MUESTRA_MINIMA = 20

TIPOS = ("coleccion", "descubrimiento", "genero", "profundidad")


def _spec(goal: dict) -> dict:
    """`spec` como dict, venga como venga.

    El pool registra un codec para jsonb, pero un script que arme su propio
    pool no lo tiene — y ahi el campo llega como str. Normalizar aca cuesta
    nada y evita que un AttributeError tumbe un turno de conversacion.
    """
    raw = goal.get("spec") if isinstance(goal, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("spec ilegible en el objetivo %s", goal.get("kind"))
            return {}
    return raw if isinstance(raw, dict) else {}

ETIQUETAS = {
    "coleccion":     "escuchar la colección en vinilo",
    "descubrimiento": "descubrir artistas nuevos",
    "genero":        "escuchar {genero}",
    "profundidad":   "escuchar álbumes enteros",
}


# ------------------------------------------------------------------ CRUD

async def activos(room_id: str = "main") -> list[dict]:
    rows = await fetch(
        "SELECT id, kind, spec, window_days FROM goals "
        "WHERE active AND room_id = $1 ORDER BY created_at",
        room_id)
    out = []
    for r in rows:
        g = dict(r)
        g["spec"] = _spec(g)
        out.append(g)
    return out


async def declarar(kind: str, spec: dict, window_days: int = 30,
                   room_id: str = "main") -> dict:
    """Un objetivo activo por tipo y sala: declarar de nuevo reemplaza."""
    await execute(
        "UPDATE goals SET active = false WHERE active AND room_id = $1 AND kind = $2",
        room_id, kind)
    row = await fetchrow(
        """
        INSERT INTO goals (room_id, kind, spec, window_days)
        VALUES ($1, $2, $3::jsonb, $4)
        RETURNING id, kind, spec, window_days
        """, room_id, kind, json.dumps(spec), window_days)
    g = dict(row)
    g["spec"] = _spec(g)
    return g


async def borrar(kind: str, room_id: str = "main") -> int:
    res = await execute(
        "UPDATE goals SET active = false WHERE active AND room_id = $1 AND kind = $2",
        room_id, kind)
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


# ------------------------------------------------------- derivar progreso

SQL_COLECCION = """
SELECT count(*) FILTER (WHERE e.weight = 1)::float
         / NULLIF(count(*), 0) AS ratio,
       count(*) AS muestra
FROM play_history ph
JOIN recordings rc ON rc.mbid = ph.recording_mbid
LEFT JOIN ephemerides e
       ON e.mbid = rc.release_mbid::text AND e.weight = 1
WHERE ph.completed
  AND ph.started_at > now() - make_interval(days => $1)
"""

# Artistas con escucha completa en la ventana que NO aparecen en los 90 dias
# previos a la ventana. "Nuevo" es nuevo para vos, no nuevo en el mundo.
SQL_DESCUBRIMIENTO = """
WITH nuevos AS (
    SELECT DISTINCT ph.artist_mbid
    FROM play_history ph
    WHERE ph.completed AND ph.artist_mbid IS NOT NULL
      AND ph.started_at > now() - make_interval(days => $1)
), previos AS (
    SELECT DISTINCT ph.artist_mbid
    FROM play_history ph
    WHERE ph.artist_mbid IS NOT NULL
      AND ph.started_at BETWEEN now() - make_interval(days => $1 + 90)
                            AND now() - make_interval(days => $1)
)
SELECT count(*) AS n,
       (SELECT count(*) FROM play_history
         WHERE completed AND started_at > now() - make_interval(days => $1)) AS muestra
FROM nuevos WHERE artist_mbid NOT IN (SELECT artist_mbid FROM previos)
"""

SQL_GENERO = """
SELECT count(*) FILTER (WHERE a.tags && $2::text[]) AS n,
       count(*) AS muestra
FROM play_history ph
JOIN artists a ON a.mbid = ph.artist_mbid
WHERE ph.completed
  AND ph.started_at > now() - make_interval(days => $1)
"""

# Profundidad: de lo escuchado entero, que proporcion pertenece a discos de
# los que escuchaste al menos 3 temas. Es una aproximacion a "escuchar
# albumes" que no necesita saber donde empieza y termina una sesion.
SQL_PROFUNDIDAD = """
WITH por_release AS (
    SELECT rc.release_mbid, count(*) AS temas
    FROM play_history ph
    JOIN recordings rc ON rc.mbid = ph.recording_mbid
    WHERE ph.completed
      AND ph.started_at > now() - make_interval(days => $1)
    GROUP BY rc.release_mbid
)
SELECT COALESCE(sum(temas) FILTER (WHERE temas >= 3), 0)::float
         / NULLIF(sum(temas), 0) AS ratio,
       COALESCE(sum(temas), 0) AS muestra
FROM por_release
"""


async def progreso(goal: dict) -> dict:
    """Estado de un objetivo. Nunca levanta: un objetivo roto no puede
    tumbar un turno de conversacion."""
    kind = goal["kind"]
    spec = _spec(goal)
    dias = goal.get("window_days") or 30
    base = {"kind": kind, "spec": spec, "dias": dias,
            "actual": 0, "target": 0, "muestra": 0,
            "unidad": "%", "suficiente": False}

    try:
        if kind == "coleccion":
            row = await fetchrow(SQL_COLECCION, dias)
            base.update(actual=(row["ratio"] or 0.0) * 100,
                        target=float(spec.get("target", 0.4)) * 100,
                        muestra=row["muestra"] or 0)

        elif kind == "descubrimiento":
            row = await fetchrow(SQL_DESCUBRIMIENTO, dias)
            base.update(actual=row["n"] or 0,
                        target=float(spec.get("target", 5)),
                        muestra=row["muestra"] or 0, unidad="artistas")

        elif kind == "genero":
            tags = spec.get("tags") or [spec.get("genero", "")]
            row = await fetchrow(SQL_GENERO, dias, [t.lower() for t in tags if t])
            base.update(actual=row["n"] or 0,
                        target=float(spec.get("target", 20)),
                        muestra=row["muestra"] or 0, unidad="temas")

        elif kind == "profundidad":
            row = await fetchrow(SQL_PROFUNDIDAD, dias)
            base.update(actual=(row["ratio"] or 0.0) * 100,
                        target=float(spec.get("target", 0.5)) * 100,
                        muestra=row["muestra"] or 0)
        else:
            logger.warning("tipo de objetivo desconocido: %s", kind)
            return base
    except Exception:
        logger.exception("no pude derivar el progreso de %s", kind)
        return base

    base["suficiente"] = base["muestra"] >= MUESTRA_MINIMA
    base["cumplido"] = base["actual"] >= base["target"]
    base["falta"] = max(0.0, base["target"] - base["actual"])
    return base


async def estado(room_id: str = "main") -> list[dict]:
    return [await progreso(g) for g in await activos(room_id)]


async def mas_atrasado(room_id: str = "main") -> dict | None:
    """El objetivo con mayor distancia relativa al target.

    Relativa y no absoluta: 3 artistas de 5 esta mas lejos que 35% de 40%
    aunque el numero crudo diga lo contrario.
    """
    estados = [e for e in await estado(room_id) if e["target"] > 0]
    if not estados:
        return None
    pendientes = [e for e in estados if not e["cumplido"]]
    if not pendientes:
        return None
    return max(pendientes, key=lambda e: e["falta"] / e["target"])


def linea_para_curador(e: dict) -> str | None:
    """Una linea, no un dump. Mismo criterio que `listo` en el hallazgo 8.

    Devuelve None si la muestra es chica: sesgar con ruido es peor que no
    sesgar.
    """
    if not e or not e.get("suficiente") or e.get("cumplido"):
        return None

    kind, spec = e["kind"], _spec(e)
    if kind == "coleccion":
        return (f"Objetivo activo: {e['target']:.0f}% de escuchas de la colección "
                f"en vinilo (vas {e['actual']:.0f}% en {e['dias']} días). "
                "A igualdad de criterio curatorial, incliná hacia weight=1.")
    if kind == "descubrimiento":
        return (f"Objetivo activo: {e['target']:.0f} artistas nuevos por ventana "
                f"(vas {e['actual']:.0f}). A igualdad de criterio, preferí "
                "artistas que no aparezcan en get_play_history.")
    if kind == "genero":
        g = spec.get("genero") or ", ".join(spec.get("tags") or [])
        return (f"Objetivo activo: más {g} (vas {e['actual']:.0f} de "
                f"{e['target']:.0f} temas). A igualdad de criterio, incliná "
                "hacia ahí — sin forzar la tesis de la playlist.")
    if kind == "profundidad":
        return (f"Objetivo activo: escuchar álbumes enteros (vas "
                f"{e['actual']:.0f}% de {e['target']:.0f}%). A igualdad de "
                "criterio, agrupá varios temas del mismo disco en vez de uno "
                "por artista.")
    return None
