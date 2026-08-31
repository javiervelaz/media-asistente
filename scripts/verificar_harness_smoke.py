"""Smoke end-to-end del harness con mpv y Neon mockeados.

Uso:  python -m scripts.verificar_harness_smoke

No toca la Pi, no toca Neon, no llama a la API. Verifica que un turno entra
por chat.responder y sale renderizado, y que el logueo no rompe el turno
cuando la base no esta.
"""
import asyncio, sys, types

# --- stubs solo si faltan (correr fuera del venv de la Pi) ------------------
for mod, attrs in [
    ("asyncpg", {"Pool": object, "create_pool": None,
                 "Connection": object}),
    ("anthropic", {"AsyncAnthropic": object, "Anthropic": object}),
]:
    try:
        __import__(mod)
    except ImportError:
        m = types.ModuleType(mod)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[mod] = m

from app.harness import chat, executors, render, session
from app.harness.router import rutear
from app import player
from app.harness import telemetry

# mpv falso
ESTADO = {"mpv_ok": True, "paused": False, "title": "x", "volume": 50,
          "position_sec": 71, "duration_sec": 245, "playlist_count": 4,
          "playlist_pos": 1}
LLAMADAS = []

async def fake_status(): return dict(ESTADO)
async def fake(*a, **k): LLAMADAS.append(("mpv", a)); return None
async def fake_vol(level): LLAMADAS.append(("volume", level)); ESTADO["volume"] = level

player.get_status = fake_status
player.resume = player.pause = player.next_track = player.prev_track = fake
player.stop = player.restart_track = fake
player.set_volume = fake_vol
SALIDA = {"ok": True}
player.salida_activa = lambda: SALIDA["ok"]

# historial falso
from app import history
TRACKS = [{"artist": "Killing Joke", "title": "Requiem"},
          {"artist": "Wire", "title": "Reuters", "rationale": "post-punk 1977"},
          {"artist": "Magazine", "title": "Shot by Both Sides"},
          {"artist": "Gang of Four", "title": "Damaged Goods"}]
history._current = {"playlist_id": "abc", "tracks": TRACKS}
history.get_current = lambda: history._current
history.get_track_at = lambda p: TRACKS[p] if p is not None and p < len(TRACKS) else None
executors.get_current = history.get_current
executors.get_track_at = history.get_track_at
async def no_advance(m="next"): LLAMADAS.append(("register_advance", m))
executors.register_advance = no_advance

# telemetria: no hay base, guardamos lo que se hubiera escrito
ESCRITO = []
telemetry.log_turn = lambda **kw: ESCRITO.append(kw)
chat.log_turn = telemetry.log_turn

# --- H2: la base falsa -----------------------------------------------------
from app.harness import queries, executors as _ex, chat as _chat
from datetime import datetime, timedelta, timezone

_HOY = [
    {"artist": "Wire", "title": "Reuters", "started_at": datetime.now(timezone.utc),
     "completed": True, "skipped": False},
    {"artist": "Magazine", "title": "Shot by Both Sides",
     "started_at": datetime.now(timezone.utc), "completed": True, "skipped": False},
    {"artist": "Gang of Four", "title": "Damaged Goods",
     "started_at": datetime.now(timezone.utc), "completed": False, "skipped": True},
]

async def _q_historial(v, limite=40): return list(_HOY)
async def _q_top(dias=30): return [
    {"artist": "Killing Joke", "completos": 12, "veces": 15, "temas": 8},
    {"artist": "Wire", "completos": 7, "veces": 7, "temas": 5}]
async def _q_salteados(dias=90): return [
    {"artist": "Los Mirlos", "skips": 4, "temas": 3}]
async def _q_nunca(limite=15): return [
    {"mbid": "22222222-0000-0000-0000-000000000000", "artist": "Sumo",
     "album": "Divididos por la Felicidad", "anio": "1985"},
    {"mbid": "33333333-0000-0000-0000-000000000000", "artist": "Virus",
     "album": "Locura", "anio": "1985"}]
async def _q_efem(limite=8): return [
    {"mbid": "11111111-0000-0000-0000-000000000000", "artist": "Joy Division",
     "album": "Closer", "anio": "1980", "aniversario": 46, "weight": 1}]
async def _q_artista(nombre):
    return {"mbid": "aaaaaaaa-0000-0000-0000-000000000000", "name": "Wire",
            "sim": 0.9} if "wire" in nombre.lower() else None
async def _q_disco(mbid, limite=20): return [
    {"title": "Pink Flag", "primary_type": "Album", "anio": 1977,
     "tracks": 21, "en_vinilo": True}]
