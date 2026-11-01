"""Hub: un solo loop que lee el estado y lo reparte a todas las pantallas.

Decisiones que importan en una Pi 3B:

- **El loop solo corre si hay alguien mirando.** Sin clientes conectados no
  se toca mpv ni Neon. Se prende con el primer cliente y se apaga solo.
- **Se manda cuando algo cambia, no cada segundo.** La posicion del tema NO
  cuenta como cambio: el cliente la interpola con `position_sec` + `server_ts`.
  Ademas va un latido cada `LATIDO_S` para corregir deriva y para que la
  pantalla note si Charly se murio.
- **Un cliente roto no frena a los demas.** Si un send falla, ese cliente se
  descarta y el resto sigue.
"""
import asyncio
import logging
import time
from zoneinfo import ZoneInfo

from fastapi import WebSocket

from app.companion import state
from app.companion.mood import Contexto, humor
from app.config import settings

logger = logging.getLogger(__name__)

POLL_S = 1.0
LATIDO_S = 15.0
VERSION = 1

try:
    TZ = ZoneInfo(settings.harness_tz)
except Exception:          # tz mal escrita: mismo criterio que harness/queries
    TZ = ZoneInfo("UTC")


def _clave(snap: dict) -> tuple:
    """Lo que, si cambia, justifica mandar un mensaje."""
    t = snap.get("track") or {}
    p = snap.get("playback") or {}
    q = snap.get("queue") or {}
    # total cambia cuando la resolucion en background suma temas a la cola.
    return (snap.get("mode"), snap.get("mood"), t.get("youtube_id"),
            t.get("title"), t.get("cover"), p.get("paused"), p.get("volume"),
            p.get("duration_sec"), q.get("position"), q.get("total"))


class Hub:
    def __init__(self) -> None:
        self.clientes: set[WebSocket] = set()
        self._tarea: asyncio.Task | None = None
        self._seq = 0
        self._ultima_clave: tuple | None = None
        self._ultimo_envio = 0.0
        self._ultimo_snap: dict | None = None
        self._silencio_desde = None
        self._yid_anterior: str | None = None
        self._despertar = asyncio.Event()
        # Un lock por cliente: el loop del hub y los acks de control escriben
        # en el mismo socket desde tareas distintas. Sin esto dos frames
        # pueden pisarse a mitad de camino.
        self._locks: dict[WebSocket, asyncio.Lock] = {}

    # ------------------------------------------------------------ estado

    async def snapshot(self) -> dict:
        """Lee el reproductor y le agrega humor y eventos. Lleva la memoria
        que `state.leer` no tiene: desde cuando hay silencio, si cambio el
        tema."""
        snap = await state.leer()
        ahora = state.ahora_local(TZ)
        t = snap.get("track") or {}
        p = snap.get("playback") or {}
        sonando = snap["mode"] == "now_playing" and not p.get("paused")

        if sonando:
            self._silencio_desde = None
        elif self._silencio_desde is None:
            self._silencio_desde = ahora

        ctx = Contexto(
            ahora=ahora,
            mpv_ok=snap["mode"] != "offline",
            sonando=sonando,
            en_pausa=snap["mode"] == "now_playing" and bool(p.get("paused")),
            de_vinilo=bool(t.get("from_vinyl")),
            artista_coleccion=bool(t.get("artist_in_collection")),
            silencio_desde=self._silencio_desde,
        )
        snap["mood"] = humor(ctx, settings.companion_dormir_desde,
                             settings.companion_dormir_hasta,
                             settings.companion_aburrido_min)

        yid = t.get("youtube_id")
        snap["event"] = ("track_changed"
                         if yid and self._yid_anterior and yid != self._yid_anterior
                         else None)
        if snap["mode"] == "now_playing":
            self._yid_anterior = yid
        return snap

    def _mensaje(self, snap: dict) -> dict:
        self._seq += 1
        return {"v": VERSION, "type": "state", "seq": self._seq,
                "server_ts": time.time(), **snap}

    # ------------------------------------------------------------ clientes

    async def enviar(self, ws: WebSocket, msg: dict) -> None:
        lock = self._locks.setdefault(ws, asyncio.Lock())
        async with lock:
            await ws.send_json(msg)

    async def conectar(self, ws: WebSocket) -> None:
        self.clientes.add(ws)
        # El recien llegado no espera al proximo cambio: recibe el estado ya.
        snap = self._ultimo_snap or await self.snapshot()
        await self.enviar(ws, self._mensaje({**snap, "event": None}))
        if self._tarea is None or self._tarea.done():
            self._tarea = asyncio.create_task(self._loop(), name="companion-hub")

    def desconectar(self, ws: WebSocket) -> None:
        self.clientes.discard(ws)
        self._locks.pop(ws, None)

    async def _broadcast(self, msg: dict) -> None:
        caidos = []
        for ws in list(self.clientes):
            try:
                await self.enviar(ws, msg)
            except Exception:
                caidos.append(ws)
        for ws in caidos:
            self.desconectar(ws)

    async def enviar_texto(self, texto: str, ttl_s: float = 20.0) -> None:
        """Mensaje para mostrar en pantalla (A3: respuestas del chat). Ya
        queda expuesto para no cambiar el protocolo despues."""
        self._seq += 1
        await self._broadcast({"v": VERSION, "type": "message", "seq": self._seq,
                               "server_ts": time.time(), "text": texto,
                               "ttl_s": ttl_s})

    # ------------------------------------------------------------ loop

    async def _loop(self) -> None:
        logger.info("companion: hub arriba (%d cliente/s)", len(self.clientes))
        try:
            while self.clientes:
                try:
                    snap = await self.snapshot()
                except Exception:
                    logger.exception("companion: snapshot fallo")
                    await asyncio.sleep(POLL_S * 5)
                    continue
                self._ultimo_snap = snap
                clave = _clave(snap)
                vencido = time.monotonic() - self._ultimo_envio >= LATIDO_S
                if clave != self._ultima_clave or snap.get("event") or vencido:
                    await self._broadcast(self._mensaje(snap))
                    self._ultima_clave = clave
                    self._ultimo_envio = time.monotonic()
                try:
                    await asyncio.wait_for(self._despertar.wait(), POLL_S)
                except asyncio.TimeoutError:
                    pass
                self._despertar.clear()
        finally:
            # Sin nadie mirando, la proxima conexion arranca de cero.
            self._ultima_clave = None
            self._ultimo_snap = None
            logger.info("companion: hub en pausa, no hay clientes")

    def refrescar(self) -> None:
        """Adelanta el proximo poll. Despues de un control (pausa, siguiente)
        la pantalla no tiene que esperar el segundo del loop para enterarse."""
        self._despertar.set()

    async def cerrar(self) -> None:
        for ws in list(self.clientes):
            try:
                await ws.close(code=1001)
            except Exception:
                pass
        self.clientes.clear()
        self._locks.clear()
        if self._tarea and not self._tarea.done():
            self._tarea.cancel()


hub = Hub()
