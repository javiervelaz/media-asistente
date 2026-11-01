"""Humor del compañero. Funcion pura: sin base, sin mpv, sin reloj propio.

Por que pura: el humor es lo que mas se va a iterar (A2 le suma rasgos
persistentes, teletrabajo, reuniones) y tiene que poder probarse con un
`datetime` armado a mano, sin levantar nada. `scripts/verificar_companion.py`
la cubre entera.

Reglas en orden de prioridad — gana la primera que aplica:

  confundido  mpv no responde. No es un humor: es el companero diciendo
              "algo anda mal" sin mentir que esta todo bien.
  encantado   suena un disco que tenes en vinilo.
  contento    suena un artista de tu coleccion (otro disco suyo).
  atento      suena cualquier otra cosa.
  expectante  hay un tema en pausa.
  dormido     sin musica, dentro de la ventana de sueño.
  aburrido    sin musica hace mas de `aburrido_min`.
  tranquilo   sin musica, recien.
"""
from dataclasses import dataclass
from datetime import datetime

MOODS = ("confundido", "encantado", "contento", "atento", "expectante",
         "dormido", "aburrido", "tranquilo")


@dataclass(frozen=True)
class Contexto:
    ahora: datetime                    # hora LOCAL
    mpv_ok: bool
    sonando: bool                      # hay track y no esta en pausa
    en_pausa: bool
    de_vinilo: bool = False            # el release esta en tu coleccion
    artista_coleccion: bool = False    # el artista tiene algun vinilo tuyo
    silencio_desde: datetime | None = None


def en_ventana(hora: int, desde: int, hasta: int) -> bool:
    """¿`hora` cae en [desde, hasta)? Si desde > hasta, cruza la medianoche.
    desde == hasta = ventana vacia (nunca duerme)."""
    desde, hasta = desde % 24, hasta % 24
    if desde == hasta:
        return False
    if desde < hasta:
        return desde <= hora < hasta
    return hora >= desde or hora < hasta


def humor(ctx: Contexto, dormir_desde: int = 0, dormir_hasta: int = 7,
          aburrido_min: int = 20) -> str:
    if not ctx.mpv_ok:
        return "confundido"
    if ctx.sonando:
        if ctx.de_vinilo:
            return "encantado"
        if ctx.artista_coleccion:
            return "contento"
        return "atento"
    if ctx.en_pausa:
        return "expectante"
    if en_ventana(ctx.ahora.hour, dormir_desde, dormir_hasta):
        return "dormido"
    if ctx.silencio_desde is not None:
        minutos = (ctx.ahora - ctx.silencio_desde).total_seconds() / 60
        if minutos >= aburrido_min:
            return "aburrido"
    return "tranquilo"
