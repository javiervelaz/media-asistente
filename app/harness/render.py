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


AYUDA = """Todo esto sale gratis, sin tocar el modelo:

*Control*
pasala · anterior · pausá · seguí · de nuevo · basta
subile / bajale · "poné el volumen en 40"
qué suena · qué sigue

*Preguntas a la base*
qué escuché hoy (o ayer, esta semana, los últimos 7 días)
qué escuché de Sumo · qué escucho más · qué me salteo
qué tengo en vinilo sin escuchar
qué discos tengo de Wire · quién tocó con Wire
efemérides

*Volver a lo escuchado*
poneme algo que haya escuchado hoy

*Música nueva* (esto sí usa el curador)
decime qué querés: "algo tranqui para cocinar", "cumbia santafesina"."""


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

# ============================================================== H2: lectura
#
# Todo lo de aca abajo reemplaza turnos que costaban ~19k tokens. Ninguna de
# estas funciones llama a nada: recibe filas y devuelve texto.

VACIO = {
    "historial_periodo": "No sonó nada {etiqueta}.",
    "historial_artista": "No tengo nada de {artista} en el historial.",
    "top_escuchados":    "Todavía no hay suficientes escuchas completas para un ranking.",
    "salteados":         "No hay artistas que saltees seguido. Buena señal.",
    "nunca_escuchado":   "Escuchaste todo lo que tenés en vinilo. Impecable.",
    "discografia":       "No tengo discos de {artista} cargados.",
    "relaciones":        "No tengo vínculos cargados para {artista}.",
    "efemerides_hoy":    "Hoy no cumple años ningún disco de la base.",
}


def _mas(total: int, mostrados: int) -> str:
    resto = total - mostrados
    return f"\n… y {resto} más." if resto > 0 else ""


def historial_periodo(rows: list[dict], etiqueta: str) -> str:
    if not rows:
        return VACIO["historial_periodo"].format(etiqueta=etiqueta)
    completos = sum(1 for r in rows if r["completed"])
    skips = sum(1 for r in rows if r["skipped"])
    cab = f"{len(rows)} temas {etiqueta} · {completos} enteros"
    if skips:
        cab += f" · {skips} salteados"
    cuerpo = "\n".join(
        f"{'✓' if r['completed'] else '↷' if r['skipped'] else '·'} "
        f"{r['artist']} — {r['title']}"
        for r in rows[:12])
    return f"{cab}\n{cuerpo}{_mas(len(rows), 12)}"


def historial_artista(rows: list[dict], artista: str, dias: int) -> str:
    if not rows:
        return VACIO["historial_artista"].format(artista=artista)
    total = sum(r["veces"] for r in rows)
    cab = f"{artista} — {total} reproducciones en {dias} días:"
    cuerpo = "\n".join(
        f"· {r['title']} ({r['veces']}x"
        + (f", {r['skips']} salteadas" if r["skips"] else "")
        + ")"
        for r in rows[:12])
    return f"{cab}\n{cuerpo}{_mas(len(rows), 12)}"


def top_escuchados(rows: list[dict], dias: int) -> str:
    if not rows:
        return VACIO["top_escuchados"]
    cuerpo = "\n".join(
        f"{i}. {r['artist']} — {r['completos']} enteros ({r['temas']} temas)"
        for i, r in enumerate(rows[:10], start=1))
    return f"Lo más escuchado en {dias} días:\n{cuerpo}"


def salteados(rows: list[dict], dias: int) -> str:
    if not rows:
        return VACIO["salteados"]
    cuerpo = "\n".join(
        f"· {r['artist']} — {r['skips']} skips ({r['temas']} temas)"
        for r in rows[:10])
    return (f"Lo que salteás seguido ({dias} días):\n{cuerpo}\n\n"
            "Esto ya pesa en el ranking local: cada skip resta.")


def nunca_escuchado(rows: list[dict]) -> str:
    if not rows:
        return VACIO["nunca_escuchado"]
    cuerpo = "\n".join(
        f"· {r['artist']} — {r['album']}" + (f" ({r['anio']})" if r["anio"] else "")
        for r in rows[:12])
    return (f"Del estante, sin escuchar entero todavía:\n{cuerpo}"
            f"{_mas(len(rows), 12)}")


def discografia(rows: list[dict], artista: str) -> str:
    if not rows:
        return VACIO["discografia"].format(artista=artista)
    cuerpo = "\n".join(
        f"· {r['anio'] or '—'}  {r['title']}"
        + ("  ◆" if r["en_vinilo"] else "")
        + (f"  ({r['tracks']}t)" if r["tracks"] else "  (sin tracklist)")
        for r in rows[:15])
    pie = "\n\n◆ = lo tenés en vinilo." if any(r["en_vinilo"] for r in rows) else ""
    return f"{artista}:\n{cuerpo}{_mas(len(rows), 15)}{pie}"


def relaciones(rows: list[dict], artista: str) -> str:
    if not rows:
        return VACIO["relaciones"].format(artista=artista)
    cuerpo = "\n".join(
        f"· {r['name']}"
        + (f" ({r['rel_type']})" if r["rel_type"] else "")
        + (f" — {r['releases']} discos" if r["releases"] else " — sin discografía")
        for r in rows[:12])
    return f"Vinculado con {artista}:\n{cuerpo}{_mas(len(rows), 12)}"


def efemerides_hoy(rows: list[dict]) -> str:
    if not rows:
        return VACIO["efemerides_hoy"]
    cuerpo = "\n".join(
        f"· {r['artist']} — {r['album']} ({r['anio']}"
        + (f", {r['aniversario']} años" if r["aniversario"] else "")
        + ")" + ("  ◆" if r["weight"] == 1 else "")
        for r in rows)
    return f"Un día como hoy:\n{cuerpo}"


def sin_artista(nombre: str) -> str:
    return (f"No tengo a \"{nombre}\" en la base. Si querés que lo traiga de "
            "MusicBrainz, pedime una playlist con ese artista y se hidrata solo.")


def reproducir_historial(resp: dict, etiqueta: str, n: int) -> str:
    ft = resp.get("first_track") or {}
    cab = f"Volviendo a {etiqueta} — {n} temas."
    if ft:
        cab += f"\nArranca: {ft.get('artist')} — {ft.get('title')}"
    return cab


def historial_vacio_para_reproducir(etiqueta: str) -> str:
    return (f"No tengo nada resuelto de {etiqueta} para volver a poner. "
            "Decime qué querés escuchar y lo armo.")
