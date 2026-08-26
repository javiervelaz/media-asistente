#!/usr/bin/env python3
"""Verifica el bloque C: la barrera contra el track inventado.

  1. get_recordings devuelve artista y disponibilidad (lo que hace verificable
     un recording_mbid).
  2. El curador real arma una playlist y se mide cuanto verifico.

El punto 2 gasta tokens: pasale --sin-curador para saltearlo.

    python scripts/verificar_bloque_c.py [--sin-curador] [-p "prompt"]
"""
import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from app.config import settings
from app.db import close_pool, fetchrow
from app.tools import get_recordings, query_releases

VERDE, AMAR, ROJO, GRIS, FIN = ("\033[32m", "\033[33m", "\033[31m",
                                "\033[90m", "\033[0m")


def ok(c: bool) -> str:
    return f"{VERDE}OK{FIN}" if c else f"{ROJO}FALLA{FIN}"


async def test_tool() -> bool:
    print("\n=== 1. get_recordings devuelve lo necesario para verificar ===")
    rel = await fetchrow("""
        SELECT r.mbid FROM releases r
        WHERE EXISTS (SELECT 1 FROM recordings rc WHERE rc.release_mbid = r.mbid)
        LIMIT 1
    """)
    if not rel:
        print(f"  {ROJO}no hay releases con tracklist cargada{FIN}")
        return False

    out = await get_recordings(str(rel["mbid"]))
    es_dict = isinstance(out, dict)
    tiene = es_dict and out.get("artist") and isinstance(out.get("tracks"), list)
    print(f"  forma: {'dict con artist + tracks' if tiene else type(out).__name__} {ok(bool(tiene))}")
    if not tiene:
        return False

    print(f"  artista: {out['artist']} — {out['release']}")
    tracks = out["tracks"]
    con_mbid = sum(1 for t in tracks if t.get("mbid"))
    listos = sum(1 for t in tracks if t.get("listo"))
    print(f"  tracks: {len(tracks)} | con mbid: {con_mbid} | ya resueltos: {listos}")
    print(f"  {GRIS}`listo` le permite al curador preferir lo que arranca al "
          f"instante{FIN}")
    return con_mbid == len(tracks)


async def test_curador(prompt: str) -> bool:
    from app.curator import curate

    print(f"\n=== 2. Curador real: {prompt!r} ===")
    print(f"  {GRIS}cuota de libres configurada: "
          f"{settings.curator_max_libres}{FIN}")
    try:
        data = await curate(prompt, n_tracks=12)
    except Exception as e:
        print(f"  {ROJO}el curador falló: {e}{FIN}")
        return False

    m = data.get("metrics") or {}
    tracks = data.get("tracks") or []
    total = len(tracks)
    verif = m.get("verificados", 0)
    ratio = (verif / total * 100) if total else 0

    print(f"\n  {data.get('title')}")
    print(f"  {GRIS}{data.get('concept', '')}{FIN}\n")
    for t in tracks:
        marca = (f"{VERDE}✓{FIN}" if t.get("origen") == "verificado"
                 else f"{AMAR}?{FIN}")
        print(f"   {marca} {t['artist']} — {t['title']}")

    print(f"\n  recordings vistos en tools: {m.get('vistos_en_tools', 0)}")
    print(f"  verificados: {verif}/{total} ({ratio:.0f}%)")
    print(f"  libres:      {m.get('libres', 0)}")
    print(f"  fuera por cuota: {m.get('descartados_por_cuota', 0)}")

    if m.get("vistos_en_tools", 0) == 0:
        print(f"  {ROJO}el modelo no llamó a get_recordings: no hubo "
              f"verificación posible{FIN}")
        return False
    if ratio < 50:
        print(f"  {AMAR}menos de la mitad verificado: revisá si la base tiene "
              f"tracklists cargadas para este pedido{FIN}")
    return True


async def main(args) -> int:
    try:
        r = [await test_tool()]
        if not args.sin_curador:
            r.append(await test_curador(args.prompt))
        else:
            print(f"\n{GRIS}(curador salteado){FIN}")
    finally:
        await close_pool()

    print()
    if all(r):
        print(f"{VERDE}Bloque C verificado.{FIN}")
        return 0
    print(f"{ROJO}Hay fallos.{FIN}")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sin-curador", action="store_true",
                    help="no llama al LLM (no gasta tokens)")
    ap.add_argument("-p", "--prompt", default="post-punk británico de 1980")
    raise SystemExit(asyncio.run(main(ap.parse_args())))
