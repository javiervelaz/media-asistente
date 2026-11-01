"""Endpoints del compañero.

  GET  /companion/state     snapshot puntual (debug, curl). Pide api key.
  WS   /companion/ws        stream de view states. Auth abajo.
  GET  /companion/preview   simulador de la pantalla 480x480. Sin secretos.

Controles: el cliente manda `{"type": "control", "action": "next"}` por el
mismo WebSocket y recibe `{"type": "ack", ...}`. Las acciones las registra
main.py con `set_controles` (mismo patron que `harness_exec.set_hooks`): asi
pausar desde la pantalla corre EXACTAMENTE el mismo codigo que /control/* —
fade cancelado, register_advance incluido— sin que este modulo importe main.

Auth del WebSocket: un navegador no puede mandar headers propios en un
WebSocket, y meter la key en la URL la deja en el access log de uvicorn. Por
eso hay dos caminos:
  - header `x-api-key` (la placa ESP32 lo puede mandar), o
  - primer mensaje `{"type": "hello", "key": "..."}` dentro de 5 s.
"""
import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from app.auth import verify_api_key
from app.companion.hub import hub
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/companion", tags=["companion"])

PREVIEW = Path(__file__).parent / "static" / "preview.html"
HELLO_TIMEOUT_S = 5.0

#: accion -> coroutine sin argumentos. Lo llena main.py en el lifespan.
_controles: dict[str, Callable[[], Awaitable]] = {}
ACCIONES = ("toggle", "play", "pause", "next", "prev", "stop",
            "volume_up", "volume_down")


def set_controles(mapa: dict[str, Callable[[], Awaitable]]) -> None:
    desconocidas = set(mapa) - set(ACCIONES)
    if desconocidas:
        raise ValueError(f"acciones fuera del protocolo: {desconocidas}")
    _controles.clear()
    _controles.update(mapa)


async def _ejecutar(msg: dict) -> dict:
    accion = str(msg.get("action") or "")
    fn = _controles.get(accion)
    if fn is None:
        return {"type": "ack", "action": accion, "ok": False,
                "error": "accion desconocida"}
    try:
        await fn()
    except Exception as e:   # HTTPException(503) de mpv incluida
        detalle = getattr(e, "detail", None) or str(e) or e.__class__.__name__
        logger.warning("companion: control %s fallo: %s", accion, detalle)
        return {"type": "ack", "action": accion, "ok": False, "error": str(detalle)}
    # mpv tarda un instante en reflejar el cambio; despues, la pantalla se
    # entera ya y no al proximo segundo.
    await asyncio.sleep(0.15)
    hub.refrescar()
    return {"type": "ack", "action": accion, "ok": True}


def _key_ok(key: str | None) -> bool:
    return bool(key) and hmac.compare_digest(str(key), settings.api_key)


@router.get("/state", dependencies=[Depends(verify_api_key)])
async def companion_state():
    return await hub.snapshot()


@router.get("/preview", response_class=HTMLResponse)
async def companion_preview():
    return HTMLResponse(PREVIEW.read_text(encoding="utf-8"))


@router.websocket("/ws")
async def companion_ws(ws: WebSocket):
    await ws.accept()
    cliente = "device"
    if not _key_ok(ws.headers.get("x-api-key")):
        try:
            hola = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT_S)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            await ws.close(code=4401)
            return
        if not (isinstance(hola, dict) and hola.get("type") == "hello"
                and _key_ok(hola.get("key"))):
            await ws.close(code=4401)
            return
        cliente = str(hola.get("client") or "preview")[:20]

    logger.info("companion: se conecto %s", cliente)
    try:
        await hub.conectar(ws)
        # Del cliente llegan controles (touch de la placa, botones del
        # simulador). A3 va a sumar aca el audio/texto del pedido. Leer hace
        # falta igual: es como nos enteramos de que se desconecto.
        while True:
            crudo = await ws.receive_text()
            try:
                msg = json.loads(crudo)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "control":
                await hub.enviar(ws, await _ejecutar(msg))
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("companion: error con %s", cliente)
    finally:
        hub.desconectar(ws)
        logger.info("companion: se fue %s", cliente)
