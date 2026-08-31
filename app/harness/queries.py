"""Las consultas del H2. Cero tokens, todas.

Cada funcion de aca reemplaza un turno que hoy cuesta ~19k tokens y ademas
responde mal: el curador no puede contestar "que escuche hoy" porque su tool
`get_play_history` esta descrita para lo contrario —evitar lo reciente— y
devuelve 30 dias agrupados, sin recording_mbid.

Schema (verificado contra las queries que ya existen en el repo):
  play_history      playlist_id, position, artist, title, rationale,
                    recording_mbid, artist_mbid, youtube_id, room_id,
                    started_at, played_ms, skipped, completed
  artists           mbid, name, country, begin_year, end_year, crawled_at
  artist_relations  source_mbid, target_mbid, rel_type
  releases          mbid, artist_mbid, title, first_release_date,
                    primary_type, secondary_types
  recordings        mbid, release_mbid, title, position, length_ms
  track_resolutions recording_mbid, youtube_id, duration_delta,
                    fail_count, play_count
  ephemerides       artist, album, release_date, month_day, mbid, weight
                    (`mbid` es el mbid del RELEASE, en texto)
"""
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.db import fetch, fetchrow

logger = logging.getLogger(__name__)

SIM_THRESHOLD = 0.4      # mas laxo que local_search: aca el usuario tipea
LIMITE = 15


# --------------------------------------------------------------- ventanas

class Ventana:
    """Rango temporal con una etiqueta para el renderer."""
    __slots__ = ("desde", "hasta", "etiqueta", "dias")

    def __init__(self, desde: datetime, hasta: datetime, etiqueta: str):
        self.desde, self.hasta, self.etiqueta = desde, hasta, etiqueta
        self.dias = max(1, (hasta - desde).days)


try:
    TZ = ZoneInfo(settings.harness_tz)
except Exception:                       # tzdata ausente: no rompas el turno
    logger.warning("zona horaria %r no disponible, uso UTC", settings.harness_tz)
    TZ = ZoneInfo("UTC")


def ahora() -> datetime:
    return datetime.now(TZ)


def _ini(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=TZ)


def _fin(d: date) -> datetime:
    return datetime.combine(d, time.max, tzinfo=TZ)


def _dia(d: date) -> tuple[datetime, datetime]:
    return (_ini(d), _fin(d))


def ventana(texto: str, hoy: date | None = None) -> Ventana:
    """Convierte una expresion temporal en un rango. Sin LLM.

    El default es hoy: si alguien pregunta "que escuche" sin decir cuando,
    casi siempre habla de la sesion en curso.

    Los rangos son tz-aware (settings.harness_tz). `play_history.started_at`
    es timestamptz: si se le pasa un datetime naive, asyncpg lo manda como
    UTC y a la noche "hoy" queda corrido un dia.
    """
    # date.today() usa la zona del proceso, que en un systemd unit puede ser
    # UTC aunque la Pi este en horario argentino.
    hoy = hoy or ahora().date()
    t = (texto or "").lower()

    if "anteayer" in t:
        d, h = _dia(hoy - timedelta(days=2));  return Ventana(d, h, "anteayer")
    if "ayer" in t:
        d, h = _dia(hoy - timedelta(days=1));  return Ventana(d, h, "ayer")
    if "semana pasada" in t:
        fin = hoy - timedelta(days=hoy.weekday() + 1)
        ini = fin - timedelta(days=6)
        return Ventana(_ini(ini), _fin(fin), "la semana pasada")
    if "esta semana" in t:
        ini = hoy - timedelta(days=hoy.weekday())
        return Ventana(_ini(ini), _fin(hoy), "esta semana")
    if "este mes" in t:
        return Ventana(_ini(hoy.replace(day=1)), _fin(hoy), "este mes")
    if "mes pasado" in t:
        fin = hoy.replace(day=1) - timedelta(days=1)
        return Ventana(_ini(fin.replace(day=1)), _fin(fin), "el mes pasado")

    if m := re.search(r"ultim[oa]s?\s+(\d{1,3})\s*(dias?|semanas?|meses?)", t):
        n, unidad = int(m.group(1)), m.group(2)
        dias = n * (7 if unidad.startswith("semana")
                    else 30 if unidad.startswith("mes") else 1)
        dias = max(1, min(dias, 365))
        ini = hoy - timedelta(days=dias)
        return Ventana(_ini(ini), _fin(hoy), f"los ultimos {dias} dias")

    d, h = _dia(hoy)
    return Ventana(d, h, "hoy")


