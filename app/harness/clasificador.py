"""Etapa 2 del router: Haiku clasifica lo que los patrones no entienden.

La etapa 1 cubre lo que se dice de UNA forma; esta cubre lo mismo dicho de
otra. No reemplaza patrones: corre solo cuando ninguno matcheó, así que cada
patrón nuevo le saca trabajo (y costo).

Tres decisiones que vienen de los bugs de los bloques anteriores:

1. **`saludo`, `ayuda`, `confirmar` y `rechazar` están en el catálogo con
   ejemplos.** Un clasificador entrenado solo en intents "útiles" empuja lo
   social al intent más cercano, que es `playlist` — el bug de "hola charly",
   ahora pagando también la clasificación.

2. **Consultar y reproducir son intents separados**, no un slot que el modelo
   deduzca. "Qué escuché hoy" y "poné algo que escuché hoy" comparten la
   ventana temporal y nada más.

3. **La ambigüedad por contexto no se delega.** `dale` depende de si hubo una
   oferta antes, no de la frase: lo resuelve el ejecutor. Está en el enum
   igual, para que el modelo no lo empuje a otro lado.

El system prompt es corto a propósito (~700 tokens): por debajo del mínimo de
prompt caching no hay nada que cachear, y un catálogo chico clasifica mejor
que uno exhaustivo.
"""
import json
import logging

from anthropic import AsyncAnthropic

from app.config import settings
from app.harness.intents import HAIKU, Intent

logger = logging.getLogger(__name__)

client = AsyncAnthropic(api_key=settings.anthropic_api_key)

#: Lo que el clasificador puede devolver. Es un subconjunto deliberado del
#: catálogo: los `control_*` los cubre la etapa 1 con patrones de tres
#: palabras y no vale pagar por conjugarlos.
INTENTS = [
    "playlist", "reproducir_historial", "reproducir_coleccion",
    "reproducir_disco_coleccion", "historial_periodo", "historial_artista",
    "top_escuchados", "salteados", "nunca_escuchado", "discografia",
    "relaciones", "efemerides_hoy", "estado_objetivos",
    "set_objetivo_coleccion", "set_objetivo_descubrimiento",
    "set_objetivo_genero", "set_objetivo_profundidad",
    "saludo", "ayuda", "confirmar", "rechazar",
    "control_next", "control_pause", "control_play", "control_stop",
    "estado_actual", "estado_cola",
    "no_entendido",
]

SYSTEM = """Clasificás mensajes para un reproductor de música doméstico en
español rioplatense. Devolvés SIEMPRE una llamada a `clasificar`.

El usuario le habla a un equipo de audio, no a un asistente general. Los
mensajes son cortos, con voseo y muletillas ("che", "dale", "porfa").

DISTINCIONES QUE IMPORTAN:

· CONSULTAR vs REPRODUCIR. "qué escuché hoy" pide una respuesta en pantalla
  (historial_periodo). "poné algo que escuché hoy" pide que suene música
  (reproducir_historial). Nunca los mezcles: uno lista, el otro reproduce.

· LA COLECCIÓN es su estante de vinilos, distinta del resto de la música.
  "algo de mi colección" → reproducir_coleccion (temas sueltos).
  "un disco de mi colección" → reproducir_disco_coleccion (un álbum entero).

· UN PEDIDO CURATORIAL es playlist: nombra un artista, un género, un ánimo o
  una época que el sistema tiene que interpretar ("algo tranqui para
  cocinar", "post-punk del 80", "poné Sumo").

· UN OBJETIVO es una intención sostenida, no un pedido para ahora:
  "quiero escuchar más jazz" → set_objetivo_genero.
  "poné jazz" → playlist.

· LO SOCIAL NO ES UN PEDIDO. "hola", "gracias", "buenas" → saludo.
  Nunca lo fuerces a playlist.

· `confidence` es tu certeza real, de 0 a 1. Por debajo de 0.6 el sistema
  repregunta en vez de adivinar, y eso está bien: equivocarse cuesta más que
  preguntar. Si el mensaje es ambiguo, bajá la confianza en vez de elegir.

· Si no encaja en ninguno, `no_entendido` con confianza baja."""

