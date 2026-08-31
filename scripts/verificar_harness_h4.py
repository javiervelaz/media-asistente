"""Objetivos (H4) contra Neon. No gasta un token.

Verifica lo que puede salir mal en silencio:
  1. La tabla `goals` existe con las columnas que usa el codigo.
  2. La derivacion de progreso coincide con un conteo manual independiente.
     Si el SQL de `goals.py` y una cuenta hecha aparte no dan lo mismo, el
     numero que ve el usuario esta mal y nadie se entera.
  3. La guarda de muestra chica: con pocas escuchas NO se inyecta sesgo.
  4. Hay material real para cada tipo de objetivo.

Los objetivos que crea son temporales y se borran al final.

Uso:  python -m scripts.verificar_harness_h4
"""
import asyncio
import logging
import time

from app.db import close_pool, execute, fetch, fetchrow
from app.harness import goals, queries as q

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(message)s")

SALA = "__verificacion__"
LENTO_MS = 300


async def _schema() -> list[str]:
    print("=" * 66)
    print("SCHEMA")
    print("=" * 66)
    fallos = []
    rows = await fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'goals'")
    reales = {r["column_name"] for r in rows}
    if not reales:
        print("  FALTA la tabla goals — corré: python -m scripts.migrate_goals")
        return ["tabla goals"]
    faltan = [c for c in ("id", "room_id", "kind", "spec", "window_days",
                          "active", "created_at") if c not in reales]
    if faltan:
        print(f"  goals: FALTAN {faltan}")
        fallos.append(f"goals.{faltan}")
    else:
        print(f"  ok  goals ({len(reales)} columnas)")

    # El codec de jsonb: sin el, asyncpg entrega `spec` como str y todo el
    # codigo que hace spec.get(...) revienta. Se prueba con un valor real,
    # no mirando la config del pool.
    tipo = await fetchrow("SELECT '{\"a\": 1}'::jsonb AS v")
    if isinstance(tipo["v"], dict):
        print("  ok  jsonb llega como dict (codec registrado en el pool)")
    else:
        print(f"  FALLA: jsonb llega como {type(tipo['v']).__name__} — "
              "falta el codec en db.py:_init_conn")
        fallos.append("codec jsonb ausente")

    tags = await fetchrow(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'artists' AND column_name = 'tags'")
    if tags:
        print(f"  ok  artists.tags = {tags['data_type']} "
              "(el objetivo de género depende de esto)")
    else:
        print("  FALTA artists.tags — el objetivo de género no va a funcionar")
        fallos.append("artists.tags")
    return fallos


async def _derivacion() -> list[str]:
    """El numero que ve el usuario vs una cuenta hecha por otro camino."""
    print()
    print("=" * 66)
    print("DERIVACIÓN — el SQL de goals.py contra un conteo independiente")
    print("=" * 66)
    fallos = []
    dias = 30

    # colección: contamos a mano las completas y cuántas son de weight=1
    manual = await fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (
                   WHERE EXISTS (
                       SELECT 1 FROM recordings rc
                       JOIN ephemerides e ON e.mbid = rc.release_mbid::text
                       WHERE rc.mbid = ph.recording_mbid AND e.weight = 1)
               ) AS del_estante
        FROM play_history ph
        WHERE ph.completed AND ph.started_at > now() - make_interval(days => $1)
        """, dias)

    e = await goals.progreso({"kind": "coleccion", "spec": {"target": 0.4},
                              "window_days": dias})
    esperado = (100.0 * manual["del_estante"] / manual["total"]
                if manual["total"] else 0.0)
    ok = abs(e["actual"] - esperado) < 1.0
    print(f"  colección: goals.py dice {e['actual']:.1f}%, "
          f"el conteo manual {esperado:.1f}%  {'ok' if ok else 'DIFIEREN'}")
    print(f"    ({manual['del_estante']} de {manual['total']} escuchas completas)")
    if not ok:
        fallos.append("la derivación de colección no coincide con el conteo manual")

    for kind, spec in (("descubrimiento", {"target": 5}),
                       ("profundidad", {"target": 0.5})):
        t0 = time.monotonic()
        e = await goals.progreso({"kind": kind, "spec": spec, "window_days": dias})
        ms = int((time.monotonic() - t0) * 1000)
        u = "" if e["unidad"] == "%" else f" {e['unidad']}"
        print(f"  {kind}: {e['actual']:.1f}{u} de {e['target']:.1f}{u} "
              f"· muestra {e['muestra']} · {ms} ms"
              + ("  << LENTO" if ms > LENTO_MS else ""))
        if ms > LENTO_MS:
            fallos.append(f"{kind} tarda {ms} ms")
    return fallos


async def _guarda() -> list[str]:
    """Con muestra chica no se sesga. Un ratio sobre 4 tracks es ruido."""
    print()
    print("=" * 66)
    print("GUARDA DE MUESTRA CHICA")
    print("=" * 66)
    fallos = []

    chico = {"kind": "coleccion", "spec": {"target": 0.4}, "dias": 30,
             "actual": 10.0, "target": 40.0, "muestra": 3,
             "unidad": "%", "suficiente": False, "cumplido": False}
    if goals.linea_para_curador(chico) is None:
        print(f"  ok  con muestra 3 (< {goals.MUESTRA_MINIMA}) NO se inyecta sesgo")
    else:
        print("  FALLA: se sesga el curador con una muestra de 3 escuchas")
        fallos.append("la guarda de muestra chica no frena")

    grande = {**chico, "muestra": 140, "suficiente": True}
    linea = goals.linea_para_curador(grande)
    if linea:
        print(f"  ok  con muestra 140 sí se inyecta:\n      {linea}")
    else:
        print("  FALLA: con muestra suficiente no se inyecta nada")
        fallos.append("no se inyecta el sesgo con muestra suficiente")

    cumplido = {**grande, "actual": 55.0, "cumplido": True}
    if goals.linea_para_curador(cumplido) is None:
        print("  ok  un objetivo cumplido deja de sesgar")
    else:
        print("  FALLA: sigue sesgando un objetivo ya cumplido")
        fallos.append("objetivo cumplido sigue sesgando")
    return fallos


async def _coleccion() -> list[str]:
    """De donde sale el material del estante, y por que puede no haber."""
    print()
    print("=" * 66)
    print("ESTADO DE LA COLECCIÓN (weight = 1)")
    print("=" * 66)
    est = await q.estado_coleccion()
    print(f"  discos marcados como tuyos : {est['del_estante']}")
    print(f"  …con mbid de MusicBrainz   : {est['con_mbid']}")
    print(f"  …con tracklist en recordings: {est['con_tracklist']}")

    rows = await fetch(
        "SELECT weight, count(*) AS n, count(mbid) AS con_mbid "
        "FROM ephemerides GROUP BY weight ORDER BY weight")
    print("\n  por weight:")
    for r in rows:
        print(f"    weight={r['weight']}: {r['n']:5} filas, "
              f"{r['con_mbid']:5} con mbid")

    from app.harness import render
    problema = render.coleccion_vacia(est)
    print()
    if problema:
        print("  Sin material, y el bot ahora dice por qué:")
        print(f"    {problema}")
    else:
        print("  ok  hay colección cargada con tracklist: el bot puede "
              "distinguir\n      \"escuchaste todo\" de \"no hay datos\".")
    return []


async def _material() -> list[str]:
    print()
    print("=" * 66)
    print("MATERIAL — hay con qué armar la cola de cada objetivo")
    print("=" * 66)
    fallos = []
    genero = await fetchrow(
        "SELECT unnest(tags) AS tag, count(*) AS n FROM artists "
        "WHERE tags IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 1")
    tag = genero["tag"] if genero else "rock"
    if genero:
        print(f"  tag más frecuente en la base: {tag!r} ({genero['n']} artistas)")

    for kind, spec in (("coleccion", {}), ("descubrimiento", {}),
                       ("profundidad", {}),
                       ("genero", {"genero": tag, "tags": [tag]})):
        t0 = time.monotonic()
        try:
            tracks = await q.tracks_para_objetivo(kind, spec, 14)
        except Exception as ex:
            print(f"  FALLA  {kind}: {type(ex).__name__}: {ex}")
            fallos.append(f"tracks_para_objetivo({kind})")
            continue
        ms = int((time.monotonic() - t0) * 1000)
        listos = sum(1 for t in tracks if t.get("listo"))
        print(f"  {kind:16} {len(tracks):3} tracks ({listos} ya resueltos)  {ms:4} ms"
              + ("  << LENTO" if ms > LENTO_MS else ""))
        for t in tracks[:2]:
            print(f"      · {t['artist']} — {t['title']}")
        if ms > LENTO_MS:
            fallos.append(f"material de {kind} tarda {ms} ms")
        if not tracks and kind in ("coleccion", "profundidad"):
            print(f"      (sin material: normal si todavía no hay señal para {kind})")
    return fallos


def _spec_defensivo() -> list[str]:
    """`_spec` tiene que aguantar str, dict, None y basura."""
    print()
    print("=" * 66)
    print("NORMALIZACIÓN DE spec")
    print("=" * 66)
    fallos = []
    casos = [
        ({"spec": {"target": 0.6}}, {"target": 0.6}, "dict"),
        ({"spec": '{"target": 0.6}'}, {"target": 0.6}, "str (sin codec)"),
        ({"spec": None}, {}, "None"),
        ({"spec": "no es json"}, {}, "basura"),
        ({}, {}, "ausente"),
    ]
    for entrada, esperado, nombre in casos:
        got = goals._spec(entrada)
        ok = got == esperado
        print(f"  {'ok  ' if ok else 'FALLA'} {nombre:18} -> {got}")
        if not ok:
            fallos.append(f"_spec con {nombre}")
    return fallos


async def _ciclo() -> list[str]:
    """Declarar, leer, borrar — en una sala aparte para no tocar la tuya."""
    print()
    print("=" * 66)
    print("CICLO declarar → leer → borrar")
    print("=" * 66)
    fallos = []
    await goals.declarar("coleccion", {"target": 0.4}, room_id=SALA)
    await goals.declarar("coleccion", {"target": 0.6}, room_id=SALA)  # reemplaza
    act = await goals.activos(SALA)
    if len(act) == 1 and act[0]["spec"].get("target") == 0.6:
        print("  ok  declarar dos veces reemplaza, no acumula")
    else:
        print(f"  FALLA: quedaron {len(act)} objetivos activos: {act}")
        fallos.append("declarar acumula en vez de reemplazar")

    n = await goals.borrar("coleccion", SALA)
    resto = await goals.activos(SALA)
    if n == 1 and not resto:
        print("  ok  borrar desactiva")
    else:
        print(f"  FALLA: borrar devolvió {n}, quedan {len(resto)}")
        fallos.append("borrar no desactiva")
    await execute("DELETE FROM goals WHERE room_id = $1", SALA)
    return fallos


async def main() -> None:
    try:
        fallos = await _schema()
        if "tabla goals" in fallos:
            print("\nCorré primero: python -m scripts.migrate_goals")
            return
        fallos += _spec_defensivo()
        fallos += await _ciclo()
        fallos += await _derivacion()
        fallos += await _guarda()
        fallos += await _coleccion()
        fallos += await _material()

        print()
        print("=" * 66)
        if fallos:
            print(f"FALLOS ({len(fallos)}):")
            for f in fallos:
                print(f"  · {f}")
        else:
            print("OK — objetivos derivados de play_history, sesgo con guarda, "
                  "y material real para cada tipo.")
        print("llamadas a la API de Anthropic: 0")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
