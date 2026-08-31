"""Smoke end-to-end del harness con mpv y Neon mockeados.

Uso:  python -m scripts.verificar_harness_smoke

No toca la Pi, no toca Neon, no llama a la API. Verifica que un turno entra
por chat.responder y sale renderizado, y que el logueo no rompe el turno
cuando la base no esta.
"""
import asyncio, sys, types

# --- stubs solo si faltan (correr fuera del venv de la Pi) ------------------
for mod, attrs in [
    ("asyncpg", {"Pool": object, "create_pool": None}),
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

# generador de playlists falso
async def fake_playlist(prompt, room_id="main"):
    LLAMADAS.append(("playlist", prompt))
    return {"title": "Post-punk 1980", "queued": 12,
            "first_track": {"artist": "Wire", "title": "Reuters"},
            "playlist_id": "p-1",
            "usage": {"in": 18400, "out": 900, "cache_read": 15200}}
executors.set_hooks(crear_playlist=fake_playlist)


async def main():
    frases = ["hola charly", "pasala", "che charly bajale 20", "qué suena",
              "qué sigue", "poné el volumen en 40", "de nuevo", "gracias",
              "ayuda",
              "armá una playlist de post-punk británico de 1980",
              "qué escuché la semana pasada"]
    print("=" * 66)
    for f in frases:
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
    assert gratis == 9, gratis
    assert ("register_advance", "next") in LLAMADAS, "el skip por chat no se registro"
    assert ESTADO["volume"] == 40
    print("\nOK — el skip por chat registra feedback igual que /control/next")

if __name__ == "__main__":
    asyncio.run(main())