async def _q_rel(mbid, limite=15): return [
    {"name": "Colin Newman", "country": "GB", "rel_type": "member_of_band",
     "releases": 9}]
async def _q_hist_art(mbid, nombre, dias=90): return [
    {"title": "Reuters", "veces": 4, "completos": 3, "skips": 1,
     "ultima": datetime.now(timezone.utc)}]
async def _q_costo(): return 19300
async def _q_de_releases(mbids, limite=14, por_album=3):
    LLAMADAS.append(("tracks_de_releases", tuple(mbids)))
    return [{"recording_mbid": "d1", "artist": "Joy Division",
             "title": "Isolation", "album": "Closer", "listo": True},
            {"recording_mbid": "d2", "artist": "Joy Division",
             "title": "Passover", "album": "Closer", "listo": True}]
async def _q_tracks(v, limite=14): return [
    {"recording_mbid": "bbbb", "artist": "Wire", "title": "Reuters",
     "youtube_id": "x1"},
    {"recording_mbid": "cccc", "artist": "Magazine",
     "title": "Shot by Both Sides", "youtube_id": "x2"}]

# --- H4: objetivos falsos --------------------------------------------------
from app.harness import goals as _goals

_GOALS = []

async def _g_activos(room_id="main"): return list(_GOALS)
async def _g_declarar(kind, spec, window_days=30, room_id="main"):
    global _GOALS
    _GOALS = [g for g in _GOALS if g["kind"] != kind]
    g = {"id": len(_GOALS) + 1, "kind": kind, "spec": spec,
         "window_days": window_days}
    _GOALS.append(g)
    return g
async def _g_borrar(kind, room_id="main"):
    global _GOALS
    n = len([g for g in _GOALS if g["kind"] == kind])
    _GOALS = [g for g in _GOALS if g["kind"] != kind]
    return n
async def _g_progreso(goal):
    kind, spec = goal["kind"], goal.get("spec") or {}
    tabla = {"coleccion": (22.0, float(spec.get("target", .4)) * 100, "%", 140),
             "descubrimiento": (2.0, float(spec.get("target", 5)), "artistas", 140),
             "genero": (6.0, float(spec.get("target", 20)), "temas", 140),
             "profundidad": (30.0, float(spec.get("target", .5)) * 100, "%", 140)}
    actual, target, unidad, muestra = tabla[kind]
    return {"kind": kind, "spec": spec, "dias": goal.get("window_days", 30),
            "actual": actual, "target": target, "muestra": muestra,
            "unidad": unidad, "suficiente": muestra >= 20,
            "cumplido": actual >= target, "falta": max(0.0, target - actual)}
_goals.activos, _goals.declarar, _goals.borrar = _g_activos, _g_declarar, _g_borrar
_goals.progreso = _g_progreso
_ex.goals = _goals

async def _q_obj(kind, spec=None, limite=14):
    LLAMADAS.append(("tracks_para_objetivo", kind))
    return [{"recording_mbid": "o1", "artist": "Sumo", "title": "La rubia tarada",
             "album": "Divididos por la Felicidad", "listo": True},
            {"recording_mbid": "o2", "artist": "Sumo", "title": "Mejor no hablar",
             "album": "Divididos por la Felicidad", "listo": True}]

for nombre, fn in [("historial_periodo", _q_historial), ("top_escuchados", _q_top),
                   ("salteados", _q_salteados), ("nunca_escuchado", _q_nunca),
                   ("efemerides_hoy", _q_efem), ("resolver_artista", _q_artista),
                   ("discografia", _q_disco), ("relaciones", _q_rel),
                   ("historial_artista", _q_hist_art),
                   ("tracks_del_periodo", _q_tracks),
                   ("tracks_de_releases", _q_de_releases),
                   ("costo_tipico", _q_costo),
                   ("tracks_para_objetivo", _q_obj)]:
    setattr(queries, nombre, fn)
_ex.queries = queries
_chat.queries = queries

async def fake_lanzar(tracks_, titulo, room_id="main"):
    LLAMADAS.append(("lanzar", titulo, len(tracks_)))
    return {"playlist_id": "p-2", "title": titulo, "queued": len(tracks_),
            "first_track": tracks_[0]}

# generador de playlists falso
async def fake_playlist(prompt, room_id="main"):
    LLAMADAS.append(("playlist", prompt))
    return {"title": "Post-punk 1980", "queued": 12,
            "first_track": {"artist": "Wire", "title": "Reuters"},
            "playlist_id": "p-1",
            "usage": {"in": 18400, "out": 900, "cache_read": 15200}}
