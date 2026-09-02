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
    # `is False` a propósito: None significa "no pude saberlo" y no justifica
    # alarmar. Va al final para no partir el bloque del tema.
    if not st.get("paused") and st.get("salida_ok") is False:
        linea += ("\n\n⚠ Estoy reproduciendo pero el audio no llega a "
                  "ninguna salida. Revisá el sink por defecto:\n"
                  "  wpctl status | grep -A5 Sinks")
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
    """`queued` es solo lo que YA se encolo; el resto llega en background.

    Mostrar solo `queued` decia "1 en cola" cuando venian 13 mas en camino:
    parecia que el sistema no habia encontrado nada. `pending` es lo que
    falta resolver.
    """
    titulo = resp.get("title") or "Playlist"
    ft = resp.get("first_track") or {}
    queued = resp.get("queued") or 0
    pending = resp.get("pending") or 0
    linea = f"{titulo}\n"
    if ft:
        linea += f"Suena: {ft.get('artist')} — {ft.get('title')}\n"
    if pending:
        linea += f"{queued + pending} temas ({pending} bajando todavía)."
    else:
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
(y cuando te ofrezco algo, alcanza con "dale")

*Tu colección* (gratis, sale de la base)
poné un disco de mi colección   (un álbum entero, en orden)
poné algo de mi colección       (temas sueltos, variados)
qué tengo en vinilo sin escuchar

*Objetivos*
cómo voy · quiero escuchar más de mi colección
quiero descubrir 5 artistas · quiero escuchar más jazz
quiero escuchar álbumes enteros

*Música nueva* (esto sí usa el curador)
decime qué querés: "algo tranqui para cocinar", "cumbia santafesina".

