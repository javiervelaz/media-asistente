"""H5 — proactividad. El bot escribe primero. Cero tokens, como todo el H2.

Regla de diseno: **ofrece, no reproduce.** Un cron que arranca musica a las
8 de la maniana es un despertador que no pediste; se apaga en tres dias y ahi
se pierde el canal, no la funcion. El despertador es una alarma que el usuario
programo y tiene que sonar; la sugerencia es una propuesta y espera un boton.

Regla de datos: **el silencio es una salida valida.** Si hoy no hay efemeride
del estante, ningun objetivo atrasado y nada sin escuchar que valga, no se
manda nada. Un bot que no tiene nada que decir y habla igual te entrena a
ignorarlo — y ese es el unico modo de falla del que no se vuelve.

Por que este bloque no se podia construir antes del 12/09: las tres fuentes
filtran `play_history.completed`, y esa columna no se escribia (el observador
de mpv nunca atribuyo un `eof`, ver `bitacora-eof-y-datos.md`). "Sin escuchar
hace 90 dias" era verdad para los 921 discos del estante, o sea azar
disfrazado de criterio.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.config import settings
from app.db import execute, fetch, fetchrow, fetchval
from app.harness import goals, queries

logger = logging.getLogger(__name__)

TIPOS = ("efemeride", "objetivo", "estante")

#: Con una sugerencia sin contestar de hace menos que esto, no se manda otra.
#: Dos preguntas sin responder son la definicion de spam. Pasado el plazo se
#: vuelve a intentar: que se te haya pasado una no puede callar al bot para
#: siempre. La fila queda con `aceptada IS NULL`, que es justamente la metrica
#: de "ignorada".
DIAS_SIN_APILAR = 3

#: No se manda el mismo tipo dos dias seguidos. Fuerza rotacion y evita que un
#: objetivo atrasado se convierta en un recordatorio diario.
DIAS_MISMO_TIPO = 2


@dataclass(slots=True)
class Sugerencia:
    kind: str
    etiqueta: str
    #: Releases que se ofrecieron. Vacio para `objetivo`: ese mensaje no
    #: nombra discos, asi que no promete una lista concreta.
    mbids: list[str] = field(default_factory=list)
    datos: dict = field(default_factory=dict)
    goal_id: int | None = None
    id: int | None = None


# ------------------------------------------------------------------ guardas

async def _silencio_hasta(room_id: str) -> datetime | None:
    try:
        return await fetchval(
            "SELECT silencio_hasta FROM sala_prefs WHERE room_id = $1", room_id)
    except Exception:
        logger.exception("no pude leer sala_prefs")
        return None


async def silenciar(room_id: str, hasta: datetime) -> None:
    await execute(
        """
        INSERT INTO sala_prefs (room_id, silencio_hasta) VALUES ($1, $2)
        ON CONFLICT (room_id) DO UPDATE SET silencio_hasta = EXCLUDED.silencio_hasta
        """, room_id, hasta)


async def _guardas(room_id: str, ahora: datetime,
                   ignorar_hora: bool) -> str | None:
    """Devuelve el motivo por el que NO se manda, o None si se puede mandar.

    Se evaluan antes de elegir contenido: no tiene sentido buscar que decir
    si no corresponde hablar.
    """
    if not settings.harness_sugerencia_activa:
        return "apagado (harness_sugerencia_activa = False)"

    hasta = await _silencio_hasta(room_id)
    if hasta and hasta > ahora:
        return f"silenciado hasta {hasta:%d/%m %H:%M}"

    # La hora se decide en harness_tz, NO en la del proceso. El cron de n8n
    # corre en el VPS y `datetime.now()` del proceso puede estar en UTC: a las
    # 20:00 de Cordoba ya es otro dia en UTC y la guarda de "una por dia" se
    # rompe justo en la franja de uso. Es el mismo bug del H2 con
    # `que escuche hoy`.
    if not ignorar_hora and ahora.hour != settings.harness_sugerencia_hora:
        return (f"no es la hora (son las {ahora.hour:02d}, "
                f"manda a las {settings.harness_sugerencia_hora:02d})")

    desde_hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)

    ya = await fetchval(
        "SELECT id FROM sugerencias WHERE room_id = $1 AND enviada_at >= $2 "
        "ORDER BY enviada_at DESC LIMIT 1", room_id, desde_hoy)
    if ya:
        return f"ya se mando una hoy (id {ya})"

    pendiente = await fetchrow(
        "SELECT id, enviada_at FROM sugerencias "
        "WHERE room_id = $1 AND aceptada IS NULL "
        "ORDER BY enviada_at DESC LIMIT 1", room_id)
    if pendiente and pendiente["enviada_at"] > ahora - timedelta(days=DIAS_SIN_APILAR):
        return (f"la anterior quedo sin contestar "
                f"(id {pendiente['id']}, {pendiente['enviada_at']:%d/%m})")

    # Si ya escuchaste hoy no hace falta empujar: ya estas usando el bot.
    escucho = await fetchval(
        "SELECT 1 FROM play_history WHERE completed AND started_at >= $1 LIMIT 1",
        desde_hoy)
    if escucho:
        return "ya hubo una escucha completa hoy"

    return None


# ------------------------------------------------------------------ metrica

async def tasa(dias: int = 90) -> list[dict]:
    """Aceptacion por tipo. La unica medida honesta del bloque."""
    rows = await fetch(
        """
        SELECT kind,
               count(*)                                 AS enviadas,
               count(*) FILTER (WHERE aceptada)         AS aceptadas,
               count(*) FILTER (WHERE aceptada IS FALSE) AS rechazadas,
               count(*) FILTER (WHERE aceptada IS NULL) AS ignoradas
        FROM sugerencias
        WHERE enviada_at > now() - make_interval(days => $1)
        GROUP BY 1 ORDER BY 1
        """, dias)
    out = []
    for r in rows:
        d = dict(r)
        d["pct"] = (100.0 * d["aceptadas"] / d["enviadas"]) if d["enviadas"] else 0.0
        out.append(d)
    return out


async def tipos_podados() -> set[str]:
    """Tipos que dejan de mandarse solos por baja aceptacion.

    El bloque se poda con sus propios datos, igual que la etapa 1 del router
    se hace crecer con `turn_log`. No queda esperando que alguien se acuerde
    de revisarlo.
    """
    try:
        filas = await tasa()
    except Exception:
        logger.exception("no pude leer la tasa de aceptacion; no podo nada")
        return set()

    podados = {
        f["kind"] for f in filas
        if f["enviadas"] >= settings.harness_sugerencia_min_envios
        and f["pct"] / 100.0 < settings.harness_sugerencia_min_aceptacion
    }
    if podados:
        logger.info("tipos podados por baja aceptacion: %s", sorted(podados))
    return podados


# ------------------------------------------------------------------ fuentes

async def _mbids_recientes() -> set[str]:
    dias = settings.harness_sugerencia_dias_repetir
    try:
        rows = await fetch(
            "SELECT DISTINCT unnest(mbids) AS mbid FROM sugerencias "
            "WHERE enviada_at > now() - make_interval(days => $1)", dias)
    except Exception:
        logger.exception("no pude leer los mbids ya sugeridos")
        return set()
    return {r["mbid"] for r in rows if r["mbid"]}


async def _efemeride(recientes: set[str]) -> Sugerencia | None:
    """Aniversario de un disco TUYO. Gana siempre que exista.

    Es lo que justifica el bloque entero: ningun servicio de streaming puede
    decirte esto, porque ninguno sabe que tenes en el estante.
    """
    for r in await queries.efemerides_hoy(limite=8):
        if r.get("weight") != 1 or not r.get("mbid"):
            continue
        if r["mbid"] in recientes:
            continue
        return Sugerencia(kind="efemeride",
                          etiqueta=f"{r['artist']} — {r['album']}",
                          mbids=[r["mbid"]], datos=dict(r))
    return None


async def _objetivo(room_id: str) -> Sugerencia | None:
    """El objetivo mas atrasado, solo si tiene muestra suficiente.

    `mbids` va vacio a proposito: el mensaje no nombra discos, ofrece armar
    algo para el objetivo. Como no lista nada concreto, no promete nada
    concreto, y la playlist se arma al aceptar con `tracks_para_objetivo`.
    Un ratio calculado sobre 4 tracks es ruido y avisar sobre ruido es peor
    que callarse.
    """
    e = await goals.mas_atrasado(room_id)
    if not e or not e.get("suficiente") or e.get("cumplido"):
        return None
    return Sugerencia(kind="objetivo",
                      etiqueta=goals.ETIQUETAS.get(e["kind"], e["kind"]).format(
                          genero=(e.get("spec") or {}).get("genero", "")),
                      mbids=[], datos=e, goal_id=e.get("id"))


SQL_ESTANTE = """
SELECT e.mbid, e.artist, e.album, left(e.release_date, 4) AS anio
FROM ephemerides e
WHERE e.weight = 1
  AND e.mbid IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM recordings rc
    JOIN play_history ph ON ph.recording_mbid = rc.mbid
    WHERE rc.release_mbid = e.mbid::uuid
      AND ph.completed
      AND ph.started_at > now() - make_interval(days => $1)
  )
  AND NOT EXISTS (
    SELECT 1 FROM sugerencias s
    WHERE s.mbids @> ARRAY[e.mbid]
      AND s.enviada_at > now() - make_interval(days => $2)
  )
