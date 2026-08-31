"""Restauración de `_current` tras un restart. No gasta un token.

El bug: mpv sobrevive a `systemctl restart`, el proceso de Python no. Sin
restaurar, reiniciar con música sonando deja `_current` vacío y la señal de
esa playlist se pierde **en silencio** — los skips no se registran, los
completos tampoco, y `/status` devuelve `dGsHLKyZ8H8.webm` en vez del tema.

Sin argumentos usa una cola simulada (no toca mpv ni tu música).
Con `--real` lee la cola de mpv de verdad y muestra qué restauraría.

Uso:  python -m scripts.verificar_restauracion [--real]
"""
import asyncio
import logging
import sys

from app import history, player
from app.db import close_pool, fetch

logging.basicConfig(level=logging.INFO, format="%(message)s")


async def _simulado() -> list[str]:
    """Toma youtube_ids reales de play_history y los presenta como si fueran
    la cola de mpv."""
    rows = await fetch(
        """
        SELECT youtube_id FROM play_history
        WHERE youtube_id IS NOT NULL AND playlist_id = (
            SELECT playlist_id FROM play_history
            WHERE youtube_id IS NOT NULL
            ORDER BY started_at DESC NULLS LAST, id DESC LIMIT 1)
        ORDER BY position NULLS LAST LIMIT 8
        """)
    return [f"/home/javier/cache/tracks/audio/{r['youtube_id']}.webm"
            for r in rows]


async def main() -> None:
    real = "--real" in sys.argv
    try:
        if real:
            print("Leyendo la cola real de mpv…\n")
            paths = await player.get_playlist()
            if not paths:
                print("mpv no tiene cola cargada. Poné algo y volvé a correr,")
                print("o corré sin --real para usar una cola simulada.")
                return
        else:
            paths = await _simulado()
            if not paths:
                print("No hay play_history con youtube_id para simular.")
                return
            print(f"Cola simulada con {len(paths)} tracks reales de tu "
                  "historial (mpv no se toca)\n")
            player.get_playlist = lambda: asyncio.sleep(0, result=paths)

        n = await history.restaurar_current()
        cur = history.get_current()

        print()
        print("=" * 62)
        print(f"restaurados: {n} de {len(cur['tracks'])} · "
              f"playlist {cur['playlist_id']}")
        print("=" * 62)
        for i, t in enumerate(cur["tracks"][:10]):
            if t.get("artist"):
                print(f"  {i:2}. {t['artist']} — {t['title']}")
            else:
                print(f"  {i:2}. (sin metadata: {t['title']})")

        print()
        if n == 0:
            print("No se restauró nada. Es correcto si mpv tiene una cola que")
            print("no viene de play_history; si no, hay un desalineamiento.")
        else:
            # Lo que importa no es la lista: es que el feedback vuelva a andar.
            t0 = history.get_track_at(0)
            byid = history.find_by_youtube_id(cur["tracks"][0]["youtube_id"])
            ok = t0 is not None and byid is not None
            print(f"{'OK' if ok else 'FALLA'} — get_track_at y "
                  "find_by_youtube_id resuelven:")
            print(f"     posición 0 -> {t0 and t0.get('artist')}")
            print(f"     por yid    -> {byid and byid.get('artist')}")
            print()
            print("Eso es lo que hace que un skip se registre y que un track")
            print("terminado cuente como señal positiva.")
        print("\nllamadas a la API de Anthropic: 0")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
