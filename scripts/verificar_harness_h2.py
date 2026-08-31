"""Las 9 consultas del H2 contra Neon. No gasta un token.

Tres cosas, en orden:
  1. El schema: cada columna que usan las queries existe de verdad. El SQL se
     escribio leyendo las queries del repo, no un \\d de la base.
  2. Cada query corre y devuelve algo con la forma esperada.
  3. Cuanto tarda. Si alguna pasa de 300 ms desde la Pi es indice faltante,
     no falta de LLM.

Uso:  python -m scripts.verificar_harness_h2
"""
import asyncio
import logging
import time

from app.db import close_pool, fetch, fetchrow
from app.harness import queries as q

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(message)s")

LENTO_MS = 300

ESPERADO = {
    "play_history": ["artist", "title", "started_at", "completed", "skipped",
                     "recording_mbid", "artist_mbid", "youtube_id", "room_id"],
    "artists": ["mbid", "name", "country"],
    "artist_relations": ["source_mbid", "target_mbid", "rel_type"],
    "releases": ["mbid", "artist_mbid", "title", "first_release_date",
                 "primary_type", "secondary_types"],
    "recordings": ["mbid", "release_mbid", "title", "position", "length_ms"],
    "track_resolutions": ["recording_mbid", "youtube_id", "fail_count",
                          "play_count"],
    "ephemerides": ["artist", "album", "release_date", "month_day", "mbid",
                    "weight"],
}


