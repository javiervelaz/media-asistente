"""Un ejecutor por intent. Ninguno de estos gasta un token.

Los intents de control reusan exactamente lo que ya hace /control/*: mismo
register_advance antes de saltar, mismo cancelado de fade. Si el harness
saltara ese paso, los skips que llegan por chat no se registrarian y la senal
que alimenta local_search quedaria a medias segun por donde entraste.

`main.py` inyecta los cancelables en el startup para no importar main desde
aca (ciclo de imports).
"""
import logging
from typing import Awaitable, Callable

from app import player
from app.harness import render
from app.harness.intents import Intent, Result
from app.harness.session import SessionState
from app.history import get_current, get_track_at, register_advance
from app.player import MPVError

logger = logging.getLogger(__name__)

DELTA_VOL = 10          # cuanto mueve un "subile" pelado

# --- hooks que vive en main.py ----------------------------------------------
_cancel_fade: Callable[[], None] = lambda: None
_cancel_resto: Callable[[], None] = lambda: None
_crear_playlist = None          # (prompt, room_id, n_tracks) -> dict


def set_hooks(cancel_fade=None, cancel_resto=None, crear_playlist=None) -> None:
    """Se llama una vez desde el lifespan de FastAPI."""
    global _cancel_fade, _cancel_resto, _crear_playlist
    if cancel_fade:
        _cancel_fade = cancel_fade
    if cancel_resto:
        _cancel_resto = cancel_resto
    if crear_playlist:
        _crear_playlist = crear_playlist


# --- control ----------------------------------------------------------------

async def _play(intent, st) -> Result:
    await player.resume()
    return Result(render.ok_control("play"), actions=["play"])


async def _pause(intent, st) -> Result:
    _cancel_fade()
    await player.pause()
    return Result(render.ok_control("pause"), actions=["pause"])


async def _next(intent, st) -> Result:
    _cancel_fade()
    await register_advance("next")      # antes de saltar, igual que /control/next
    await player.next_track()
    return Result(render.ok_control("next"), actions=["next"])


async def _prev(intent, st) -> Result:
    _cancel_fade()
    await register_advance("prev")      # volver atras no es skip
    await player.prev_track()
    return Result(render.ok_control("prev"), actions=["prev"])


async def _stop(intent, st) -> Result:
    _cancel_fade()
    _cancel_resto()
    await player.stop()
    return Result(render.ok_control("stop"), actions=["stop"])


async def _replay(intent, st) -> Result:
    _cancel_fade()
    await player.restart_track()
    return Result(render.ok_control("replay"), actions=["replay"])


async def _vol(intent: Intent, st, signo: int) -> Result:
    _cancel_fade()
    estado = await player.get_status()
    if not estado.get("mpv_ok"):
        raise MPVError("mpv no responde")
    actual = int(estado.get("volume") or 0)
    delta = int(intent.slots.get("delta") or DELTA_VOL)
    nuevo = max(0, min(100, actual + signo * delta))
    await player.set_volume(nuevo)
    return Result(render.volumen(nuevo), data={"level": nuevo},
                  actions=["volume"])


async def _vol_up(intent, st) -> Result:
    return await _vol(intent, st, +1)


async def _vol_down(intent, st) -> Result:
    return await _vol(intent, st, -1)


async def _vol_set(intent: Intent, st) -> Result:
    _cancel_fade()
    nivel = max(0, min(100, int(intent.slots.get("level", 50))))
    await player.set_volume(nivel)
    return Result(render.volumen(nivel), data={"level": nivel},
                  actions=["volume"])


# --- estado -----------------------------------------------------------------

async def _estado_actual(intent, st: SessionState) -> Result:
    estado = await player.get_status()
    t = get_track_at(estado.get("playlist_pos")) if estado.get("mpv_ok") else None
    if t:
        st.tocar(last_artist=t.get("artist"),
                 last_artist_mbid=t.get("artist_mbid"))
    return Result(render.estado_actual(estado, t),
                  ok=bool(estado.get("mpv_ok")),
                  data={"now_playing": f"{t['artist']} - {t['title']}" if t else None})


async def _estado_cola(intent, st) -> Result:
    estado = await player.get_status()
    cur = get_current()
    return Result(
        render.estado_cola(estado, cur.get("tracks") or [],
                           estado.get("playlist_pos")),
        ok=bool(estado.get("mpv_ok")))


async def _playlist(intent: Intent, st: SessionState) -> Result:
    """EL UNICO ejecutor que gasta tokens. Reusa /playlist entero: mismo
    ruteo local/hybrid/curador, mismo fade, mismo registro en play_history.

    El `usage` que vuelve se escribe en turn_log: es lo que convierte
    "me parece que gasta poco" en un numero por turno.
    """
    if _crear_playlist is None:
        return Result(render.error("el generador de playlists no esta enganchado"),
                      ok=False)
    prompt = (intent.slots.get("prompt") or "").strip()
    if not prompt:
        return Result(render.no_entendido(), ok=False)

    resp = await _crear_playlist(prompt, st.room_id)
    st.tocar(last_playlist_id=str(resp.get("playlist_id") or "") or None)
    return Result(render.playlist(resp), data=resp, actions=["playlist"])


async def _no_entendido(intent, st) -> Result:
    return Result(render.no_entendido(), ok=False)


EJECUTORES: dict[str, Callable[[Intent, SessionState], Awaitable[Result]]] = {
    "control_play": _play,
    "control_pause": _pause,
    "control_next": _next,
    "control_prev": _prev,
    "control_stop": _stop,
    "control_replay": _replay,
    "control_vol_up": _vol_up,
    "control_vol_down": _vol_down,
    "control_vol_set": _vol_set,
    "estado_actual": _estado_actual,
    "estado_cola": _estado_cola,
    "playlist": _playlist,
    "no_entendido": _no_entendido,
}


async def ejecutar(intent: Intent, st: SessionState) -> Result:
    """Nunca levanta: un reproductor que se cae porque mpv no contesta es peor
    que uno que dice que no pudo."""
    fn = EJECUTORES.get(intent.name)
    if fn is None:
        logger.warning("intent sin ejecutor: %s", intent.name)
        return Result(render.no_entendido(), ok=False)
    try:
        res = await fn(intent, st)
        st.tocar(last_intent=intent.name)
        return res
    except MPVError as e:
        logger.warning("mpv no respondio en %s: %s", intent.name, e)
        return Result(render.MPV_CAIDO, ok=False)
    except Exception:
        logger.exception("fallo el ejecutor de %s", intent.name)
        return Result(render.error("error interno"), ok=False)