executors.set_hooks(crear_playlist=fake_playlist, lanzar_tracks=fake_lanzar)


async def main():
    frases = ["hola charly", "pasala", "che charly bajale 20", "qué suena",
              "qué sigue", "poné el volumen en 40", "de nuevo", "gracias",
              # --- H2 ---
              "qué escuché hoy", "qué escucho más", "qué me salteo",
              "qué tengo en vinilo sin escuchar", "efemérides",
              "qué discos tengo de Wire", "quién tocó con Wire",
              "qué escuché de Wire", "qué discos tengo de Pescado Rabioso",
              "poneme algo que haya escuchado hoy",
              # la oferta y su confirmacion: dos turnos, cero tokens
              "efemérides", "dale",
              "qué tengo en vinilo sin escuchar", "no",
              # --- lo unico que gasta ---
              # infinitivos: "parar" no matcheaba y caia al fallback
              "parar", "seguir",
              # el freno de gasto: lo no entendido avisa antes de pagar
              "ese temita que sonaba en el bar", "no",
              # y ahora aceptando: el gasto queda atribuido al turno del "dale"
              "algo raro que no entiendo", "dale",
              # el bot tiene que avisar que suena contra la nada
              "__sin_salida__", "qué suena",
              # --- H4 ---
              "cómo voy",
              "quiero escuchar más de mi colección",
              "cómo voy",
              "dale",
              "borrame el objetivo de vinilo",
              # un pedido explicito no pide permiso
              "armá una playlist de post-punk británico de 1980"]
    print("=" * 66)
    for f in frases:
        if f == "__sin_salida__":
            SALIDA["ok"] = False       # simula el sink fantasma
            continue
        r = await chat.responder(f, session_id="tg:937324746")
        marca = "GRATIS" if r["free"] else f"${r['cost_tokens']} tok"
        print(f"\n> {f}")
        print(f"  [{r['intent']} · {r['stage']} · {marca} · {r['latency_ms']}ms]")
        for linea in r["reply"].splitlines():
            print(f"  {linea}")

    print("\n" + "=" * 66)
    gratis = sum(1 for e in ESCRITO if e["model"] is None)
    print(f"turnos logueados: {len(ESCRITO)}  ·  gratis: {gratis}/{len(ESCRITO)}")
    caros = [e for e in ESCRITO if e["model"]]
    for e in caros:
        print(f"  gasto: {e['intent']} (stage={e['stage']}) "
              f"in={e['input_tokens']} cache={e['cached_tokens']} out={e['output_tokens']}")
    assert gratis == len(ESCRITO) - 2, (gratis, len(ESCRITO))
    frenados = [e for e in ESCRITO if e["intent"] == "confirmar_gasto"]
    assert len(frenados) == 2, frenados
    assert all(f["model"] is None for f in frenados), "el freno no puede costar"
    # el "dale" que acepta ejecuta la playlist y ESE turno es el que paga
    conf = [e for e in ESCRITO if e["intent"] == "confirmar"]
    assert len(conf) == 2, conf
    pagos = [e for e in conf if e["model"]]
    assert len(pagos) == 1, f"el dale sobre un freno tiene que quedar como pago: {conf}"
    # el objetivo atrasado se reproduce por SQL, sin curador
    assert ("tracks_para_objetivo", "coleccion") in LLAMADAS, \
        "el dale sobre un objetivo tiene que armar la cola por SQL"
    avisos = [e for e in ESCRITO if e["intent"] == "estado_actual"]
    assert len(avisos) >= 2, avisos
    obj = [e for e in ESCRITO if e["intent"] == "reproducir_objetivo"]
    assert obj and obj[0]["model"] is None, "reproducir_objetivo no puede gastar"
    assert ("lanzar", "De nuevo: hoy", 2) in LLAMADAS, "reproducir_historial no lanzo"
    # lo que suena tiene que ser EXACTAMENTE el release listado
    assert ("tracks_de_releases", ("11111111-0000-0000-0000-000000000000",)) in LLAMADAS, \
        "el 'dale' no reprodujo los releases que se listaron"
    assert ("lanzar", "Las efemérides de hoy", 2) in LLAMADAS
    assert ("register_advance", "next") in LLAMADAS, "el skip por chat no se registro"
    assert ESTADO["volume"] == 40
    print("\nOK — el skip por chat registra feedback igual que /control/next")

if __name__ == "__main__":
    asyncio.run(main())