# --------------------------------------------------------------- artistas

async def resolver_artista(nombre: str) -> dict | None:
    """Nombre tipeado -> artista del grafo, por trigram.

    Devuelve None si no matchea: mejor decir "no lo tengo" que responder
    sobre otro artista parecido.
    """
    if not nombre or len(nombre.strip()) < 2:
        return None
    row = await fetchrow(
        """
        SELECT mbid, name, similarity(name, $1) AS sim
        FROM artists
        WHERE name % $1 AND similarity(name, $1) >= $2
        ORDER BY sim DESC, name
        LIMIT 1
        """, nombre.strip(), SIM_THRESHOLD)
    return dict(row) if row else None


# --------------------------------------------------------------- historial

async def historial_periodo(v: Ventana, limite: int = 40) -> list[dict]:
    rows = await fetch(
        """
        SELECT artist, title, started_at, completed, skipped
        FROM play_history
        WHERE started_at >= $1 AND started_at <= $2
        ORDER BY started_at DESC
        LIMIT $3
        """, v.desde, v.hasta, limite)
    return [dict(r) for r in rows]


async def historial_artista(artist_mbid, nombre: str,
                            dias: int = 90) -> list[dict]:
    """Por mbid cuando el artista esta en el grafo; por nombre si no.

    El fallback por texto importa: play_history guarda artist como texto y
    artist_mbid puede ser NULL (el curador devolvio un track libre, o el
    recording no estaba en el grafo cuando sono).
    """
    rows = await fetch(
        """
        SELECT title, count(*) AS veces,
               count(*) FILTER (WHERE completed) AS completos,
               count(*) FILTER (WHERE skipped)   AS skips,
               max(started_at) AS ultima
        FROM play_history
        WHERE started_at > now() - make_interval(days => $3)
          AND ( ($1::uuid IS NOT NULL AND artist_mbid = $1::uuid)
             OR artist % $2 )
        GROUP BY title
        ORDER BY max(started_at) DESC
        LIMIT $4
        """, artist_mbid, nombre or "", dias, LIMITE)
    return [dict(r) for r in rows]


async def top_escuchados(dias: int = 30) -> list[dict]:
    """Ranking por señal positiva, no por veces que empezo a sonar.

    Un track que arranco 8 veces y se salteo 8 no es un favorito.
    """
    rows = await fetch(
        """
        SELECT artist,
               count(*) FILTER (WHERE completed) AS completos,
               count(*) AS veces,
               count(DISTINCT title) AS temas
        FROM play_history
        WHERE started_at > now() - make_interval(days => $1)
        GROUP BY artist
        HAVING count(*) FILTER (WHERE completed) > 0
        ORDER BY completos DESC, veces DESC
        LIMIT $2
        """, dias, LIMITE)
    return [dict(r) for r in rows]


async def salteados(dias: int = 90) -> list[dict]:
    rows = await fetch(
        """
        SELECT artist, count(*) AS skips,
               count(DISTINCT title) AS temas
        FROM play_history
        WHERE skipped AND started_at > now() - make_interval(days => $1)
        GROUP BY artist
        HAVING count(*) >= 2
        ORDER BY skips DESC
        LIMIT $2
        """, dias, LIMITE)
    return [dict(r) for r in rows]


