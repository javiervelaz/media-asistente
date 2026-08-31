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
    # infinitivos: "parar" caia al fallback y de ahi al curador
    ("parar",                       "control_pause", {}),
    ("pausar",                      "control_pause", {}),
    ("seguir",                      "control_play", {}),
    ("pasar",                       "control_next", {}),
    ("subir",                       "control_vol_up", {}),
    ("bajar",                       "control_vol_down", {}),
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
    ("poné algo tranquilo para la cena",                "playlist", None),
    # verbos que faltaban — caian al freno de gasto en vez de ser pedidos
    ("Reproducir talking heads",                        "playlist", {"prompt": "talking heads"}),
    ("Pone the Beatles",                                "playlist", {"prompt": "the Beatles"}),
    ("buscame Café Tacvba",                             "playlist", {"prompt": "Café Tacvba"}),

    # El caso que costo 19k tokens: el normalizador come "charly" como
    # vocativo, queda "hola", ningun patron matcheaba y el curador leia el
    # texto original como un pedido de Charly Garcia.
    ("hola",                        "saludo", {}),
    ("hola charly",                 "saludo", {}),
    ("che charly, buenas",          "saludo", {}),
    ("buen día",                    "saludo", {}),
    ("ayuda",                       "ayuda", {}),
    ("qué sabés hacer",             "ayuda", {}),

    # --- H2: lo que antes caia al fallback y costaba ~19k tokens ---
    ("qué escuché hoy",                     "historial_periodo", None),
    ("qué escuché la semana pasada",        "historial_periodo", None),
    ("qué sonó ayer",                       "historial_periodo", None),
    ("qué escuché",                         "historial_periodo", None),
    ("qué escuché de Sumo",                 "historial_artista", {"artista": "sumo"}),
    ("cuántas veces escuché de Spinetta",   "historial_artista", None),
    ("qué escucho más",                     "top_escuchados", {}),
    ("mis más escuchados",                  "top_escuchados", {}),
    ("qué me salteo",                       "salteados", {}),
    ("qué tengo en vinilo sin escuchar",    "nunca_escuchado", {}),
    ("discos sin escuchar",                 "nunca_escuchado", {}),
    ("qué discos tengo de Killing Joke",    "discografia", {"artista": "killing joke"}),
    ("discografía de Wire",                 "discografia", {"artista": "wire"}),
    ("quién tocó con Luis Alberto Spinetta","relaciones", None),
    ("con quién tocó Charly García",        "relaciones", None),
    ("efemérides",                          "efemerides_hoy", {}),
    ("qué se cumple hoy",                   "efemerides_hoy", {}),

    # El pedido que motivo todo el bloque: reproduce, no lista.
    ("algo que haya escuchado hoy",              "reproducir_historial", None),
    ("poneme algo que haya escuchado hoy",       "reproducir_historial", None),
    ("poné algo que escuché ayer",               "reproducir_historial", None),
    ("volvé a poner lo que sonó esta semana",    "reproducir_historial", None),

    # Respuesta a una oferta. "dale" y "ok" antes se los comia el
    # normalizador —eran "ruido" al principio de una orden— y quedaban en "".
    ("dale",                        "confirmar", {}),
    ("dale?",                       "confirmar", {}),
    ("ok",                          "confirmar", {}),
    ("sí",                          "confirmar", {}),
    ("obvio",                       "confirmar", {}),
    ("ponelo",                      "confirmar", {}),
    ("de una",                      "confirmar", {}),
    ("no",                          "rechazar", {}),
    ("mejor no",                    "rechazar", {}),
    ("ahora no",                    "rechazar", {}),

    # --- H4: objetivos ---
    # la colección, directo y sin curador
    ("poné algo de mi colección",           "reproducir_coleccion", {}),
    ("algo de mi colección",                "reproducir_coleccion", {}),
    ("poneme un vinilo",                    "reproducir_coleccion", {}),
    ("dame algo del estante",               "reproducir_coleccion", {}),
    ("mis discos",                          "reproducir_coleccion", {}),

    ("cómo voy",                            "estado_objetivos", {}),
    ("cómo voy con mis objetivos",          "estado_objetivos", {}),
    ("mis objetivos",                       "estado_objetivos", {}),
    ("quiero escuchar más de mi colección", "set_objetivo_coleccion", {}),
    ("más vinilo",                          "set_objetivo_coleccion", {}),
    ("quiero escuchar más de mi colección 60", "set_objetivo_coleccion", {"n": 60}),
    ("quiero descubrir 5 artistas",         "set_objetivo_descubrimiento", {"n": 5}),
    ("descubrir bandas nuevas",             "set_objetivo_descubrimiento", {}),
    ("quiero escuchar álbumes enteros",     "set_objetivo_profundidad", {}),
    ("discos enteros",                      "set_objetivo_profundidad", {}),
    ("quiero escuchar más jazz",            "set_objetivo_genero", {"genero": "jazz"}),
    ("quiero escuchar más post punk",       "set_objetivo_genero", {"genero": "post punk"}),
    ("borrame el objetivo de vinilo",       "borrar_objetivo", {"que": "vinilo"}),
    ("sacá el objetivo de jazz",            "borrar_objetivo", {"que": "jazz"}),
]

#: Tienen que caer en no_entendido. Un patron que se coma alguna de estas es
#: un bug peor que un patron faltante.
#: Consultas de LECTURA que todavia no tienen intent (bloque H2). Hoy caen al
#: fallback y, con harness_fallback_playlist=True, terminan armando una
#: playlist en vez de responder. Es el gasto que H2 elimina: cada una de estas
#: frases va a aparecer en `SELECT text_in FROM turn_log WHERE stage='fallback'`.
NEGATIVOS: list[str] = [
    "ponete a bailar",                     # ni comando ni consulta
    "",
]


#: Textos que NO matchean ningun patron y son demasiado cortos como para
#: mandarlos al curador. Tienen que terminar en `repreguntar`, no en playlist.
CORTOS: list[str] = ["gracias", "mmm", "asdf", "🎵", "aaa"]


def _fallback(texto: str) -> str:
    """Reproduce la decision de chat._resolver_fallback sin importar config."""
    from app.harness.router import normalizar
    it = etapa1(texto)
    if it:
        return it.name
    return "repreguntar" if len(normalizar(texto).split()) < 2 else "playlist"


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

    print(f"\n--- cortos que no deben llegar al curador ({len(CORTOS)}) ---")
    fugas = 0
    for t in CORTOS:
        got = _fallback(t)
        if got != "repreguntar":
            fugas += 1
            print(f"  FUGA  {t!r} -> {got}  (19k tokens por nada)")
    print(f"  {len(CORTOS) - fugas}/{len(CORTOS)} repreguntan en vez de gastar")
    if fugas:
        fallos.append("cortos que llegan al curador")

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
