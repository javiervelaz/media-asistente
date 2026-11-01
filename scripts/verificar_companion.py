"""A0 — compañero de escritorio. No toca mpv, ni Neon, ni gasta un token.

Cubre tres cosas:
  1. humor(): todas las reglas, incluida la ventana de sueño que cruza la
     medianoche (el borde donde es facil equivocarse).
  2. state.leer(): mpv caido, cola vacia, track con metadata del grafo y track
     sin mbid (cae a la miniatura de YouTube).
  3. el WebSocket de punta a punta: auth por header, auth por hello, key
     mala -> 4401, y que el estado llegue con la forma del protocolo.

Uso:  python -m scripts.verificar_companion
"""
import asyncio
import sys
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.companion import hub as hub_mod
from app.companion import state
from app.companion.mood import Contexto, en_ventana, humor
from app.companion.routes import router, set_controles
from app.config import settings

fallas: list[str] = []


def check(nombre: str, ok: bool, detalle: str = "") -> None:
    print(f"  {'ok ' if ok else 'MAL'}  {nombre}" + (f"  ({detalle})" if detalle and not ok else ""))
    if not ok:
        fallas.append(nombre)


# ------------------------------------------------------------------ 1. humor
print("humor")
NOCHE = datetime(2026, 11, 1, 3, 0)
TARDE = datetime(2026, 11, 1, 16, 0)


def ctx(**kw):
    base = dict(ahora=TARDE, mpv_ok=True, sonando=False, en_pausa=False)
    base.update(kw)
    return Contexto(**base)


check("mpv caido -> confundido", humor(ctx(mpv_ok=False)) == "confundido")
check("vinilo -> encantado", humor(ctx(sonando=True, de_vinilo=True)) == "encantado")
check("artista de la coleccion -> contento",
      humor(ctx(sonando=True, artista_coleccion=True)) == "contento")
check("cualquier otra cosa -> atento", humor(ctx(sonando=True)) == "atento")
check("pausa -> expectante", humor(ctx(en_pausa=True)) == "expectante")
check("musica de noche NO duerme", humor(ctx(ahora=NOCHE, sonando=True)) == "atento")
check("silencio de noche -> dormido", humor(ctx(ahora=NOCHE)) == "dormido")
check("silencio corto -> tranquilo",
      humor(ctx(silencio_desde=TARDE - timedelta(minutes=5))) == "tranquilo")
check("silencio largo -> aburrido",
      humor(ctx(silencio_desde=TARDE - timedelta(minutes=25))) == "aburrido")
check("ventana 23->7 incluye 2", en_ventana(2, 23, 7))
check("ventana 23->7 incluye 23", en_ventana(23, 23, 7))
check("ventana 23->7 excluye 7", not en_ventana(7, 23, 7))
check("ventana 23->7 excluye 15", not en_ventana(15, 23, 7))
check("ventana vacia nunca duerme", not en_ventana(3, 5, 5))


# ------------------------------------------------------------------ 2. state.leer
print("state.leer")

ESTADO = {"mpv": {}, "path": "", "track": None, "row": None, "db_falla": False}


async def fake_status():
    return dict(ESTADO["mpv"])


async def fake_fetchrow(_sql, _yid):
    if ESTADO["db_falla"]:
        raise ConnectionError("Neon dormido")
    return ESTADO["row"]


state.player.get_status = fake_status
state.player.track_en_curso = lambda: {"path": ESTADO["path"]}
state.history.get_track_at = lambda pos: ESTADO["track"]
state.fetchrow = fake_fetchrow


def run(coro):
    return asyncio.run(coro)


ESTADO["mpv"] = {"mpv_ok": False}
check("mpv caido -> offline", run(state.leer())["mode"] == "offline")

ESTADO["mpv"] = {"mpv_ok": True, "playlist_pos": -1, "playlist_count": 0}
check("cola vacia -> idle", run(state.leer())["mode"] == "idle")

ESTADO["mpv"] = {"mpv_ok": True, "playlist_pos": 0, "playlist_count": 3, "paused": False,
                 "position_sec": 95.0, "duration_sec": 271.0, "volume": 70, "title": "x"}
ESTADO["path"] = "/cache/abc123.opus"
ESTADO["track"] = {"artist": "The Rolling Stones", "title": "Gimme Shelter",
                   "youtube_id": "abc123", "rationale": "porque si"}
ESTADO["row"] = {"album": "Let It Bleed", "rg": "rg-uuid", "anio": 1969,
                 "artista": "The Rolling Stones", "titulo": "Gimme Shelter",
                 "de_vinilo": True, "artista_coleccion": True}