async def _schema() -> list[str]:
    print("=" * 66)
    print("SCHEMA")
    print("=" * 66)
    fallos = []
    for tabla, cols in ESPERADO.items():
        rows = await fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = $1", tabla)
        reales = {r["column_name"]: r["data_type"] for r in rows}
        if not reales:
            print(f"  FALTA la tabla {tabla}")
            fallos.append(f"tabla {tabla}")
            continue
        faltan = [c for c in cols if c not in reales]
        if faltan:
            print(f"  {tabla}: FALTAN {faltan}")
            print(f"    tiene: {sorted(reales)}")
            fallos.append(f"{tabla}.{faltan}")
        else:
            print(f"  ok  {tabla} ({len(reales)} columnas)")

    # pg_trgm: sin esto, resolver_artista no matchea nada
    ext = await fetchrow(
        "SELECT 1 AS ok FROM pg_extension WHERE extname = 'pg_trgm'")
    if ext:
        print("  ok  pg_trgm instalada")
    else:
        print("  FALTA pg_trgm — resolver_artista no va a matchear nunca")
        fallos.append("pg_trgm")

    # ephemerides.mbid es TEXT y se castea a uuid en nunca_escuchado()
    tipo = await fetchrow(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'ephemerides' AND column_name = 'mbid'")
    if tipo:
        print(f"  ephemerides.mbid = {tipo['data_type']} "
              f"(las queries lo castean a uuid)")
    return fallos


async def _ventanas() -> list[str]:
    print()
    print("=" * 66)
    print("PARSER DE VENTANAS (sin base)")
    print("=" * 66)
    fallos = []
    casos = ["", "hoy", "ayer", "anteayer", "esta semana", "la semana pasada",
             "este mes", "el mes pasado", "los ultimos 7 dias",
             "ultimas 2 semanas", "ultimos 3 meses"]
    for c in casos:
        v = q.ventana(c)
        print(f"  {c!r:24} -> {v.etiqueta:20} "
              f"{v.desde:%d/%m %H:%M} .. {v.hasta:%d/%m %H:%M}")
        if v.hasta < v.desde:
            fallos.append(f"ventana invertida: {c!r}")
    return fallos


async def _consultas() -> list[str]:
    print()
    print("=" * 66)
    print("CONSULTAS")
    print("=" * 66)
    fallos = []

    # un artista real de la base para las que necesitan mbid
    a = await fetchrow(
        "SELECT mbid, name FROM artists "
        "WHERE EXISTS (SELECT 1 FROM releases r WHERE r.artist_mbid = artists.mbid) "
        "ORDER BY random() LIMIT 1")
    if a:
        print(f"  artista de prueba: {a['name']}\n")

    hoy = q.ventana("hoy")
    mes = q.ventana("este mes")

    casos = [
        ("historial_periodo (hoy)", q.historial_periodo(hoy)),
        ("historial_periodo (mes)", q.historial_periodo(mes)),
        ("top_escuchados", q.top_escuchados(30)),
        ("salteados", q.salteados(90)),
        ("nunca_escuchado", q.nunca_escuchado()),
        ("efemerides_hoy", q.efemerides_hoy()),
        ("tracks_del_periodo (hoy)", q.tracks_del_periodo(hoy)),
        ("tracks_del_periodo (mes)", q.tracks_del_periodo(mes)),
        ("tracks_de_releases (vacio)", q.tracks_de_releases([])),
        ("costo_tipico", q.costo_tipico()),
    ]
    if a:
        casos += [
            ("resolver_artista (exacto)", q.resolver_artista(a["name"])),
            ("resolver_artista (basura)", q.resolver_artista("zzqxwv")),
            ("historial_artista", q.historial_artista(a["mbid"], a["name"], 90)),
            ("discografia", q.discografia(a["mbid"])),
            ("relaciones", q.relaciones(a["mbid"])),
        ]

    for nombre, coro in casos:
        t0 = time.monotonic()
        try:
            out = await coro
        except Exception as e:
            print(f"  FALLA  {nombre}: {type(e).__name__}: {e}")
            fallos.append(nombre)
            continue
        ms = int((time.monotonic() - t0) * 1000)
        n = len(out) if isinstance(out, list) else (1 if out else 0)
        marca = "  << LENTO" if ms > LENTO_MS else ""
        print(f"  ok  {nombre:28} {n:3} filas  {ms:4} ms{marca}")
        if ms > LENTO_MS:
            fallos.append(f"{nombre} tarda {ms} ms")

    return fallos


async def _muestras() -> None:
    print()
    print(f"  costo tipico de un turno de curador: {await q.costo_tipico():,} tokens")
    print("  (sale de turn_log; si no hay datos todavia usa el default)")

    print()
    print("=" * 66)
    print("MUESTRAS — lo que va a ver el usuario")
    print("=" * 66)
    from app.harness import render

    hoy = q.ventana("hoy")
    print("\n[qué escuché hoy]")
    print(render.historial_periodo(await q.historial_periodo(hoy), hoy.etiqueta))

    print("\n[qué escucho más]")
    print(render.top_escuchados(await q.top_escuchados(30), 30))

    print("\n[qué tengo en vinilo sin escuchar]")
    print(render.nunca_escuchado(await q.nunca_escuchado()))

    print("\n[efemérides]")
    print(render.efemerides_hoy(await q.efemerides_hoy()))

    print("\n[efemérides → dale] — lo que suena es lo que se listó")
    ef = await q.efemerides_hoy()
    mbids = [r["mbid"] for r in ef if r.get("mbid")]
    if mbids:
        tr = await q.tracks_de_releases(mbids)
        print(f"  {len(mbids)} discos listados -> {len(tr)} tracks encolables")
        for t in tr[:5]:
            print(f"  · {t['artist']} — {t['title']}  ({t['album']})"
                  + ("  [listo]" if t.get("listo") else "  [hay que bajarlo]"))
    else:
        print("  no hay efemérides hoy con mbid cargado")

    print("\n[algo que haya escuchado hoy] — tracks listos para sonar")
    tr = await q.tracks_del_periodo(hoy)
    if tr:
        for t in tr[:5]:
            print(f"  · {t['artist']} — {t['title']}")
        print(f"  ({len(tr)} en total, todos ya resueltos en YouTube)")
    else:
        print("  nada resuelto en la ventana; probá con 'este mes'")


async def main() -> None:
    try:
        fallos = await _schema()
        fallos += await _ventanas()
        fallos += await _consultas()
        await _muestras()

        print()
        print("=" * 66)
        if fallos:
            print(f"FALLOS ({len(fallos)}):")
            for f in fallos:
                print(f"  · {f}")
        else:
            print("OK — las 9 consultas del H2 andan. Turnos que dejan de "
                  "costar ~19k tokens cada uno.")
        print("llamadas a la API de Anthropic: 0")
    finally:
        # El pool vive en ESTE loop: cerrarlo desde otro asyncio.run() tira
        # "Event loop is closed" aunque todo haya salido bien.
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
