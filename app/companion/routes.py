"""Endpoints del compañero.

  GET  /companion/state     snapshot puntual (debug, curl). Pide api key.
  WS   /companion/ws        stream de view states. Auth abajo.
  GET  /companion/preview   simulador de la pantalla 480x480. Sin secretos.

Auth del WebSocket: un navegador no puede mandar headers propios en un
WebSocket, y meter la key en la URL la deja en el access log de uvicorn. Por
eso hay dos caminos:
  - header `x-api-key` (la placa ESP32 lo puede mandar), o
  - primer mensaje `{"type": "hello", "key": "..."}` dentro de 5 s.
"""
import asyncio
import hmac
import logging
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
        # Lo que mande el cliente por ahora se ignora (A3 va a usar este
        # canal para el audio/texto del pedido). Leer igual hace falta: es
        # como nos enteramos de que se desconecto.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("companion: error con %s", cliente)
    finally:
        hub.desconectar(ws)
        logger.info("companion: se fue %s", cliente)
