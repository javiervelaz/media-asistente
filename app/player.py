"""Control de mpv vía socket IPC"""
import json
import logging
import socket
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


class MPVError(Exception):
    pass


def _send_command(command: list[Any], timeout: float = 2.0) -> dict:
    """Manda un comando JSON a mpv y devuelve la respuesta parseada"""
    payload = json.dumps({"command": command}) + "\n"

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(settings.mpv_socket)
        sock.sendall(payload.encode())

        # mpv puede mandar varios eventos antes de la respuesta del comando.
        # Leemos hasta encontrar una línea que tenga 'request_id' o 'error'.
        buffer = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
            # Procesamos línea por línea
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "error" in msg:
                    sock.close()
                    if msg["error"] != "success":
                        raise MPVError(msg["error"])
                    return msg
        sock.close()
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise MPVError(f"mpv socket not available: {e}") from e
    except socket.timeout:
        raise MPVError("mpv timeout") from None

    return {}


def play_url(url: str, replace: bool = True) -> None:
    mode = "replace" if replace else "append-play"
    _send_command(["loadfile", url, mode])


def enqueue_url(url: str) -> None:
    _send_command(["loadfile", url, "append-play"])


def pause() -> None:
    _send_command(["set_property", "pause", True])


def resume() -> None:
    _send_command(["set_property", "pause", False])


def stop() -> None:
    _send_command(["stop"])


def next_track() -> None:
    _send_command(["playlist-next"])


def prev_track() -> None:
    _send_command(["playlist-prev"])


def set_volume(level: int) -> None:
    level = max(0, min(100, level))
    _send_command(["set_property", "volume", level])


def get_status() -> dict:
    """Devuelve estado actual: si está sonando, qué, volumen, posición"""
    def _get(prop):
        try:
            r = _send_command(["get_property", prop])
            return r.get("data")
        except MPVError:
            return None

    return {
        "paused": _get("pause"),
        "title": _get("media-title"),
        "volume": _get("volume"),
        "position_sec": _get("time-pos"),
        "duration_sec": _get("duration"),
        "playlist_count": _get("playlist-count"),
        "playlist_pos": _get("playlist-pos"),
    }


def clear_playlist() -> None:
    _send_command(["playlist-clear"])

def set_video(enabled: bool) -> None:
    """Activa o desactiva el track de video del archivo actual"""
    _send_command(["set_property", "vid", "auto" if enabled else "no"])