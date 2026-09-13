"""Verifica que el observador de mpv atribuye el `end-file` al track correcto.

**Por que existe este script.** Durante meses `register_complete` no se llamo
ni una vez. El observador resolvia el track asi:

    path = ev.get("playlist_entry_path") or ""

y mpv **no manda ese campo** en `end-file`: el evento trae `reason`,
`file_error` y `playlist_entry_id`, nada mas. `path` quedaba vacio, `yid` en
None, y el `continue` se comia la unica senal positiva del sistema. Las 26
filas con `completed = true` de la base las puso el backfill de
`migrate_completed.py` a partir de `played_ms`; el observador, cero.

Ningun test lo detectaba porque todos mockeaban el evento **incluyendo el
campo inventado**: confirmaban lo que ya creiamos en vez de lo que mpv hace.
Asi que esto levanta un socket que habla el protocolo JSON IPC de verdad,
deja que el observador se conecte y le manda los eventos tal como mpv los
manda — sin `playlist_entry_path`.

Lo que protege, en orden de importancia:

  1. que el observador PIDA las property-change al conectarse. Si alguien
     saca ese paso, la atribucion vuelve a morir en silencio;
  2. que un `eof` sin `playlist_entry_path` igual se atribuya;
  3. que `played_ms` salga del `time-pos` medido y no de una estimacion;
  4. que un `eof` prematuro (stream cortado) NO cuente como escucha completa;
  5. que `reason=stop` no registre nada y `reason=error` avise.

No gasta tokens y no toca Neon.

Uso:  python -m scripts.verificar_eof
"""
import asyncio
import json
import os
import socket
import sys
import tempfile

# Este script no toca Neon ni la API, asi que tiene que correr en cualquier
# maquina — incluida una sin `.env` completo. `app.config` valida al importar,
# entonces se rellenan los obligatorios con basura antes de importar nada de
# `app`. Si ya estan en el entorno, no se pisan.
os.environ.setdefault("api_key", "verificacion")
os.environ.setdefault("anthropic_api_key", "verificacion")
os.environ.setdefault("database_url", "postgresql://verificacion/verificacion")

from app import player   # noqa: E402

FALLOS: list[str] = []


def check(cond: bool, que: str, detalle: str = "") -> None:
    print(f'  {"ok   " if cond else "FALLA"} {que}' + (f'  [{detalle}]' if detalle else ""))
    if not cond:
        FALLOS.append(que)


class MpvFalso:
    """Un socket que habla como mpv. Sin inventarle campos al protocolo."""

    def __init__(self) -> None:
        self.path = os.path.join(tempfile.mkdtemp(prefix="mpvfake-"), "sock")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(1)
        self.srv.settimeout(10)
        self.cli: socket.socket | None = None
        self.pedidos: list[list] = []

    def aceptar(self) -> None:
        self.cli, _ = self.srv.accept()
        self.cli.settimeout(5)

    def leer_comandos(self, cuantos: int) -> None:
        """Junta los comandos que el observador manda al conectarse."""
        buf = b""
        while len(self.pedidos) < cuantos:
            try:
                chunk = self.cli.recv(4096)
            except socket.timeout:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "command" in msg:
                    self.pedidos.append(msg["command"])
                    # mpv responde success a cada observe_property.
                    self.mandar({"request_id": msg.get("request_id"),
                                 "error": "success", "data": None})

    def mandar(self, obj: dict) -> None:
        self.cli.sendall((json.dumps(obj) + "\n").encode())

    def prop(self, nombre: str, data) -> None:
        self.mandar({"event": "property-change", "id": 1,
                     "name": nombre, "data": data})

    def end_file(self, reason: str, **extra) -> None:
        """El evento REAL: sin `playlist_entry_path`."""
        ev = {"event": "end-file", "reason": reason, "playlist_entry_id": 1}
        ev.update(extra)
        self.mandar(ev)

    def cerrar(self) -> None:
        for s in (self.cli, self.srv):
            try:
                if s:
                    s.close()
            except OSError:
                pass


async def main() -> int:
    mpv = MpvFalso()
    player.settings.mpv_socket = mpv.path

    completos: list[tuple] = []
    fallidos: list[tuple] = []

    async def on_eof(yid: str, played_ms: int) -> None:
        completos.append((yid, played_ms))

    async def on_fail(yid: str, motivo: str) -> None:
        fallidos.append((yid, motivo))

    player.iniciar_observador(on_fail=on_fail, on_eof=on_eof)
    await asyncio.to_thread(mpv.aceptar)

    print("\n1 · el observador pide las property-change al conectarse")
    await asyncio.to_thread(mpv.leer_comandos, len(player.OBSERVADAS))
    pedidas = {c[2] for c in mpv.pedidos if len(c) == 3 and c[0] == "observe_property"}
    for prop in player.OBSERVADAS:
        check(prop in pedidas, f"pide observe_property de {prop!r}")
    check(bool(pedidas), "sin esto la atribucion del eof vuelve a morir muda")

    print("\n2 · eof sin `playlist_entry_path` (el evento real de mpv)")
    archivo = "/home/javier/cache/dGsHLKyZ8H8.webm"
    player.registrar_track(archivo, "dGsHLKyZ8H8")
    mpv.prop("path", archivo)
    mpv.prop("duration", 245.0)
    mpv.prop("time-pos", 120.0)
    mpv.prop("time-pos", 244.3)
    mpv.prop("time-pos", None)          # mpv la deja sin valor al terminar
    mpv.end_file("eof")
    await asyncio.sleep(0.6)

    check(len(completos) == 1, "el eof se atribuyo", f"{completos}")
    if completos:
        yid, ms = completos[0]
        check(yid == "dGsHLKyZ8H8", "atribuido al youtube_id correcto", yid)
        check(abs(ms - 244300) < 1500,
              "played_ms sale del time-pos medido, no de length_ms", f"{ms} ms")

    print("\n3 · el registro en memoria sobrevive por el nombre del archivo")
    player._por_path.clear()            # como despues de un restart
    check(player._yid_de_path("/otro/lado/AbCdEfGhIjK.webm") == "AbCdEfGhIjK",
          "cae al stem del archivo cuando el path no esta registrado")

    print("\n4 · un stream cortado NO es una escucha completa")
    completos.clear()
    mpv.prop("path", "/cache/XXXXXXXXXXX.webm")
    mpv.prop("duration", 300.0)
    mpv.prop("time-pos", 12.0)
    mpv.end_file("eof")
    await asyncio.sleep(0.6)
    check(not completos, "eof con 12s de 300s descartado", f"{completos}")

    print("\n5 · stop manual y error")
    completos.clear()
    mpv.prop("path", "/cache/YYYYYYYYYYY.webm")
    mpv.prop("duration", 200.0)
    mpv.prop("time-pos", 199.0)
    mpv.end_file("stop")
    await asyncio.sleep(0.4)
    check(not completos, "reason=stop no registra escucha (lo hace register_advance)")

    mpv.end_file("error", file_error="loading failed")
    await asyncio.sleep(0.4)
    check(len(fallidos) == 1, "reason=error llama a on_fail", f"{fallidos}")

    print("\n6 · el track en curso es consultable para diagnostico")
    est = player.track_en_curso()
    check(est.get("path", "").endswith("YYYYYYYYYYY.webm"),
          "track_en_curso() refleja el ultimo path observado", str(est.get("path")))

    mpv.cerrar()

    print()
    if FALLOS:
        print(f"{len(FALLOS)} FALLOS:")
        for f in FALLOS:
            print(f"  - {f}")
        return 1
    print("todo ok — el eof se atribuye contra el protocolo real de mpv")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
