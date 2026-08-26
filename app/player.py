"""Control de mpv vía socket IPC"""
import asyncio
import json
import logging
import socket
import threading
from itertools import count
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_req_id = count(1)


class MPVError(Exception):
    pass


# ---------------------------------------------------------------- low level

def _conectar(timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(settings.mpv_socket)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        sock.close()
        raise MPVError(f"mpv socket no disponible: {e}") from e
    return sock


def _enviar_en_socket(sock: socket.socket, command: list[Any]) -> Any:
    """Manda un comando y espera la respuesta con el request_id correspondiente."""
    rid = next(_req_id)
    payload = json.dumps({"command": command, "request_id": rid}) + "\n"
    sock.sendall(payload.encode())

    buffer = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            raise MPVError(f"timeout esperando {command[0]}") from None
        if not chunk:
            raise MPVError(f"mpv cerró la conexión durante {command[0]}")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("request_id") != rid:
                continue          # evento o respuesta de otro comando
            if msg.get("error") != "success":
                raise MPVError(f"{command[0]}: {msg.get('error')}")
            return msg.get("data")


def _send_command(command: list[Any], timeout: float = 2.0) -> Any:
    sock = _conectar(timeout)
    try:
        return _enviar_en_socket(sock, command)
    finally:
        sock.close()


def _send_batch(commands: list[list[Any]], timeout: float = 3.0) -> list[Any]:
    """Varios comandos en una sola conexión. Los que fallan devuelven None."""
    sock = _conectar(timeout)
    out = []
    try:
        for cmd in commands:
            try:
                out.append(_enviar_en_socket(sock, cmd))
            except MPVError:
                out.append(None)
    finally:
        sock.close()
    return out


async def _cmd(command: list[Any], timeout: float = 2.0) -> Any:
    return await asyncio.to_thread(_send_command, command, timeout)


# ---------------------------------------------------------------- comandos

async def clear_playlist() -> None:
    await _cmd(["playlist-clear"])
    await _cmd(["stop"])


async def play_path(path: str | Path, replace: bool = True) -> None:
    """Reproduce un archivo local. La descarga la hace tracks.obtener_track()."""
    mode = "replace" if replace else "append-play"
    await _cmd(["loadfile", str(path), mode])
    await _cmd(["set_property", "pause", False])


async def enqueue_path(path: str | Path) -> None:
    await _cmd(["loadfile", str(path), "append"])


async def pause() -> None:
    await _cmd(["set_property", "pause", True])


async def resume() -> None:
    await _cmd(["set_property", "pause", False])


async def stop() -> None:
    await _cmd(["stop"])


async def next_track() -> None:
    await _cmd(["playlist-next"])


async def prev_track() -> None:
    await _cmd(["playlist-prev"])


async def set_volume(level: int) -> None:
    level = max(0, min(100, level))
    await _cmd(["set_property", "volume", level])


async def set_video(enabled: bool) -> None:
    await _cmd(["set_property", "vid", "auto" if enabled else "no"])


PROPS = ("pause", "media-title", "volume", "time-pos",
         "duration", "playlist-count", "playlist-pos")


async def get_status() -> dict:
    """Una sola conexión para las 7 propiedades."""
    cmds = [["get_property", p] for p in PROPS]
    try:
        vals = await asyncio.to_thread(_send_batch, cmds)
    except MPVError as e:
        logger.error("no se pudo leer estado de mpv: %s", e)
        return {"mpv_ok": False}

    d = dict(zip(PROPS, vals))
    return {
        "mpv_ok": True,
        "paused": d["pause"],
        "title": d["media-title"],
        "volume": d["volume"],
        "position_sec": d["time-pos"],
        "duration_sec": d["duration"],
        "playlist_count": d["playlist-count"],
        "playlist_pos": d["playlist-pos"],
    }


# ---------------------------------------------------------------- observador

_por_path: dict[str, str] = {}     # path absoluto → youtube_id
MAX_REGISTRO = 500                 # techo: el Pi 3B tiene 1 GB


def registrar_track(path: str | Path, youtube_id: str) -> None:
    """Para poder atribuir un end-file a un youtube_id (fallido o completo)."""
    _por_path[str(Path(path).resolve())] = youtube_id
    while len(_por_path) > MAX_REGISTRO:
        _por_path.pop(next(iter(_por_path)))


def _observador(loop: asyncio.AbstractEventLoop, on_fail, on_eof) -> None:
    """Conexión persistente que lee eventos. Corre en un thread propio."""
    while True:
        try:
            sock = _conectar(timeout=None)
        except MPVError as e:
            logger.warning("observador: %s — reintento en 5s", e)
            threading.Event().wait(5)
            continue

        logger.info("observador de mpv conectado")
        buffer = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("event") != "end-file":
                        continue
                    reason = ev.get("reason")
                    if reason == "stop":
                        continue          # stop/next manual, no es error
                    path = ev.get("playlist_entry_path") or ""
                    yid = _por_path.get(str(Path(path).resolve())) if path else None

                    if reason == "eof":
                        # El track llegó al final: es la única señal positiva
                        # del sistema. Antes se descartaba acá mismo y el
                        # término `completos` del scoring valía siempre cero.
                        if yid and on_eof:
                            asyncio.run_coroutine_threadsafe(on_eof(yid), loop)
                        elif not yid:
                            logger.debug("eof de un path no registrado: %s", path)
                        continue

                    logger.error(
                        "mpv end-file reason=%s file_error=%s path=%s yid=%s",
                        reason, ev.get("file_error"), path, yid)
                    if yid and on_fail:
                        asyncio.run_coroutine_threadsafe(
                            on_fail(yid, f"end-file reason={reason}"), loop)
        except OSError as e:
            logger.warning("observador desconectado: %s", e)
        finally:
            sock.close()
        threading.Event().wait(2)


def iniciar_observador(on_fail=None, on_eof=None) -> None:
    """Llamar una vez en el startup de FastAPI.

    on_fail: coroutine (youtube_id, motivo) -> None, típicamente music.mark_failed.
    on_eof:  coroutine (youtube_id) -> None, típicamente history.register_complete.
    """
    loop = asyncio.get_running_loop()
    t = threading.Thread(target=_observador, args=(loop, on_fail, on_eof),
                         daemon=True, name="mpv-observer")
    t.start()