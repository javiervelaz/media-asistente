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
#: Cuenta mbids en los argumentos de un tool call. Es la columna que decide si
#: el batch de H7 se esta usando de verdad: con `release_mbids: [a,b,c]` el
#: modelo agrupa, con una lista de uno sigue preguntando de a un album y H7
#: no sirvio, aunque el codigo nuevo este desplegado.
MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
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


def fecha(linea: str) -> str | None:
    m = HORA.match(linea)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="~/logs/media-api.log")
    ap.add_argument("-n", type=int, default=20, help="ultimas N playlists")
    ap.add_argument("--desde", help="solo desde esta fecha, YYYY-MM-DD. "
                                    "Comparar 'antes' con 'despues' tiene que "
                                    "ser dos comandos, no dos interpretaciones.")
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
                actual = {"tools": [], "api": [], "t0": t, "lotes": []}
            actual["tools"].append(m.group(1))
            if m.group(1) == "get_recordings":
                actual.setdefault("lotes", []).append(len(MBID.findall(linea)))
            continue

        if API.search(linea):
            if actual is None:
                actual = {"tools": [], "api": [], "t0": t, "lotes": []}
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
                          libres=int(m.group("l")), t1=t,
                          dia=fecha(linea) or "?")
            playlists.append(actual)
            actual = None

    # Una playlist sin tokens es local: no paso por el curador.
    curadas = [p for p in playlists if p.get("out")]
    if not curadas:
        print("no hay playlists del curador en el log")
        return 1

    if args.desde:
        curadas = [p for p in curadas if p.get("dia", "") >= args.desde]
        if not curadas:
            print(f"no hay playlists del curador desde {args.desde}")
            return 1
    muestra = curadas[-args.n:]
    print(f"{len(curadas)} playlists del curador en el log · últimas {len(muestra)}")
    print("=" * 94)
    print(f"{'fecha':10} {'playlist':24} {'tk':>3} {'tools':>5} {'API':>4} "
          f"{'alb/ll':>6} {'out':>6} {'cache_w':>8} {'seg':>6} {'USD':>7}")
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
        lotes = p.get("lotes") or []
        # Promedio de albumes por llamada a get_recordings. "—" = no la uso.
        alb = f"{sum(lotes)/len(lotes):.1f}" if lotes else "—"
        print(f"{p.get('dia','?'):10} {p['titulo'][:24]:24} {p['tracks']:3} "
              f"{len(p['tools']):5} {len(api):4} {alb:>6} {p['out']:6} "
              f"{p['cache_w']:8} {seg:6.1f} {usd:7.4f}")
        for k, v in (("tools", len(p["tools"])), ("api", len(api)),
                     ("out", p["out"]), ("cache_r", p["cache_r"]),
                     ("cache_w", p["cache_w"]), ("usd", usd), ("seg", seg)):
            tot[k] += v

    n = len(muestra)
    print("-" * 94)
    lotes = [x for p in muestra for x in (p.get("lotes") or [])]
    alb = f"{sum(lotes)/len(lotes):.1f}" if lotes else "—"
    print(f"{'PROMEDIO':10} {'':24} {'':3} {tot['tools']/n:5.1f} "
          f"{tot['api']/n:4.1f} {alb:>6} {tot['out']/n:6.0f} "
          f"{tot['cache_w']/n:8.0f} {tot['seg']/n:6.1f} {tot['usd']/n:7.4f}")

    cw = tot["cache_w"] * P_CACHE_W / 1_000_000
    print(f"\nla escritura de caché es el {100*cw/tot['usd']:.0f}% del gasto")
    print(f"herramientas por vuelta a la API: {tot['tools']/max(tot['api'],1):.1f}")
    print(f"herramientas por vuelta a la API: {tot['tools']/max(tot['api'],1):.1f}")

    # El veredicto NO se saca del promedio de la muestra. La primera version
    # de esto promediaba 18 playlists de antes de H7 con 2 de despues, daba
    # alb/ll = 1.1 y concluia que el modelo no agrupaba — cuando las dos
    # nuevas agrupaban 4 y 6 albumes por llamada. Un promedio que cruza un
    # cambio de codigo no mide el cambio: lo diluye.
    def _alb(p):
        l = p.get("lotes") or []
        return sum(l) / len(l) if l else 0.0

    pre = [p for p in muestra if 0 < _alb(p) <= 1.0]
    post = [p for p in muestra if _alb(p) > 1.0]
    print()
    if pre and post:
        print("La muestra cruza H7. Los promedios de arriba no sirven para")
        print("comparar; estos sí:")
        print(f"{'':12}{'n':>3} {'tools':>6} {'API':>5} {'alb/ll':>7} "
              f"{'out':>6} {'seg':>7} {'USD':>7}")
        for etq, grupo in (("antes", pre), ("después", post)):
            k = len(grupo)
            api = lambda p: len([a for a in p["api"] if a is not None])
            print(f"{etq:12}{k:3} {sum(len(p['tools']) for p in grupo)/k:6.1f} "
                  f"{sum(api(p) for p in grupo)/k:5.1f} "
                  f"{sum(_alb(p) for p in grupo)/k:7.1f} "
                  f"{sum(p['out'] for p in grupo)/k:6.0f}")
        print("\nY con --desde se mide una sola época por corrida, que es como")
        print("hay que hacerlo.")
    elif not post:
        print("Ninguna playlist agrupó álbumes: o el log es de antes de H7, o")
        print("la Pi corre el código viejo.")
    else:
        print(f"Todas agrupan ({sum(_alb(p) for p in post)/len(post):.1f} "
              f"álbumes por llamada). H7 vivo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