s = run(state.leer())
t = s["track"] or {}
check("now_playing con metadata", s["mode"] == "now_playing" and t.get("album") == "Let It Bleed")
check("tapa de Cover Art Archive", t.get("cover_source") == "caa" and "rg-uuid" in (t.get("cover") or ""))
check("vinilo marcado", t.get("from_vinyl") is True)
check("rationale viaja", t.get("rationale") == "porque si")

state._cache.clear()
ESTADO["row"] = None
ESTADO["path"] = "/cache/zzz999.opus"
ESTADO["track"] = {"artist": "Alguien", "title": "Algo", "youtube_id": "zzz999"}
t = run(state.leer())["track"]
check("sin mbid -> miniatura de YouTube", t["cover_source"] == "youtube" and "zzz999" in t["cover"])
check("sin mbid -> titulo de la playlist", t["title"] == "Algo" and t["artist"] == "Alguien")

state._cache.clear()
ESTADO["db_falla"] = True
ESTADO["track"] = {"artist": "A", "title": "B", "youtube_id": "q1"}
try:
    t = run(state.leer())["track"]
    check("Neon caido no rompe la pantalla", t["title"] == "B")
except Exception as e:
    check("Neon caido no rompe la pantalla", False, repr(e))
check("Neon caido no se cachea", "q1" not in state._cache)
ESTADO["db_falla"] = False


# ------------------------------------------------------------------ 3. websocket
print("websocket")
hub_mod.POLL_S = 0.05
app = FastAPI()
app.include_router(router)
KEY = settings.api_key

with TestClient(app) as client:
    with client.websocket_connect("/companion/ws", headers={"x-api-key": KEY}) as ws:
        m = ws.receive_json()
        check("auth por header", m.get("type") == "state" and m.get("v") == 1)
        claves = {"v", "type", "seq", "server_ts", "mode", "mood", "event", "track", "playback"}
        check("forma del mensaje", claves <= set(m), str(set(m)))

    with client.websocket_connect("/companion/ws") as ws:
        ws.send_json({"type": "hello", "key": KEY, "client": "preview"})
        m = ws.receive_json()
        check("auth por hello", m.get("type") == "state")

        # Cambia el tema -> tiene que llegar un state con event=track_changed
        ESTADO["track"] = {"artist": "Otro", "title": "Tema nuevo", "youtube_id": "nuevo1"}
        ESTADO["path"] = "/cache/nuevo1.opus"
        visto = False
        for _ in range(20):
            m = ws.receive_json()
            if (m.get("track") or {}).get("youtube_id") == "nuevo1":
                visto = m.get("event") == "track_changed"
                break
        check("cambio de tema -> event track_changed", visto)

    codigo = None
    try:
        with client.websocket_connect("/companion/ws") as ws:
            ws.send_json({"type": "hello", "key": "mala"})
            ws.receive_json()
    except WebSocketDisconnect as e:
        codigo = e.code
    check("key mala -> 4401", codigo == 4401, f"code={codigo}")

    # Controles: registrados como en main.py, con fakes que anotan la llamada.
    llamadas: list[str] = []

    async def fake_next():
        llamadas.append("next")

    async def fake_pause():
        raise RuntimeError("mpv no responde")

    set_controles({"next": fake_next, "pause": fake_pause})
    with client.websocket_connect("/companion/ws", headers={"x-api-key": KEY}) as ws:
        ws.receive_json()                      # estado inicial

        def ack_de(accion):
            ws.send_json({"type": "control", "action": accion})
            for _ in range(50):
                m = ws.receive_json()
                if m.get("type") == "ack":
                    return m
            return {}

        a = ack_de("next")
        check("control next -> corre el handler", llamadas == ["next"] and a.get("ok") is True)
        a = ack_de("pause")
        check("control que falla -> ack ok=false con error",
              a.get("ok") is False and "mpv" in (a.get("error") or ""))
        a = ack_de("volar")
        check("accion desconocida -> ack ok=false", a.get("ok") is False)
    try:
        set_controles({"autodestruccion": fake_next})
        check("set_controles rechaza acciones fuera del protocolo", False)
    except ValueError:
        check("set_controles rechaza acciones fuera del protocolo", True)

    r = client.get("/companion/preview")
    check("preview se sirve", r.status_code == 200 and "480" in r.text)

print()
if fallas:
    print(f"{len(fallas)} falla(s): {', '.join(fallas)}")
    sys.exit(1)
print("todo ok")