Hablame como te salga: si no encaja en ningún atajo, lo interpreto igual.
Cuando algo va a gastar tokens, te aviso antes."""


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


def coleccion_vacia(est: dict) -> str:
    """Por que no hay nada que poner del estante. Cada caso tiene un arreglo
    distinto, así que decir "escuchaste todo" en los cuatro es inútil."""
    if not est.get("del_estante"):
        return ("No tengo tu colección cargada: no hay discos con `weight = 1` "
                "en `ephemerides`. Hasta que los cargues, no puedo distinguir "
                "tu estante del resto de la base.")
    if not est.get("con_mbid"):
        return (f"Tengo {est['del_estante']} discos marcados como tuyos, pero "
                "ninguno con `mbid` de MusicBrainz, así que no puedo llegar a "
                "sus temas. Falta hidratarlos.")
    if not est.get("con_tracklist"):
        return (f"Tus {est['con_mbid']} discos tienen mbid pero ninguno tiene "
                "tracklist cargada en `recordings`. Pedime una playlist con "
                "alguno de esos artistas y se hidrata solo.")
    return None


def nunca_escuchado(rows: list[dict], ofrecible: bool = False,
                    est: dict | None = None) -> str:
    if not rows:
        # "Escuchaste todo" solo se puede afirmar si HAY colección cargada.
        problema = coleccion_vacia(est or {})
        return problema or VACIO["nunca_escuchado"]
    cuerpo = "\n".join(
        f"· {r['artist']} — {r['album']}" + (f" ({r['anio']})" if r["anio"] else "")
        for r in rows[:12])
    return (f"Del estante, sin escuchar entero todavía:\n{cuerpo}"
            f"{_mas(len(rows), 12)}" + (OFRECER if ofrecible else ""))


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


OFRECER = "\n\n¿Lo pongo? (dale / no)"


def efemerides_hoy(rows: list[dict], ofrecible: bool = False) -> str:
    if not rows:
        return VACIO["efemerides_hoy"]
    cuerpo = "\n".join(
        f"· {r['artist']} — {r['album']} ({r['anio']}"
        + (f", {r['aniversario']} años" if r["aniversario"] else "")
        + ")" + ("  ◆" if r["weight"] == 1 else "")
        for r in rows)
    return f"Un día como hoy:\n{cuerpo}" + (OFRECER if ofrecible else "")


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


def confirmado(resp: dict, etiqueta: str, n: int) -> str:
    ft = resp.get("first_track") or {}
    linea = f"Va: {etiqueta} — {n} temas."
    if ft:
        linea += f"\nArranca: {ft.get('artist')} — {ft.get('title')}"
    return linea


def sin_oferta_nada_que_poner(etiqueta: str) -> str:
    return (f"No pude armar la cola de {etiqueta}: esos discos no tienen "
            "tracklist cargada. Pedime una playlist con alguno y se hidrata.")


def rechazado() -> str:
    return "Listo, no toco nada."


def nada_que_confirmar() -> str:
    return "No te ofrecí nada. Mandá 'ayuda' si querés ver qué manejo."


def confirmar_gasto(texto: str, tokens: int, entendido: bool) -> str:
    """Aviso antes de gastar. El numero sale de turn_log, no de una constante."""
    aprox = f"~{tokens // 1000}k tokens" if tokens >= 1000 else f"~{tokens} tokens"
    if entendido:
        cab = f"Eso lo arma el curador ({aprox})."
    else:
        cab = (f"No entendí \"{texto}\" como comando. Puedo mandárselo al "
               f"curador y que arme una playlist, pero eso gasta ({aprox}).")
    return f"{cab}\n¿Lo hago? (dale / no)"


# ================================================================ H4: objetivos

def _barra(actual: float, target: float, ancho: int = 10) -> str:
    if target <= 0:
        return ""
    llenos = max(0, min(ancho, round(ancho * actual / target)))
    return "█" * llenos + "·" * (ancho - llenos)


def objetivos(estados: list[dict]) -> str:
    if not estados:
        return ("No tenés objetivos activos. Se declaran hablando:\n"
                "· \"quiero escuchar más de mi colección\"\n"
                "· \"quiero descubrir 5 artistas\"\n"
                "· \"quiero escuchar más jazz\"\n"
                "· \"quiero escuchar álbumes enteros\"")

    lineas = []
    for e in estados:
        nombre = ETIQUETA_OBJ.get(e["kind"], e["kind"])
        if e["kind"] == "genero":
            g = (e.get("spec") or {}).get("genero", "")
            nombre = f"escuchar {g}" if g else nombre
        u = e["unidad"]
        actual = f"{e['actual']:.0f}{'%' if u == '%' else ''}"
        target = f"{e['target']:.0f}{'%' if u == '%' else ''}"
        cola = "" if u == "%" else f" {u}"
        marca = "  ✓" if e.get("cumplido") else ""
        lineas.append(f"{_barra(e['actual'], e['target'])}  {nombre}: "
                      f"{actual} de {target}{cola}{marca}")
        if not e["suficiente"]:
            lineas.append(f"    (solo {e['muestra']} escuchas completas en "
                          f"{e['dias']} días — todavía es ruido, no sesga nada)")

    return "Cómo venís:\n" + "\n".join(lineas)


ETIQUETA_OBJ = {
    "coleccion": "colección en vinilo",
    "descubrimiento": "artistas nuevos",
    "genero": "género",
    "profundidad": "álbumes enteros",
}


def objetivo_declarado(kind: str, e: dict) -> str:
    nombre = ETIQUETA_OBJ.get(kind, kind)
    if kind == "genero":
        nombre = (e.get("spec") or {}).get("genero", nombre)
    u = e["unidad"]
    target = f"{e['target']:.0f}{'%' if u == '%' else ''}"
    cola = "" if u == "%" else f" {u}"
    txt = f"Anotado: {nombre}, {target}{cola} en {e['dias']} días."
    if e["suficiente"]:
        actual = f"{e['actual']:.0f}{'%' if u == '%' else ''}"
        txt += f" Ahora vas {actual}{cola}."
    else:
        txt += (f" Todavía no hay muestra suficiente ({e['muestra']} escuchas "
                "completas), así que no va a sesgar nada hasta que la haya.")
    return txt


def objetivo_borrado(que: str, n: int) -> str:
    return (f"Listo, saqué el objetivo de {que}." if n
            else f"No tenías ningún objetivo de {que}.")


def objetivo_playlist(resp: dict, e: dict, n: int) -> str:
    nombre = ETIQUETA_OBJ.get(e["kind"], e["kind"])
    ft = resp.get("first_track") or {}
    linea = f"Para el objetivo más atrasado ({nombre}) — {n} temas."
    if ft:
        linea += f"\nArranca: {ft.get('artist')} — {ft.get('title')}"
    return linea


def sin_objetivo_atrasado() -> str:
    return ("No hay objetivos pendientes. O los cumpliste todos, o no "
            "declaraste ninguno todavía — mandá 'cómo voy'.")


def sin_material_objetivo(nombre: str) -> str:
    return (f"No encontré material sin escuchar para el objetivo de {nombre}. "
            "Pedime una playlist normal y el grafo se hidrata solo.")


def coleccion(resp: dict, n: int) -> str:
    ft = resp.get("first_track") or {}
    linea = f"De tu colección — {n} temas."
    if ft:
        linea += f"\nArranca: {ft.get('artist')} — {ft.get('title')}"
    return linea


def disco_coleccion(tracks: list[dict]) -> str:
    """Un disco entero: lo que importa es el álbum, no el primer tema."""
    if not tracks:
        return sin_disco_coleccion()
    t0 = tracks[0]
    faltan = sum(1 for t in tracks if not t.get("listo"))
    linea = f"{t0.get('artist')} — {t0.get('album')}\n{len(tracks)} temas, en orden."
    if faltan:
        linea += f" ({faltan} hay que bajarlos)"
    return linea


def sin_disco_coleccion() -> str:
    return ("No encontré un disco de tu colección para poner entero: los que "
            "tenés cargados o sonaron hace poco, o no tienen tracklist. "
            "Probá \"algo de mi colección\" para temas sueltos.")


def coleccion_artista(tracks: list[dict], artista: str) -> str:
    albumes = []
    for t in tracks:
        if t.get("album") and t["album"] not in albumes:
            albumes.append(t["album"])
    linea = f"{artista}, de tu colección — {len(tracks)} temas"
    if albumes:
        linea += f" de {len(albumes)} disco" + ("s" if len(albumes) > 1 else "")
        linea += ":\n" + "\n".join(f"· {a}" for a in albumes[:5])
    return linea


def sin_artista_en_coleccion(artista: str) -> str:
    return (f"No tenés discos de {artista} en el estante. "
            f"Puedo armarte algo igual: \"poné {artista}\".")