async def nunca_escuchado(limite: int = LIMITE) -> list[dict]:
    """Discos de la coleccion en vinilo (weight=1) que nunca sonaron enteros.

    Ningun servicio de streaming puede responder esto: ninguno sabe que hay
    en el estante. `ephemerides.mbid` es el mbid del release, en texto.
    """
    rows = await fetch(
        """
        SELECT e.mbid, e.artist, e.album, left(e.release_date, 4) AS anio
        FROM ephemerides e
        WHERE e.weight = 1
          AND NOT EXISTS (
            SELECT 1
            FROM recordings rc
            JOIN play_history ph ON ph.recording_mbid = rc.mbid
            WHERE e.mbid IS NOT NULL
              AND rc.release_mbid = e.mbid::uuid
              AND ph.completed
          )
        ORDER BY e.release_date NULLS LAST, e.artist
        LIMIT $1
        """, limite)
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ grafo

async def discografia(artist_mbid, limite: int = 20) -> list[dict]:
    rows = await fetch(
        """
        SELECT r.title, r.primary_type,
               EXTRACT(YEAR FROM r.first_release_date)::int AS anio,
               (SELECT count(*) FROM recordings rc
                 WHERE rc.release_mbid = r.mbid) AS tracks,
               (e.weight = 1) AS en_vinilo
        FROM releases r
        LEFT JOIN ephemerides e ON e.mbid = r.mbid::text AND e.weight = 1
        WHERE r.artist_mbid = $1
          AND r.primary_type = 'Album'
          AND NOT ('Compilation' = ANY(COALESCE(r.secondary_types, '{}')))
        ORDER BY r.first_release_date NULLS LAST
        LIMIT $2
        """, artist_mbid, limite)
    return [dict(r) for r in rows]


async def relaciones(artist_mbid, limite: int = LIMITE) -> list[dict]:
    """Vinculos del artista, ordenados por discografia cargada.

    `get_artist_graph` ordena por `a.name` con LIMIT 30: los que sobreviven
    son los que empiezan con A. Dentro de una playlist el sesgo pasa
    desapercibido; como respuesta en pantalla, no.
    """
    rows = await fetch(
        """
        SELECT DISTINCT a.name, a.country, ar.rel_type,
               (SELECT count(*) FROM releases r
                 WHERE r.artist_mbid = a.mbid) AS releases
        FROM artist_relations ar
        JOIN artists a
          ON a.mbid = CASE WHEN ar.source_mbid = $1
                           THEN ar.target_mbid ELSE ar.source_mbid END
        WHERE ar.source_mbid = $1 OR ar.target_mbid = $1
        ORDER BY releases DESC, a.name
        LIMIT $2
        """, artist_mbid, limite)
    return [dict(r) for r in rows]


async def efemerides_hoy(limite: int = 8) -> list[dict]:
    rows = await fetch(
        """
        SELECT mbid, artist, album, left(release_date, 4) AS anio,
               (EXTRACT(YEAR FROM CURRENT_DATE)
                - left(release_date, 4)::int) AS aniversario,
               weight
        FROM ephemerides
        WHERE month_day = to_char(CURRENT_DATE, 'MM-DD')
          AND release_date IS NOT NULL
        ORDER BY weight, release_date
        LIMIT $1
        """, limite)
    return [dict(r) for r in rows]


# ------------------------------------------------- reproducir el historial

async def tracks_del_periodo(v: Ventana, limite: int = 14) -> list[dict]:
    """Lo escuchado en la ventana, listo para volver a sonar.

    Solo tracks con `recording_mbid` YA resuelto en YouTube: en un Pi 3B eso
    es la diferencia entre arrancar al instante y esperar una descarga de
    yt-dlp por tema. Todo lo que sono en la ventana ya paso por ahi, asi que
    el filtro casi no descarta nada.

    Se excluye lo salteado sin completar: no tiene sentido devolverte lo que
    pasaste de largo.
    """
    rows = await fetch(
        """
        SELECT DISTINCT ON (ph.recording_mbid)
               ph.recording_mbid::text AS recording_mbid,
               ph.artist, ph.title, ph.youtube_id
        FROM play_history ph
        JOIN track_resolutions tr ON tr.recording_mbid = ph.recording_mbid
        WHERE ph.started_at >= $1 AND ph.started_at <= $2
          AND ph.recording_mbid IS NOT NULL
          AND COALESCE(tr.fail_count, 0) < 3
          AND (ph.completed OR NOT ph.skipped)
        ORDER BY ph.recording_mbid, ph.started_at DESC
        LIMIT $3
        """, v.desde, v.hasta, limite)
    return [dict(r) for r in rows]


