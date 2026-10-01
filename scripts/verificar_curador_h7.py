#!/usr/bin/env python3
"""H7 — el batch de tracklists y la salida por mbid, sin gastar un token.

Prueba las tres cosas que H7 cambia y que fallarian en silencio:

1. `_truncar` con la clave `albums`. Con el tope viejo, seis albumes caian en
   la rama del `preview` y el modelo perdia todos los tracks.
2. `_parse` con la salida nueva (solo `recording_mbid`). El filtro viejo
   exigia `artist` y `title` siempre: habria descartado la playlist entera.
3. La salida vieja y la mixta siguen andando. El modelo no cambia de formato
   el dia que deployamos, y una playlist con un track de memoria tiene que
   seguir entrando.

    PYTHONPATH=. DATABASE_URL=postgresql://x:x@localhost/x python3 scripts/verificar_curador_h7.py
"""
import json
import sys

from app import curator as c

MB = "11111111-2222-3333-4444-{:012d}"


def _album(i: int, n: int = 12) -> dict:
    return {
        "artist": f"Artista {i}", "artist_mbid": MB.format(900 + i),
        "release_mbid": MB.format(800 + i), "release": f"Album numero {i}",
        "tracks": [{"mbid": MB.format(i * 100 + t),
                    "title": f"Un titulo razonablemente largo {t}",
                    "length_ms": 240000, "listo": t % 2 == 0}
                   for t in range(n)],
    }


class _Resp:
    """Lo minimo que `_parse` necesita de una respuesta de la API."""
    def __init__(self, texto):
        self.content = [type("B", (), {"type": "text", "text": texto})()]
        self.stop_reason = "end_turn"


def main() -> int:
    fallos = []

    def check(cond, msg):
        print(("ok  " if cond else "MAL ") + msg)
        if not cond:
            fallos.append(msg)

    print("=" * 72)
    print("1 · truncado del batch")
    out = {"albums": [_album(i) for i in range(6)]}
    crudo = len(json.dumps(out))
    r = json.loads(c._truncar(out, c.MAX_TOOL_RESULT_BATCH))
    check(len(r.get("albums", [])) == 6 and "preview" not in r,
          f"6 albumes ({crudo} chars) entran enteros con el tope del batch")
    r4 = json.loads(c._truncar(out, c.MAX_TOOL_RESULT))
    check("preview" not in r4 and r4.get("truncado"),
          "con el tope comun recorta albumes y avisa, no devuelve un preview")

    print("\n2 · vistos desde el batch")
    vistos = {}
    c._registrar_vistos("get_recordings", {}, out, vistos)
    check(len(vistos) == 72, f"72 tracks registrados (hay {len(vistos)})")
    # La forma vieja, un album suelto, tiene que seguir entrando: el deploy
    # del codigo y el formato del modelo no son atomicos.
    v2 = {}
    c._registrar_vistos("get_recordings", {}, _album(0), v2)
    check(len(v2) == 12, f"un album suelto sigue andando (hay {len(v2)})")

    print("\n3 · _parse con la salida nueva")
    # Un track por album: los seis son de artistas distintos. Con diez del
    # mismo album, `MAX_POR_ARTISTA = 2` recorta a dos — la guarda de densidad
    # funcionando, no un bug. Mi primer fixture cayo justo ahi.
    mbids = [MB.format(i * 100) for i in range(6)]
    nueva = json.dumps({
        "title": "Prueba", "concept": "x", "narration": "y",
        "tracks": [{"recording_mbid": m, "rationale": "porque"} for m in mbids],
    })
    d = c._parse(_Resp(nueva), vistos, n_tracks=10)
    check(len(d["tracks"]) == 6, f"6 tracks aceptados (hay {len(d['tracks'])})")
    check(all(t.get("artist") and t.get("title") for t in d["tracks"]),
          "el servidor rellenó artista y título desde la base")
    check(d["metrics"]["verificados"] == 6 and d["metrics"]["libres"] == 0,
          "los 6 quedan como verificados")
    # El ahorro, medido sobre el mismo contenido
    vieja = json.dumps({
        "title": "Prueba", "concept": "x", "narration": "y",
        "tracks": [{"artist": vistos[m]["artist"], "title": vistos[m]["title"],
                    "recording_mbid": m, "length_ms": 240000,
                    "rationale": "porque"} for m in mbids],
    })
    print(f"     salida vieja {len(vieja)} chars · nueva {len(nueva)} chars "
          f"· {100 - 100*len(nueva)//len(vieja)}% menos")

    print("\n4 · compatibilidad y tracks libres")
    d = c._parse(_Resp(vieja), vistos, n_tracks=10)
    check(len(d["tracks"]) == 6, "la salida vieja sigue entrando entera")

    mixta = json.dumps({
        "title": "Prueba", "concept": "x", "narration": "y",
        "tracks": [{"recording_mbid": mbids[0], "rationale": "a"},
                   {"artist": "Alguien", "title": "De memoria", "rationale": "b"},
                   {"rationale": "sin nada"}],
    })
    d = c._parse(_Resp(mixta), vistos, n_tracks=10)
    check(len(d["tracks"]) == 2,
          f"el track sin mbid y sin artista se descarta (quedaron {len(d['tracks'])})")
    check(d["metrics"]["libres"] == 1, "el de memoria queda marcado como libre")
    libre = [t for t in d["tracks"] if t["origen"] == "libre"]
    check(bool(libre) and libre[0]["artist"] == "Alguien",
          "el libre conserva artista y título, que es con lo que se busca")

    print("=" * 72)
    if fallos:
        print(f"{len(fallos)} FALLOS")
        return 1
    print("OK — el batch entra entero, la salida por mbid se rellena desde la")
    print("base, y la forma vieja y la mixta siguen andando.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
