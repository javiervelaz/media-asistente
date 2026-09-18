#!/usr/bin/env python3
"""H6 - las frases REALES que pagaron el clasificador, contra el router.

No hay frases inventadas aca. Las 26 salieron de:

    SELECT text_in, intent, stage FROM turn_log
    WHERE stage IN ('haiku','fallback') AND created_at > now() - interval '30 days'

La columna `haiku_dijo` es lo que el clasificador contesto cobrando ~3300
tokens. Vale leerla: en 8 de 26 se equivoco o se rindio. El objetivo de H6
no es solo que salgan gratis, es que salgan BIEN.

No toca la base ni la API:

    PYTHONPATH=. DATABASE_URL=postgresql://x:x@localhost/x python3 scripts/verificar_harness_h6.py
"""
import sys

from app.harness.router import etapa1

# (texto, intent esperado, lo que contesto Haiku cobrando)
CASOS = [
    # --- discos de UN artista del estante -------------------------------
    ("Que discos de Queen hay en la coleccion?",            "coleccion_consulta",   "coleccion_de_artista"),
    ("Que discos de Joaquin Sabina hay en la coleccion?",   "coleccion_consulta",   "coleccion_de_artista"),
    ("Que discos de Marilyn Manson hay en la coleccion?",   "coleccion_consulta",   "coleccion_de_artista"),
    ("De Andres Calamaro que tenemos en la coleccion?",     "coleccion_consulta",   "coleccion_de_artista"),
    ("que discos de the beatles tengo un mi coleccion?",    "coleccion_consulta",   "coleccion_de_artista"),
    ("que discos de frank sinastra tenemos en la coleccion?", "coleccion_consulta",  "discografia"),
    ("Buscar rolling stones en mi coleccion",               "coleccion_consulta",   "-"),
    ("arctick monkeys de la coleccion",                     "coleccion_consulta",   "-"),

    # --- el estante sin escuchar ----------------------------------------
    ("que discos de mi coleccion  estan sin escuchar?",     "nunca_escuchado",       "nunca_escuchado"),
    ("Que me queda sin escuchar de la coleccion?",          "nunca_escuchado",       "nunca_escuchado"),
    ("Que artistas de la coleccion no escuche aun?",        "nunca_escuchado",       "nunca_escuchado"),
    ("que tento sin ecuchar en el estante_",                "nunca_escuchado",       "nunca_escuchado"),
    ("sin escuchar?",                                       "nunca_escuchado",       "repreguntar"),
    ("que hay en la coleccion para escuchar?",              "nunca_escuchado",       "reproducir_coleccion"),
    ("Que puedo escuchar de mi coleccion?",                 "nunca_escuchado",       "reproducir_coleccion"),

    # --- el bloque nuevo: atributo sobre el estante ----------------------
    ("que artistas de jazz tengo en la coleccion?",         "coleccion_por_atributo", "set_objetivo_genero"),
    ("Listame artistas de jazz de la coleccion",            "coleccion_por_atributo", "repreguntar"),
    # Sin sustantivo desambiguador: entra por la rama de dos pasos
    # (artista primero, atributo despues). El intent es el generico.
    ("Cuantos canciones de jazz tengo en mi coleccion?",    "coleccion_consulta",     "repreguntar"),
    ("que tenemos de jazz?",                                "coleccion_consulta",     "playlist"),
    ("Que artistas de los 80 tenemos en la coleccion?",     "coleccion_por_atributo", "repreguntar"),
    ("Que artistas de los anios 80 puedo escuchar?",        "coleccion_por_atributo", "playlist"),
    ("que tengo artistas nacidos  en argentina tengo en mi coleccion?",
                                                            "coleccion_por_atributo", "repreguntar"),

    # --- typo de control: 3358 tokens por dos letras cambiadas de lugar --
    ("enxt",                                                "control_next",          "control_next"),
    ("proximo",                                             "control_next",          "-"),

    # --- estos SI tienen que seguir yendo al clasificador ---------------
    # No son consultas sobre el estante: son pedidos curatoriales.
    ("quiero descubrir mas jazz",                           None, "set_objetivo_descubrimiento"),
    ("Descubramos mas artistas del estilo de Donald faden", None, "set_objetivo_descubrimiento"),
]


def main() -> int:
    ancho = max(len(c[0]) for c in CASOS)
    ok = mal = 0
    print("=" * (ancho + 46))
    for texto, esperado, haiku_dijo in CASOS:
        it = etapa1(texto)
        got = it.name if it else None
        bien = got == esperado
        ok, mal = (ok + bien, mal + (not bien))
        marca = "ok " if bien else "MAL"
        destino = got or "-> clasificador"
        print(f"{marca} {texto:<{ancho}}  {destino:<24} (haiku: {haiku_dijo})")

    print("=" * (ancho + 46))
    gratis = sum(1 for c in CASOS if c[1] is not None)
    print(f"{ok}/{len(CASOS)} correctos - {mal} mal")
    print(f"objetivo: {gratis} de {len(CASOS)} frases reales tienen que salir "
          f"por etapa 1, a cero tokens")
    if mal:
        print(f"\nfaltan {mal} - cada una son ~3300 tokens y una respuesta "
              f"que hoy inventa el modelo")
        return 1
    print(f"\nOK - {gratis} x 3304 tok = {gratis * 3304} tokens que dejan de "
          f"pagarse, y {gratis} respuestas que pasan a salir de la base")
    return 0


if __name__ == "__main__":
    sys.exit(main())