ORDER BY random()
LIMIT 1
"""


async def _estante() -> Sugerencia | None:
    """Un disco del estante que no suena hace rato.

    `ORDER BY random()` y no un orden determinista: sobre 921 discos, un
    ORDER BY estable te manda los mismos veinte durante un mes. El azar
    acotado por "no repetir en N dias" da variedad sin estado extra.
    """
    r = await fetchrow(SQL_ESTANTE,
                       settings.harness_sugerencia_estante_dias,
                       settings.harness_sugerencia_dias_repetir)
    if not r:
        return None
    return Sugerencia(kind="estante",
                      etiqueta=f"{r['artist']} — {r['album']}",
                      mbids=[r["mbid"]], datos=dict(r))


# ------------------------------------------------------------------ eleccion

async def _ultimo_kind(room_id: str, ahora: datetime) -> str | None:
    return await fetchval(
        "SELECT kind FROM sugerencias WHERE room_id = $1 AND enviada_at > $2 "
        "ORDER BY enviada_at DESC LIMIT 1",
        room_id, ahora - timedelta(days=DIAS_MISMO_TIPO))


async def elegir(room_id: str = "main", *, ahora: datetime | None = None,
                 ignorar_hora: bool = False) -> tuple[Sugerencia | None, str]:
    """Que mandar hoy, o por que no se manda nada. NO registra nada.

    Devuelve `(sugerencia, motivo)`. Con sugerencia en None, `motivo` explica
    el silencio — que es lo que hace diagnosticable un cron que decide
    callarse. Sin eso, un bot mudo y un cron que no corrio son
    indistinguibles.
    """
    ahora = ahora or queries.ahora()

    motivo = await _guardas(room_id, ahora, ignorar_hora)
    if motivo:
        return None, motivo

    podados = await tipos_podados()
    ultimo = await _ultimo_kind(room_id, ahora)
    recientes = await _mbids_recientes()

    descartes = []
    for kind, buscar in (("efemeride", lambda: _efemeride(recientes)),
                         ("objetivo",  lambda: _objetivo(room_id)),
                         ("estante",   _estante)):
        if kind in podados:
            descartes.append(f"{kind}: podado por baja aceptacion")
            continue
        if kind == ultimo:
            descartes.append(f"{kind}: se mando hace menos de {DIAS_MISMO_TIPO} dias")
            continue
        try:
            s = await buscar()
        except Exception:
            logger.exception("fuente %s fallo; sigo con la que sigue", kind)
            descartes.append(f"{kind}: error")
            continue
        if s:
            return s, "ok"
        descartes.append(f"{kind}: sin material")

    return None, "nada que sugerir hoy — " + "; ".join(descartes)


# ------------------------------------------------------------------ registro

async def registrar(s: Sugerencia, room_id: str = "main") -> int:
    row = await fetchrow(
        """
        INSERT INTO sugerencias (room_id, kind, etiqueta, mbids, goal_id)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """, room_id, s.kind, s.etiqueta, list(s.mbids), s.goal_id)
    s.id = row["id"]
    logger.info("sugerencia %s registrada: %s (%s)", s.id, s.kind, s.etiqueta)
    return s.id


async def responder(sug_id: int, aceptada: bool) -> dict | None:
    """Marca la respuesta. Idempotente a proposito.

    El `AND aceptada IS NULL` hace que un segundo toque del boton no
    reproduzca de nuevo: en Telegram la gente aprieta dos veces, y encolar la
    playlist dos veces seria un bug visible y molesto. Devuelve None si la
    sugerencia no existe o ya estaba contestada.
    """
    row = await fetchrow(
        """
        UPDATE sugerencias
        SET aceptada = $2, respondida_at = now()
        WHERE id = $1 AND aceptada IS NULL
        RETURNING id, room_id, kind, etiqueta, mbids, goal_id
        """, sug_id, aceptada)
    if row is None:
        logger.info("sugerencia %s inexistente o ya contestada", sug_id)
        return None
    return dict(row)


def callback_data(sug_id: int, aceptada: bool) -> str:
    """El payload del boton inline.

    Telegram limita `callback_data` a 64 bytes y un mbid son 36 caracteres:
    no entran ni dos. Por eso el boton lleva la referencia y el contenido vive
    en la fila. Con un id de 10 digitos esto son 17 bytes.
    """
    return f"sug:{sug_id}:{'si' if aceptada else 'no'}"