async def tracks_de_releases(mbids: list[str], limite: int = 14,
                             por_album: int = 3) -> list[dict]:
    """Tracks de releases concretos, listos para encolar.

    Existe para que lo que se REPRODUCE sea exactamente lo que se LISTO. El
    `/despertador` usa su propia ventana (±7 dias, solo Album, sin
    compilations), asi que ofrecer "¿lo pongo?" despues de listar efemerides
    y despues lanzar el despertador seria mentir: son dos conjuntos distintos.

    Prioriza lo ya resuelto en YouTube (`tr.recording_mbid IS NOT NULL`) sin
    exigirlo: si un disco del estante nunca sono, igual tiene que poder sonar
    ahora — solo va a tardar la descarga.
    """
    limpios = [m for m in (mbids or []) if m]
    if not limpios:
        return []
    rows = await fetch(
        """
        WITH ordenados AS (
            SELECT rc.mbid::text AS recording_mbid, a.name AS artist,
                   rc.title, rc.length_ms, r.title AS album,
                   (tr.recording_mbid IS NOT NULL) AS listo,
                   row_number() OVER (
                       PARTITION BY rc.release_mbid
                       ORDER BY (tr.recording_mbid IS NOT NULL) DESC,
                                rc.position NULLS LAST
                   ) AS n
            FROM recordings rc
            JOIN releases r ON r.mbid = rc.release_mbid
            JOIN artists  a ON a.mbid = r.artist_mbid
            LEFT JOIN track_resolutions tr ON tr.recording_mbid = rc.mbid
            WHERE rc.release_mbid = ANY($1::uuid[])
              AND COALESCE(tr.fail_count, 0) < 3
              AND (rc.length_ms IS NULL OR rc.length_ms BETWEEN 60000 AND 900000)
        )
        SELECT recording_mbid, artist, title, length_ms, album, listo
        FROM ordenados WHERE n <= $2
        ORDER BY listo DESC, n
        LIMIT $3
        """, limpios, por_album, limite)
    return [dict(r) for r in rows]


# --------------------------------------------------- cuanto cuesta un turno

_COSTO_CACHE: dict = {"valor": None, "hasta": None}
COSTO_DEFAULT = 19_000          # antes de tener datos propios
COSTO_TTL_MIN = 30


async def costo_tipico() -> int:
    """Tokens de entrada que suele gastar un turno de curador.

    Sale de `turn_log`: el sistema aprende su propio costo en vez de tener un
    numero magico en el codigo. Cacheado en memoria — es para un mensaje, no
    vale una consulta por turno.
    """
    ahora_ = ahora()
    if _COSTO_CACHE["hasta"] and ahora_ < _COSTO_CACHE["hasta"]:
        return _COSTO_CACHE["valor"]

    valor = COSTO_DEFAULT
    try:
        row = await fetchrow(
            """
            SELECT avg(input_tokens + output_tokens)::int AS prom
            FROM turn_log
            WHERE model IS NOT NULL AND input_tokens > 0
              AND created_at > now() - interval '30 days'
            """)
        if row and row["prom"]:
            valor = int(row["prom"])
    except Exception:
        logger.debug("no pude estimar el costo tipico, uso el default")

    _COSTO_CACHE["valor"] = valor
    _COSTO_CACHE["hasta"] = ahora_ + timedelta(minutes=COSTO_TTL_MIN)
    return valor
