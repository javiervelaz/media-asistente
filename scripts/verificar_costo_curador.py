#!/usr/bin/env python3
"""Mide lo que cuesta cada playlist del curador, leyendo el log.

Es el "antes" de H7. El turn_log mide el TURNO entero; esto mide el agent
loop por dentro: cuantas vueltas a la API, cuantas herramientas y cuanto
output. Sin esta separacion no se puede saber si un arreglo sirvio, porque
los dos sintomas —latencia y costo— salen del mismo lugar.

    .venv/bin/python scripts/verificar_costo_curador.py
    .venv/bin/python scripts/verificar_costo_curador.py --log ~/logs/otro.log -n 40

El log se lee en binario a proposito: tiene al menos un byte NUL (dos
procesos escribiendo al mismo `append:` sin bloqueo) y abrirlo como texto
revienta con UnicodeDecodeError o corta a la mitad.
"""
import argparse
import re
import sys
from pathlib import Path

HORA = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d):(\d\d):(\d\d),(\d{3})")
TOOL = re.compile(r"app\.curator: tool (\w+)")
API = re.compile(r"api\.anthropic\.com/v1/messages")
TOKENS = re.compile(r"app\.curator: tokens — in:(\d+) out:(\d+) "
                    r"cache_r:(\d+) cache_w:(\d+)")
LISTA = re.compile(r"app\.curator: playlist '(?P<t>.*?)': (?P<n>\d+) tracks "
                   r"\((?P<v>\d+) verificados, (?P<l>\d+) libres\)")

# Precios Sonnet, USD por millon de tokens. Si cambian, se cambian aca.
P_IN, P_CACHE_R, P_CACHE_W, P_OUT = 3.00, 0.30, 3.75, 15.00


def segundos(linea: str) -> float | None:
    m = HORA.match(linea)
    if not m:
        return None
    _, h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="~/logs/media-api.log")
    ap.add_argument("-n", type=int, default=20, help="ultimas N playlists")
    args = ap.parse_args()

    ruta = Path(args.log).expanduser()
    if not ruta.exists():
        print(f"no existe {ruta}")
        return 1

    # errors="replace": el NUL y cualquier basura no tienen que frenar la
    # medicion. Un medidor que se cae por un byte raro no se usa.
    lineas = ruta.read_bytes().decode("utf-8", errors="replace").splitlines()

    playlists, actual = [], None
    for linea in lineas:
        t = segundos(linea)

        if m := TOOL.search(linea):
            if actual is None:
                actual = {"tools": [], "api": [], "t0": t}
            actual["tools"].append(m.group(1))
            continue

        if API.search(linea):
            if actual is None:
                actual = {"tools": [], "api": [], "t0": t}
            actual["api"].append(t)
            continue

        if m := TOKENS.search(linea):
            if actual is not None:
                actual.update(zip(("in", "out", "cache_r", "cache_w"),
                                  map(int, m.groups())))
            continue

        if m := LISTA.search(linea):
            if actual is None:
                continue
            actual.update(titulo=m.group("t"), tracks=int(m.group("n")),
                          verificados=int(m.group("v")),
                          libres=int(m.group("l")), t1=t)
            playlists.append(actual)
            actual = None

    # Una playlist sin tokens es local: no paso por el curador.
    curadas = [p for p in playlists if p.get("out")]
    if not curadas:
        print("no hay playlists del curador en el log")
        return 1

    muestra = curadas[-args.n:]
    print(f"{len(curadas)} playlists del curador en el log · últimas {len(muestra)}")
    print("=" * 94)
    print(f"{'playlist':28} {'tk':>3} {'tools':>5} {'API':>4} "
          f"{'out':>6} {'cache_r':>8} {'cache_w':>8} {'seg':>6} {'USD':>7}")
    print("-" * 94)

    tot = {"tools": 0, "api": 0, "out": 0, "cache_r": 0, "cache_w": 0,
           "usd": 0.0, "seg": 0.0}
    for p in muestra:
        usd = (p.get("in", 0) * P_IN + p["cache_r"] * P_CACHE_R
               + p["cache_w"] * P_CACHE_W + p["out"] * P_OUT) / 1_000_000
        # Las vueltas a la API del mismo pedido: las que caen dentro de la
        # ventana de la playlist. Un salto de horas es otra sesion.
        api = [a for a in p["api"] if a is not None]
        gaps = [b - a for a, b in zip(api, api[1:]) if 0 < b - a < 600]
        seg = sum(gaps) + (p["t1"] - api[-1] if api and p.get("t1") else 0)
        print(f"{p['titulo'][:28]:28} {p['tracks']:3} {len(p['tools']):5} "
              f"{len(api):4} {p['out']:6} {p['cache_r']:8} {p['cache_w']:8} "
              f"{seg:6.1f} {usd:7.4f}")
        for k, v in (("tools", len(p["tools"])), ("api", len(api)),
                     ("out", p["out"]), ("cache_r", p["cache_r"]),
                     ("cache_w", p["cache_w"]), ("usd", usd), ("seg", seg)):
            tot[k] += v

    n = len(muestra)
    print("-" * 94)
    print(f"{'PROMEDIO':28} {'':3} {tot['tools']/n:5.1f} {tot['api']/n:4.1f} "
          f"{tot['out']/n:6.0f} {tot['cache_r']/n:8.0f} {tot['cache_w']/n:8.0f} "
          f"{tot['seg']/n:6.1f} {tot['usd']/n:7.4f}")

    cw = tot["cache_w"] * P_CACHE_W / 1_000_000
    print(f"\nla escritura de caché es el {100*cw/tot['usd']:.0f}% del gasto")
    print(f"herramientas por vuelta a la API: {tot['tools']/max(tot['api'],1):.1f}")
    print("\nH7 busca bajar `tools`, `API` y `out`. Los tres se multiplican:")
    print("menos vueltas es contexto más chico, y contexto más chico acelera")
    print("todas las llamadas y baja la escritura de caché.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
