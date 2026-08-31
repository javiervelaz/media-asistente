"""Plantillas de salida. El LLM no entra aca.

El antipatron que este modulo evita: SQL devuelve 14 filas -> se las mandas al
modelo para que redacte lindo -> 600 tokens por una pregunta que un f-string
resuelve en cero. A 50 turnos diarios gastas mas en narrar el historial que en
curar musica.

Aburrido a proposito. Un renderer aburrido y gratis le gana a uno simpatico que
cuesta por turno y que ocasionalmente inventa un track que no estaba en las
filas.
"""

MPV_CAIDO = "mpv no responde. Los datos los puedo consultar igual."


def _mmss(seg: float | None) -> str:
    if not seg or seg < 0:
        return "0:00"
    seg = int(seg)
    return f"{seg // 60}:{seg % 60:02d}"


def ok_control(accion: str) -> str:
    return {
        "play": "Dale.",
        "pause": "Pausado.",
        "next": "Siguiente.",
        "prev": "Anterior.",
        "stop": "Listo, corto.",
        "replay": "Desde el principio.",
    }.get(accion, "Ok.")


def volumen(nivel: int) -> str:
    return f"Volumen {nivel}."


def estado_actual(st: dict, track: dict | None) -> str:
    if not st.get("mpv_ok"):
        return MPV_CAIDO
    if st.get("playlist_pos") is None and not st.get("title"):
        return "No hay nada sonando."

    if track:
        cab = f"{track['artist']} — {track['title']}"
        razon = track.get("rationale")
    else:
        # Sin playlist en memoria: reinicio del servicio o playlist vieja.
        cab = st.get("title") or "—"
        razon = None

    tiempo = f"{_mmss(st.get('position_sec'))} / {_mmss(st.get('duration_sec'))}"
    linea = f"{cab}  ({tiempo})"
    if st.get("paused"):
        linea += "  [pausado]"
    if razon:
        linea += f"\n{razon}"
    return linea


def estado_cola(st: dict, tracks: list[dict], pos: int | None,
                maximo: int = 5) -> str:
    if not st.get("mpv_ok"):
        return MPV_CAIDO

    total = st.get("playlist_count") or 0
    if not tracks:
        # mpv tiene cola pero el servicio se reinicio: sabemos cuantos, no cuales.
        if total > 1 and pos is not None:
            return f"Quedan {total - pos - 1} temas en la cola (sin detalle: se reinicio el servicio)."
        return "No hay nada en la cola."

    i = (pos or 0) + 1
    siguientes = tracks[i:i + maximo]
    if not siguientes:
        return "Este es el ultimo de la cola."

    cuerpo = "\n".join(f"{n}. {t['artist']} — {t['title']}"
                       for n, t in enumerate(siguientes, start=1))
    restan = len(tracks) - i - len(siguientes)
    cola = f"\n… y {restan} mas." if restan > 0 else ""
    return f"Despues de esta:\n{cuerpo}{cola}"


def playlist(resp: dict) -> str:
    titulo = resp.get("title") or "Playlist"
    ft = resp.get("first_track") or {}
    queued = resp.get("queued") or 0
    linea = f"{titulo}\n"
    if ft:
        linea += f"Suena: {ft.get('artist')} — {ft.get('title')}\n"
    linea += f"{queued} en cola."
    m = resp.get("metrics") or {}
    if m.get("libres"):
        linea += f"  ({m['libres']} sin verificar)"
    return linea


AYUDA = """Manejo esto sin gastar un token:

· pasala · anterior · pausá · seguí · de nuevo · basta
· subile / bajale (o "poné el volumen en 40")
· qué suena · qué sigue

Y para armar música, decime qué querés escuchar:
"poneme algo tranquilo para cocinar", "cumbia santafesina",
"post-punk británico del 80"."""


def saludo(track: dict | None) -> str:
    if track:
        return (f"Acá andamos. Ahora suena {track['artist']} — {track['title']}.\n"
                "Decime qué querés escuchar, o mandá 'ayuda'.")
    return "Acá andamos. Decime qué querés escuchar, o mandá 'ayuda'."


def ayuda() -> str:
    return AYUDA


def no_entendido() -> str:
    return ("No te entendi. Por ahora manejo los controles (pausa, siguiente, "
            "volumen) y te digo que suena o que sigue.")


def repreguntar(texto: str) -> str:
    """Para lo que no se entendio y es demasiado corto como para adivinar.

    Repreguntar cuesta cero. Adivinar cuesta una sesion de curador y una
    playlist que nadie pidio.
    """
    return (f"No sé qué hacer con \"{texto}\". Si es un pedido de música "
            "decímelo un poco más largo (\"poné algo tranqui\", "
            "\"cumbia santafesina\"); si no, mandá 'ayuda'.")


def error(detalle: str) -> str:
    return f"No pude: {detalle}"