EJEMPLOS = [
    ("ponete algo que me levante el ánimo", "playlist", None),
    ("necesito música para laburar", "playlist", None),
    ("algo tipo Radiohead pero más tranquilo", "playlist", None),
    ("qué estuve escuchando estos días", "historial_periodo", None),
    ("cuánto escuché de Spinetta", "historial_artista", "Spinetta"),
    ("volvé a poner lo de recién", "reproducir_historial", None),
    ("algo del estante que no haya escuchado", "reproducir_coleccion", None),
    ("poné un vinilo entero", "reproducir_disco_coleccion", None),
    ("me gustaría escuchar más folklore", "set_objetivo_genero", "folklore"),
    ("qué bandas se parecen a Wire", "relaciones", "Wire"),
    ("buenas tardes", "saludo", None),
    ("qué onda, qué podés hacer", "ayuda", None),
    ("sí, hacelo", "confirmar", None),
    ("nah, dejá", "rechazar", None),
    ("basta de música", "control_stop", None),
    ("asdkjh", "no_entendido", None),
]

TOOL = {
    "name": "clasificar",
    "description": "Clasifica el mensaje del usuario en un intent del catálogo.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": INTENTS},
            "artista": {
                "type": ["string", "null"],
                "description": "Nombre del artista si el mensaje lo menciona, "
                               "tal como lo escribió el usuario.",
            },
            "genero": {
                "type": ["string", "null"],
                "description": "Género para set_objetivo_genero.",
            },
            "cuando": {
                "type": ["string", "null"],
                "description": "Expresión temporal tal cual: 'hoy', 'ayer', "
                               "'la semana pasada', 'últimos 7 días'.",
            },
            "confidence": {"type": "number"},
        },
        "required": ["intent", "confidence"],
    },
}


def _mensajes(texto: str) -> list[dict]:
    """Los ejemplos van como turnos, no como texto en el system: el modelo
    imita mejor una conversación que una lista."""
    msgs = []
    for frase, intent, extra in EJEMPLOS:
        args = {"intent": intent, "confidence": 0.9}
        if extra:
            args["artista" if intent in ("historial_artista", "relaciones")
                 else "genero"] = extra
        msgs.append({"role": "user", "content": frase})
        msgs.append({"role": "assistant", "content": [{
            "type": "tool_use", "id": f"ej_{len(msgs)}",
            "name": "clasificar", "input": args}]})
        msgs.append({"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": f"ej_{len(msgs) - 1}",
            "content": "ok"}]})
    msgs.append({"role": "user", "content": texto})
    return msgs


async def clasificar(texto: str) -> tuple[Intent | None, dict]:
    """Devuelve (Intent, uso). Intent es None si no se pudo clasificar.

    Nunca levanta: si la API falla o tarda, el turno sigue por el fallback.
    Un reproductor no puede depender de la red para entender "pausá".
    """
    uso = {"in": 0, "out": 0, "cache_read": 0}
    try:
        resp = await client.messages.create(
            model=settings.claude_model,
            max_tokens=200,
            system=SYSTEM,
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "clasificar"},
            messages=_mensajes(texto),
            timeout=settings.harness_clasificador_timeout,
        )
    except Exception as e:
        logger.warning("clasificador no disponible (%s): sigo sin él",
                       type(e).__name__)
        return None, uso

    u = resp.usage
    uso["in"] = u.input_tokens
    uso["out"] = u.output_tokens
    uso["cache_read"] = getattr(u, "cache_read_input_tokens", 0) or 0

    bloque = next((b for b in resp.content if b.type == "tool_use"), None)
    if bloque is None:
        logger.warning("el clasificador no devolvió tool_use")
        return None, uso

    datos = bloque.input or {}
    nombre = datos.get("intent")
    if nombre not in INTENTS:
        logger.warning("intent fuera del catálogo: %r", nombre)
        return None, uso

    slots = {}
    if datos.get("artista"):
        slots["artista"] = str(datos["artista"]).strip()
    if datos.get("genero"):
        slots["genero"] = str(datos["genero"]).strip().lower()
    if datos.get("cuando"):
        slots["cuando"] = str(datos["cuando"]).strip().lower()
    # El prompt del curador es el texto original: el clasificador entiende la
    # intención, no reescribe el pedido.
    if nombre == "playlist":
        from app.harness.router import sin_verbo
        slots["prompt"] = sin_verbo(texto)

    conf = float(datos.get("confidence") or 0)
    logger.info("clasificador: %r → %s (%.2f) %d tok",
                texto[:60], nombre, conf, uso["in"] + uso["out"])
    return Intent(name=nombre, slots=slots, confidence=conf, stage=HAIKU), uso
