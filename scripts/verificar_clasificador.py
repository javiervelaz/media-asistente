"""El clasificador (H3) contra un set etiquetado. ESTE SÍ GASTA TOKENS.

Una clasificación cuesta ~400 tokens de entrada; el set completo son unas
30 llamadas. Es barato comparado con equivocarse: cada error del clasificador
que termina en `playlist` cuesta lo que cuesta una sesión de curador.

Lo que mide:
  1. Aciertos por intent, con matriz de confusión de lo que falla.
  2. Cuántas frases del set las agarra la ETAPA 1 — esas no deberían llegar
     acá, y si llegan es un patrón que falta (gratis de arreglar).
  3. El costo real por clasificación.
  4. Que la confianza baje cuando el mensaje es ambiguo.

Uso:
    python -m scripts.verificar_clasificador            # set fijo
    python -m scripts.verificar_clasificador --turnlog  # tus frases reales
"""
import asyncio
import logging
import sys

from app.db import close_pool, fetch
from app.harness import clasificador
from app.harness.router import etapa1

logging.basicConfig(level=logging.WARNING, format="%(message)s")

#: (frase, intent esperado). Deliberadamente NO son las de los ejemplos del
#: prompt: se mide generalización, no memoria.
SET = [
    # pedidos curatoriales dichos de formas que ningún patrón cubre
    ("tengo ganas de algo melancólico", "playlist"),
    ("música para una cena tranquila", "playlist"),
    ("algo parecido a lo de anoche pero más movido", "playlist"),
    ("quiero escuchar guitarras distorsionadas", "playlist"),

    # consultas
    ("contame qué sonó el fin de semana", "historial_periodo"),
    ("cuántas veces puse a Charly este mes", "historial_artista"),
    ("a quién escucho más últimamente", "top_escuchados"),
    ("qué temas paso siempre", "salteados"),
    ("qué discos míos siguen sin sonar", "nunca_escuchado"),
    ("qué sacó Wire", "discografia"),
    ("con quiénes se relaciona Spinetta", "relaciones"),
    ("algún aniversario hoy", "efemerides_hoy"),

    # reproducir
    ("volvé a lo de esta mañana", "reproducir_historial"),
    ("poné cosas de mis vinilos", "reproducir_coleccion"),
    ("quiero un álbum completo del estante", "reproducir_disco_coleccion"),

    # objetivos
    ("me propongo escuchar más tango", "set_objetivo_genero"),
    ("quiero descubrir bandas que no conozco", "set_objetivo_descubrimiento"),
    ("cómo vengo con lo que me propuse", "estado_objetivos"),

    # social — el bug de "hola charly", que no puede volver
    ("buen día Charly", "saludo"),
    ("qué tal, todo bien", "saludo"),
    ("qué cosas sabés hacer", "ayuda"),
    ("gracias!", "saludo"),

    # control dicho raro
    ("che, bajá un toque eso", "control_pause"),
    ("saltá a la que sigue", "control_next"),

    # ambiguos: lo correcto es CONFIANZA BAJA, no acertar
    ("algo", None),
    ("no sé", None),
    ("mmm", None),
]


async def _del_turnlog(limite: int = 25) -> list[tuple[str, str | None]]:
    """Tus frases reales: las que el router no entendió."""
    rows = await fetch(
        """
        SELECT text_in, count(*) AS n
        FROM turn_log
        WHERE stage <> 'regex' AND length(text_in) > 3
        GROUP BY text_in ORDER BY n DESC, max(created_at) DESC
        LIMIT $1
        """, limite)
    return [(r["text_in"], None) for r in rows]


async def main() -> None:
    usar_log = "--turnlog" in sys.argv
    try:
        casos = await _del_turnlog() if usar_log else SET
        if usar_log and not casos:
            print("turn_log no tiene frases fuera de la etapa 1 todavía.")
            print("Usá el bot unos días y volvé — o corré sin --turnlog.")
            return
        if usar_log:
            print(f"{len(casos)} frases reales de turn_log (sin etiqueta: "
                  "mirá si la clasificación te parece correcta)\n")

        aciertos = errores = ambiguos_ok = cache = 0
        etapa1_deberia = []
        confusion: dict = {}
        tokens = 0

        for frase, esperado in casos:
            # Si la etapa 1 ya la agarra, no debería llegar al clasificador.
            pat = etapa1(frase)
            if pat is not None:
                etapa1_deberia.append((frase, pat.name))

            it, uso = await clasificador.clasificar(frase)
            tokens += uso["in"] + uso["out"]
            cache += uso.get("cache_read") or 0
            if it is None:
                print(f"  SIN RESPUESTA  {frase!r}")
                errores += 1
                continue

            marca = ""
            if usar_log:
                # Sin etiqueta y sin respuesta correcta conocida: NO se juzga.
                # Confundir "no etiquetado" con "debe dudar" hacía que toda
                # clasificación acertada saliera como MAL.
                marca = "—"
            elif esperado is None:
                # Ambiguo a propósito: lo correcto es dudar, no acertar.
                ok = it.confidence < 0.6
                ambiguos_ok += ok
                marca = "ok (duda)" if ok else f"MAL (confianza {it.confidence:.2f})"
            elif it.name == esperado:
                aciertos += 1
                marca = "ok"
            else:
                errores += 1
                confusion[(esperado, it.name)] = \
                    confusion.get((esperado, it.name), 0) + 1
                marca = f"MAL (esperaba {esperado})"

            slots = {k: v for k, v in it.slots.items() if k != "prompt"}
            print(f"  {marca:28} {frase!r:46} → {it.name} "
                  f"({it.confidence:.2f}) {slots or ''}")

        etiquetados = [] if usar_log else [c for c in casos if c[1] is not None]
        ambiguos = [] if usar_log else [c for c in casos if c[1] is None]

        print()
        print("=" * 70)
        if etiquetados:
            print(f"aciertos: {aciertos}/{len(etiquetados)} "
                  f"({100 * aciertos / len(etiquetados):.0f}%)")
        if ambiguos:
            print(f"ambiguos que dudan bien: {ambiguos_ok}/{len(ambiguos)}")
        print(f"costo: {tokens} tokens en {len(casos)} clasificaciones "
              f"(~{tokens // max(1, len(casos))} por turno)")
        if cache:
            print(f"       {cache} leidos de cache "
                  f"({100 * cache // max(1, tokens):d}% del total)")
        if usar_log:
            print("\nEstas frases no tienen etiqueta: revisá vos si cada")
            print("clasificación es la que esperabas. Las que estén mal se")
            print("arreglan con un ejemplo en el prompt o, mejor, con un")
            print("patrón en la etapa 1 para que ni lleguen acá.")

        if confusion:
            print("\nconfusiones (esperado → devuelto):")
            for (esp, got), n in sorted(confusion.items(), key=lambda x: -x[1]):
                print(f"  {n}x  {esp} → {got}")
            print("\n  Una confusión hacia `playlist` es la cara: cada una es")
            print("  una sesión de curador que no correspondía.")

        if etapa1_deberia:
            print(f"\n{len(etapa1_deberia)} frases del set las agarra la ETAPA 1:")
            for frase, intent in etapa1_deberia[:10]:
                print(f"  {frase!r} → {intent}")
            print("  Esas no llegan al clasificador en producción. Si aparecen")
            print("  seguido en turn_log con stage='haiku', falta un patrón —")
            print("  que es gratis de agregar y le saca costo a la etapa 2.")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
