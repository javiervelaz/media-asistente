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


async def restart_track() -> None:
    """Vuelve al principio del tema actual sin recargarlo."""
    await _cmd(["seek", 0, "absolute"])
    await _cmd(["set_property", "pause", False])


async def set_volume(level: int) -> None:
    level = max(0, min(100, level))
    await _cmd(["set_property", "volume", level])


async def set_video(enabled: bool) -> None:
    await _cmd(["set_property", "vid", "auto" if enabled else "no"])


async def get_playlist() -> list[str]:
    """Paths de la cola actual de mpv, en orden.

    Es la unica fuente confiable del ORDEN despues de un reinicio del
    servicio: mpv sobrevive, el proceso de Python no.
    """
    try:
        entradas = await _cmd(["get_property", "playlist"]) or []
    except MPVError as e:
        logger.debug("no pude leer la playlist de mpv: %s", e)
        return []
    return [e.get("filename") or "" for e in entradas]


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


# ------------------------------------------------------- salida de audio real

_SALIDA_CACHE: dict = {"ok": None, "hasta": 0.0}
SALIDA_TTL_S = 5.0


def _hay_stream() -> bool | None:
    """¿El audio de mpv llega a algún sink?

    mpv puede reportar que reproduce —posición avanzando, sin pausa— mientras
    su salida no llega a ningún lado: pasa cuando el sink por defecto de
    PipeWire apunta a un device que ya no existe (un Bluetooth desconectado,
    por ejemplo). Ahí `/status` dice todo verde y no se escucha nada.

    Devuelve None si no se puede saber: mejor callarse que inventar un
    diagnóstico. Nunca levanta.
    """
    import shutil
    import subprocess

    if not shutil.which("pactl"):
        return None
    try:
        out = subprocess.run(["pactl", "list", "short", "sink-inputs"],
                             capture_output=True, text=True, timeout=2)
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    return any(l.strip() for l in out.stdout.splitlines())


def salida_activa() -> bool | None:
    """`_hay_stream` cacheado: se consulta en cada `qué suena` y no vale una
    llamada a pactl por turno."""
    import time as _t
    ahora = _t.monotonic()
    if _SALIDA_CACHE["ok"] is not None and ahora < _SALIDA_CACHE["hasta"]:
        return _SALIDA_CACHE["ok"]
    ok = _hay_stream()
    if ok is not None:
        _SALIDA_CACHE["ok"], _SALIDA_CACHE["hasta"] = ok, ahora + SALIDA_TTL_S
    return ok


# ---------------------------------------------------------------- observador

_por_path: dict[str, str] = {}     # path absoluto -> youtube_id
MAX_REGISTRO = 500                 # techo: el Pi 3B tiene 1 GB

#: Estado del track en curso, mantenido por el observador desde
#: `property-change`.
#:
#: **Por que existe:** mpv NO manda el path en el evento `end-file`. Los
#: campos del evento son `reason`, `file_error` y `playlist_entry_id` — nunca
#: `playlist_entry_path`, que es lo que este modulo leia desde el primer
#: commit. Resultado: `yid` era siempre None, el `eof` nunca se atribuia y
#: `register_complete` no se llamo una sola vez. Las 26 filas con
#: `completed = true` que hay en la base las puso el backfill de
#: `migrate_completed.py` a partir de `played_ms`, no el observador.
#:
#: La unica forma confiable de saber QUE termino es haber seguido `path`
#: mientras sonaba. De paso, seguir `time-pos` y `duration` da el `played_ms`
#: real de una escucha completa, que antes se estimaba con `length_ms`.
_actual: dict = {"path": None, "time_pos": 0.0, "duration": None}

#: Propiedades que el observador pide al conectarse.
OBSERVADAS = ("path", "time-pos", "duration")

#: Un `eof` con menos de esta fraccion de la duracion reproducida no es una
#: escucha completa: es un stream que se corto. Sin esta guarda, una descarga
#: trunca entra a la base como senal positiva y envenena el ranking.
EOF_RATIO_MINIMO = 0.6

_diagnostico_end_file = True       # loguea el primer end-file crudo


def registrar_track(path: str | Path, youtube_id: str) -> None:
    """Para poder atribuir un end-file a un youtube_id (fallido o completo)."""
    _por_path[str(Path(path).resolve())] = youtube_id
    while len(_por_path) > MAX_REGISTRO:
        _por_path.pop(next(iter(_por_path)))


def track_en_curso() -> dict:
    """Lo que el observador cree que esta sonando. Para diagnostico."""
    return dict(_actual)


