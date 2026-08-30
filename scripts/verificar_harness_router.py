"""Cobertura del router de etapa 1. No gasta un token ni toca la base.

Dos numeros que importan:
  - aciertos: de las frases que DEBEN matchear, cuantas matchean bien
  - cobertura: que porcentaje del set se resuelve gratis (stage=regex)

Los falsos positivos son el modo de falla caro: un patron laxo que se come una
frase larga responde cualquier cosa con confianza 1.0 y nunca llega al
fallback. Por eso el set incluye NEGATIVOS que TIENEN que caer en
no_entendido.

Uso:  python -m scripts.verificar_harness_router
"""
import sys

from app.harness.intents import IMPLEMENTADOS
from app.harness.router import etapa1, normalizar, rutear

# (frase, intent esperado, slots esperados o None)
POSITIVOS: list[tuple[str, str, dict | None]] = [
    # control basico, como se habla de verdad
    ("pasala",                      "control_next", {}),
    ("Pasá",                        "control_next", {}),
    ("che charly pasala porfa",     "control_next", {}),
    ("siguiente",                   "control_next", {}),
    ("otra",                        "control_next", {}),
    ("esta no",                     "control_next", {}),
    ("la que sigue",                "control_next", {}),
    ("anterior",                    "control_prev", {}),
    ("volvé atrás",                 "control_prev", {}),
    ("pausá",                       "control_pause", {}),
    ("pará",                        "control_pause", {}),
    ("frenala",                     "control_pause", {}),
    ("shhh",                        "control_pause", {}),
    ("seguí",                       "control_play", {}),
    ("dale play",                   "control_play", {}),
    ("basta",                       "control_stop", {}),
    ("cortala",                     "control_stop", {}),
    ("de nuevo",                    "control_replay", {}),
    ("repetila",                    "control_replay", {}),
    ("desde el principio",          "control_replay", {}),

    # volumen: pelado y con numero
    ("subile",                      "control_vol_up", {}),
    ("subí",                        "control_vol_up", {}),
    ("más fuerte",                  "control_vol_up", {}),
    ("no se escucha",               "control_vol_up", {}),
    ("subile 20",                   "control_vol_up", {"delta": 20}),
    ("bajale",                      "control_vol_down", {}),
    ("está muy fuerte",             "control_vol_down", {}),
    ("bajale 15",                   "control_vol_down", {"delta": 15}),
    ("poné el volumen en 40",       "control_vol_set", {"level": 40}),
    ("ponelo en 70",                "control_vol_set", {"level": 70}),
    ("volumen 55",                  "control_vol_set", {"level": 55}),
    ("volumen en 200",              "control_vol_set", {"level": 100}),   # clamp

    # estado
    ("qué suena",                   "estado_actual", {}),
    ("que es esto",                 "estado_actual", {}),
    ("quién canta",                 "estado_actual", {}),
    ("qué estoy escuchando",        "estado_actual", {}),
    ("qué sigue",                   "estado_cola", {}),
    ("qué viene",                   "estado_cola", {}),
    ("la cola",                     "estado_cola", {}),

    # pedidos curatoriales: clasifican gratis, pero el ejecutor gasta.
    # El valor de reconocerlos en la etapa 1 no es ahorrar el turno, es que
    # turn_log distinga gasto intencional (stage=regex) de gasto por no
    # haber entendido (stage=fallback).
    ("poneme algo tranquilo para cocinar",              "playlist", None),
    ("armá una playlist de post-punk británico de 1980","playlist", None),
    ("poné algo de los discos que tengo en vinilo",     "playlist", None),
    ("quiero escuchar algo de jazz modal",              "playlist", None),
    ("tirame cumbia santafesina",                       "playlist", None),
]

#: Tienen que caer en no_entendido. Un patron que se coma alguna de estas es
#: un bug peor que un patron faltante.
#: Consultas de LECTURA que todavia no tienen intent (bloque H2). Hoy caen al
#: fallback y, con harness_fallback_playlist=True, terminan armando una
#: playlist en vez de responder. Es el gasto que H2 elimina: cada una de estas
#: frases va a aparecer en `SELECT text_in FROM turn_log WHERE stage='fallback'`.
NEGATIVOS: list[str] = [
    "qué escuché la semana pasada",
    "qué discos tengo de Killing Joke",
    "quién tocó con Luis Alberto Spinetta",
    "cómo voy con mis objetivos",
    "cuántas veces escuché Sumo este mes",
    "qué me salteo siempre",
    "",
]


def main() -> int:
    print("=" * 62)
    print("ROUTER ETAPA 1 — cobertura y falsos positivos")
    print("=" * 62)

    fallos: list[str] = []

    print(f"\n--- positivos ({len(POSITIVOS)}) ---")
    for frase, esperado, slots in POSITIVOS:
        it = etapa1(frase)
        got = it.name if it else "no_entendido"
        ok = got == esperado and (slots is None or it.slots == slots)
        if not ok:
            fallos.append(f"{frase!r}: esperaba {esperado}{slots or ''}, "
                          f"dio {got}{it.slots if it else ''}")
            print(f"  FAIL  {frase!r} -> {got} {it.slots if it else ''}")

    aciertos = len(POSITIVOS) - len(fallos)
    print(f"  {aciertos}/{len(POSITIVOS)} correctos")

    print(f"\n--- sin intent todavia ({len(NEGATIVOS)}) ---")
    fp = 0
    for frase in NEGATIVOS:
        it = etapa1(frase)
        if it is not None:
            fp += 1
            print(f"  FALSO POSITIVO  {frase!r} -> {it.name}")
    print(f"  {len(NEGATIVOS) - fp}/{len(NEGATIVOS)} caen al fallback (esperado)")

    print("\n--- normalizador ---")
    for f in ["Che Charly, pasala porfa", "¿Qué suena?", "SUBILE!!"]:
        print(f"  {f!r} -> {normalizar(f)!r}")

    print("\n--- integridad ---")
    huerfanos = {n for n, _ in _pares()} - IMPLEMENTADOS
    if huerfanos:
        print(f"  FAIL  patrones sin ejecutor: {sorted(huerfanos)}")
        fallos.append("patrones sin ejecutor")
    else:
        print("  todos los patrones tienen ejecutor")

    total = len(POSITIVOS) + len(NEGATIVOS)
    gratis = sum(1 for f, e, _ in POSITIVOS if etapa1(f) and e != "playlist")
    caros = sum(1 for _, e, _ in POSITIVOS if e == "playlist")
    print(f"\nturnos gratis: {gratis}/{total} ({100 * gratis / total:.0f}%)")
    print(f"turnos que gastan (playlist): {caros}")
    print(f"caen al fallback y hoy gastan sin querer: {len(NEGATIVOS) - fp} "
          f"-> los cubre el bloque H2")
    print(f"llamadas a la API en esta corrida: 0")

    if fallos or fp:
        print(f"\nFALLOS: {len(fallos)} + {fp} falsos positivos")
        return 1
    print("\nOK")
    return 0


def _pares():
    from app.harness.router import PATRONES
    return [(n, p) for n, p in PATRONES]


if __name__ == "__main__":
    sys.exit(main())
