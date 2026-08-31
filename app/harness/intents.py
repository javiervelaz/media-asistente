"""Catalogo de intents y contratos del harness.

Una sola fila de este catalogo gasta tokens (`playlist`). El resto sale de la
base que ya venis hidratando o del socket de mpv.
"""
from dataclasses import dataclass, field
from typing import Any

# --- etapas del router, en orden de costo -----------------------------------
REGEX = "regex"          # patrones: 0 tokens
HAIKU = "haiku"          # clasificador: ~400 in cacheados / 60 out  (H3)
FALLBACK = "fallback"    # no se entendio: se repregunta con plantilla
ERROR = "error"


@dataclass(slots=True)
class Intent:
    name: str
    slots: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    stage: str = REGEX


@dataclass(slots=True)
class Result:
    """Lo que devuelve un ejecutor. `reply` ya viene renderizado."""
    reply: str
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)


# --- catalogo ---------------------------------------------------------------
# modelo=None significa que el intent NUNCA llama a la API.

@dataclass(frozen=True, slots=True)
class Spec:
    name: str
    desc: str          # se reusa como descripcion en el tool schema del H3
    modelo: str | None = None


CATALOGO: tuple[Spec, ...] = (
    # --- control (mpv IPC) ---
    Spec("control_play",     "reanudar la reproduccion pausada"),
    Spec("control_pause",    "pausar o frenar la musica"),
    Spec("control_next",     "saltar al tema siguiente"),
    Spec("control_prev",     "volver al tema anterior"),
    Spec("control_stop",     "detener del todo y vaciar la cola"),
    Spec("control_replay",   "volver a empezar el tema actual"),
    Spec("control_vol_up",   "subir el volumen"),
    Spec("control_vol_down", "bajar el volumen"),
    Spec("control_vol_set",  "poner el volumen en un valor exacto"),

    # --- estado (memoria + mpv) ---
    Spec("estado_actual",    "que tema esta sonando ahora"),
    Spec("estado_cola",      "que temas vienen despues"),

    # --- lectura de la base (H2) ---
    Spec("historial_periodo",  "que se escucho en un rango de fechas"),
    Spec("historial_artista",  "que se escucho de un artista"),
    Spec("top_escuchados",     "los artistas o temas mas escuchados"),
    Spec("salteados",          "que se saltea siempre"),
    Spec("nunca_escuchado",    "discos de la coleccion en vinilo que nunca sonaron"),
    Spec("discografia",        "que discos hay de un artista"),
    Spec("relaciones",         "quien toco con quien, vinculos entre artistas"),
    Spec("efemerides_hoy",     "que aniversario de disco cae hoy"),
    Spec("reproducir_historial",
         "volver a poner lo que ya se escucho en un periodo"),
    Spec("reproducir_releases",
         "poner discos concretos que se acaban de listar"),
    Spec("confirmar_gasto",
         "se freno un turno caro y se pidio confirmacion"),
    Spec("confirmar", "aceptar lo que se acaba de ofrecer"),
    Spec("rechazar",  "rechazar lo que se acaba de ofrecer"),

    # --- objetivos (H4) ---
    Spec("estado_objetivos", "como viene contra los objetivos de escucha"),
    Spec("set_objetivo",     "declarar un objetivo de escucha"),

    # --- lo unico que gasta ---
    Spec("playlist", "armar una playlist nueva a partir de un pedido curatorial",
         modelo="curator"),

    # --- meta ---
    Spec("saludo", "un saludo, sin pedido concreto"),
    Spec("ayuda",  "que sabe hacer el bot"),
    Spec("repreguntar",  "demasiado corto para adivinar: se pide confirmacion"),
    Spec("no_entendido", "no se pudo clasificar el pedido"),
)

POR_NOMBRE: dict[str, Spec] = {s.name: s for s in CATALOGO}
NOMBRES: tuple[str, ...] = tuple(s.name for s in CATALOGO)

#: Los que estan implementados hoy. El router no puede devolver un intent
#: sin ejecutor: se registran aca a medida que se implementan los bloques.
IMPLEMENTADOS: set[str] = {
    "control_play", "control_pause", "control_next", "control_prev",
    "control_stop", "control_replay", "control_vol_up", "control_vol_down",
    "control_vol_set", "estado_actual", "estado_cola",
    "playlist", "saludo", "ayuda", "repreguntar",
    # H2
    "historial_periodo", "historial_artista", "top_escuchados", "salteados",
    "nunca_escuchado", "discografia", "relaciones", "efemerides_hoy",
    "reproducir_historial", "reproducir_releases",
    "confirmar", "rechazar",
}


def gratis(name: str) -> bool:
    spec = POR_NOMBRE.get(name)
    return spec is not None and spec.modelo is None