def _yid_de_path(path: str) -> str | None:
    """youtube_id de un path, con red de contencion.

    Los archivos del cache se llaman `<youtube_id>.<ext>`, asi que si el
    registro en memoria no tiene ese path —un restart del servicio, un path
    relativo— el nombre del archivo alcanza. `register_complete` valida
    contra `play_history` de todos modos, asi que un stem que no corresponda
    no marca nada.
    """
    if not path:
        return None
    yid = _por_path.get(str(Path(path).resolve()))
    if yid:
        return yid
    stem = Path(path).stem
    return stem or None


def _pedir_observaciones(sock: socket.socket) -> None:
    """Pide las property-change SIN esperar respuesta.

    No usa `_enviar_en_socket` a proposito: ese metodo descarta todo lo que
    no traiga su `request_id`, y en este socket lo que no trae request_id son
    justamente los eventos. Las respuestas de estos comandos las ve el loop
    de lectura y las ignora por no tener clave `event`.
    """
    for i, prop in enumerate(OBSERVADAS, start=1):
        payload = json.dumps({"command": ["observe_property", i, prop],
                              "request_id": next(_req_id)}) + "\n"
        sock.sendall(payload.encode())


def _aplicar_property_change(ev: dict) -> None:
    nombre, data = ev.get("name"), ev.get("data")
    if nombre == "path":
        # data None al final de la cola: se conserva el ultimo path valido
        # para que el end-file que viene atras se pueda atribuir.
        if data:
            _actual.update(path=str(data), time_pos=0.0, duration=None)
    elif nombre == "time-pos":
        # mpv manda None al terminar el archivo. Guardar el ultimo valor
        # bueno es justo lo que hace medible cuanto se escucho.
        if isinstance(data, (int, float)):
            _actual["time_pos"] = float(data)
    elif nombre == "duration":
        if isinstance(data, (int, float)) and data > 0:
            _actual["duration"] = float(data)


def _observador(loop: asyncio.AbstractEventLoop, on_fail, on_eof) -> None:
    """Conexion persistente que lee eventos. Corre en un thread propio."""
    global _diagnostico_end_file

    while True:
        try:
            sock = _conectar(timeout=None)
        except MPVError as e:
            logger.warning("observador: %s - reintento en 5s", e)
            threading.Event().wait(5)
            continue

        logger.info("observador de mpv conectado")
        try:
            _pedir_observaciones(sock)
        except OSError as e:
            logger.warning("observador: no pude pedir las propiedades: %s", e)
            sock.close()
            threading.Event().wait(2)
            continue

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

                    evento = ev.get("event")
                    if evento == "property-change":
                        _aplicar_property_change(ev)
                        continue
                    if evento != "end-file":
                        continue

                    if _diagnostico_end_file:
                        # Una vez, para poder confirmar contra mpv real que
                        # campos trae de verdad el evento.
                        logger.info("primer end-file crudo: %s", ev)
                        _diagnostico_end_file = False

                    reason = ev.get("reason")
                    if reason == "stop":
                        continue          # next/stop manual, no es error

                    # `playlist_entry_path` primero por si alguna version de
                    # mpv lo manda; el seguimiento de `path` es el que
                    # realmente funciona.
                    path = ev.get("playlist_entry_path") or _actual.get("path") or ""
                    yid = _yid_de_path(path)
                    escuchado = float(_actual.get("time_pos") or 0.0)
                    duracion = _actual.get("duration")

                    if reason == "eof":
                        if not yid:
                            logger.warning(
                                "eof sin path atribuible (path=%r): la senal "
                                "positiva de este track se pierde", path)
                            continue
                        # Un stream cortado tambien llega como eof. Si se
                        # escucho menos del minimo, no es una escucha
                        # completa y no entra a la base como tal.
                        if duracion and escuchado < duracion * EOF_RATIO_MINIMO:
                            logger.info(
                                "eof prematuro: %s (%.0fs de %.0fs) - no cuenta",
                                yid, escuchado, duracion)
                            continue
                        if on_eof:
                            asyncio.run_coroutine_threadsafe(
                                on_eof(yid, int(escuchado * 1000)), loop)
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

    on_fail: coroutine (youtube_id, motivo) -> None, tipicamente music.mark_failed.
    on_eof:  coroutine (youtube_id, played_ms) -> None, tipicamente
             history.register_complete.
    """
    loop = asyncio.get_running_loop()
    t = threading.Thread(target=_observador, args=(loop, on_fail, on_eof),
                         daemon=True, name="mpv-observer")
    t.start()
