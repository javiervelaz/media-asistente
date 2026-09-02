"""Un ejecutor por intent. Ninguno de estos gasta un token.

Los intents de control reusan exactamente lo que ya hace /control/*: mismo
register_advance antes de saltar, mismo cancelado de fade. Si el harness
saltara ese paso, los skips que llegan por chat no se registrarian y la senal
que alimenta local_search quedaria a medias segun por donde entraste.

`main.py` inyecta los cancelables en el startup para no importar main desde
aca (ciclo de imports).
"""
import asyncio
import logging
from typing import Awaitable, Callable

from app import player
from app.harness import goals, queries, render
from app.harness.intents import Intent, Result
from app.harness.session import SessionState
from app.history import get_current, get_track_at, register_advance
from app.player import MPVError

logger = logging.getLogger(__name__)

DELTA_VOL = 10          # cuanto mueve un "subile" pelado

# --- hooks que vive en main.py ----------------------------------------------
_cancel_fade: Callable[[], None] = lambda: None
_cancel_resto: Callable[[], None] = lambda: None
_crear_playlist = None          # (prompt, room_id) -> dict
_lanzar_tracks = None           # (tracks, titulo, room_id) -> dict


def set_hooks(cancel_fade=None, cancel_resto=None, crear_playlist=None,
              lanzar_tracks=None) -> None:
    """Se llama una vez desde el lifespan de FastAPI."""
    global _cancel_fade, _cancel_resto, _crear_playlist, _lanzar_tracks
    if cancel_fade:
        _cancel_fade = cancel_fade
    if cancel_resto:
        _cancel_resto = cancel_resto
    if crear_playlist:
        _crear_playlist = crear_playlist
    if lanzar_tracks:
        _lanzar_tracks = lanzar_tracks


# --- control ----------------------------------------------------------------

async def _play(intent, st) -> Result:
    await player.resume()
    return Result(render.ok_control("play"), actions=["play"])


async def _pause(intent, st) -> Result:
    _cancel_fade()
    await player.pause()
    return Result(render.ok_control("pause"), actions=["pause"])


async def _next(intent, st) -> Result:
    _cancel_fade()
    await register_advance("next")      # antes de saltar, igual que /control/next
    await player.next_track()
    return Result(render.ok_control("next"), actions=["next"])


async def _prev(intent, st) -> Result:
    _cancel_fade()
    await register_advance("prev")      # volver atras no es skip
    await player.prev_track()
    return Result(render.ok_control("prev"), actions=["prev"])


async def _stop(intent, st) -> Result:
    _cancel_fade()
    _cancel_resto()
    await player.stop()
    return Result(render.ok_control("stop"), actions=["stop"])


async def _replay(intent, st) -> Result:
    _cancel_fade()
    await player.restart_track()
    return Result(render.ok_control("replay"), actions=["replay"])


async def _vol(intent: Intent, st, signo: int) -> Result:
    _cancel_fade()
    estado = await player.get_status()
    if not estado.get("mpv_ok"):
        raise MPVError("mpv no responde")
    actual = int(estado.get("volume") or 0)
    delta = int(intent.slots.get("delta") or DELTA_VOL)
    nuevo = max(0, min(100, actual + signo * delta))
    await player.set_volume(nuevo)
    return Result(render.volumen(nuevo), data={"level": nuevo},
                  actions=["volume"])


async def _vol_up(intent, st) -> Result:
    return await _vol(intent, st, +1)


async def _vol_down(intent, st) -> Result:
    return await _vol(intent, st, -1)


async def _vol_set(intent: Intent, st) -> Result:
    _cancel_fade()
    nivel = max(0, min(100, int(intent.slots.get("level", 50))))
    await player.set_volume(nivel)
    return Result(render.volumen(nivel), data={"level": nivel},
                  actions=["volume"])


# --- estado -----------------------------------------------------------------

async def _estado_actual(intent, st: SessionState) -> Result:
    estado = await player.get_status()
    # mpv puede estar "reproduciendo" contra la nada: sin esto el bot dice
    # "Suena: X" con total confianza mientras no sale un sonido.
    if estado.get("mpv_ok") and not estado.get("paused"):
        estado["salida_ok"] = await asyncio.to_thread(player.salida_activa)
    t = get_track_at(estado.get("playlist_pos")) if estado.get("mpv_ok") else None
    if t:
        st.tocar(last_artist=t.get("artist"),
                 last_artist_mbid=t.get("artist_mbid"))
    return Result(render.estado_actual(estado, t),
                  ok=bool(estado.get("mpv_ok")),
                  data={"now_playing": f"{t['artist']} - {t['title']}" if t else None})


async def _estado_cola(intent, st) -> Result:
    estado = await player.get_status()
    cur = get_current()
    return Result(
        render.estado_cola(estado, cur.get("tracks") or [],
                           estado.get("playlist_pos")),
        ok=bool(estado.get("mpv_ok")))


async def _playlist(intent: Intent, st: SessionState) -> Result:
    """EL UNICO ejecutor que gasta tokens. Reusa /playlist entero: mismo
    ruteo local/hybrid/curador, mismo fade, mismo registro en play_history.

    El `usage` que vuelve se escribe en turn_log: es lo que convierte
    "me parece que gasta poco" en un numero por turno.
    """
    if _crear_playlist is None:
        return Result(render.error("el generador de playlists no esta enganchado"),
                      ok=False)
    prompt = (intent.slots.get("prompt") or "").strip()
    if not prompt:
        return Result(render.no_entendido(), ok=False)

    resp = await _crear_playlist(prompt, st.room_id)
    st.tocar(last_playlist_id=str(resp.get("playlist_id") or "") or None)
    # `gasto` marca el turno como pago para turn_log. No alcanza con mirar el
    # nombre del intent: por una confirmacion el intent ruteado es `confirmar`,
    # y no alcanza con los tokens porque la via local devuelve 0 sin usage.
    return Result(render.playlist(resp), data={**resp, "gasto": True},
                  actions=["playlist"])


async def _saludo(intent, st: SessionState) -> Result:
    """Un saludo no es un pedido de musica. Antes caia al fallback y el
    curador leia "hola charly" como un pedido de Charly Garcia: 19k tokens
    por decir hola."""
    try:
        estado = await player.get_status()
        t = get_track_at(estado.get("playlist_pos")) if estado.get("mpv_ok") else None
    except MPVError:
        t = None
    return Result(render.saludo(t))


async def _ayuda(intent, st) -> Result:
    return Result(render.ayuda())


async def _repreguntar(intent: Intent, st) -> Result:
    return Result(render.repreguntar(intent.slots.get("texto", "")), ok=False)


async def _no_entendido(intent, st) -> Result:
    return Result(render.no_entendido(), ok=False)



# ============================================================== H2: lectura
#
# Ninguno de estos gasta un token. Cada uno reemplaza un turno que caia al
# fallback y terminaba en el curador — que ademas no podia responderlo: su
# tool `get_play_history` esta descrita para EVITAR lo reciente, trae 30 dias
# agrupados y sin recording_mbid.

DIAS_ARTISTA = 90
DIAS_TOP = 30
DIAS_SKIPS = 90


async def _con_artista(intent: Intent, st: SessionState):
    """Resuelve el slot `artista` contra el grafo.

    Si el usuario no nombro a nadie, usa el ultimo mencionado en la sesion:
    asi "y quien toco con el" funciona sin mandar historial al modelo.
    """
    nombre = (intent.slots.get("artista") or "").strip()
    if not nombre and st.last_artist:
        nombre = st.last_artist
    if not nombre:
        return None, None
    a = await queries.resolver_artista(nombre)
    if a:
        st.tocar(last_artist=a["name"], last_artist_mbid=str(a["mbid"]))
    return a, nombre


async def _historial_periodo(intent: Intent, st) -> Result:
    v = queries.ventana(intent.slots.get("cuando", ""))
    rows = await queries.historial_periodo(v)
    return Result(render.historial_periodo(rows, v.etiqueta),
                  data={"count": len(rows), "ventana": v.etiqueta})


async def _historial_artista(intent: Intent, st) -> Result:
    a, nombre = await _con_artista(intent, st)
    if not nombre:
        return Result(render.no_entendido(), ok=False)
    rows = await queries.historial_artista(
        a["mbid"] if a else None, nombre, DIAS_ARTISTA)
    etiqueta = a["name"] if a else nombre
    return Result(render.historial_artista(rows, etiqueta, DIAS_ARTISTA),
                  data={"count": len(rows)})


async def _top_escuchados(intent, st) -> Result:
    rows = await queries.top_escuchados(DIAS_TOP)
    return Result(render.top_escuchados(rows, DIAS_TOP), data={"count": len(rows)})


async def _salteados(intent, st) -> Result:
    rows = await queries.salteados(DIAS_SKIPS)
    return Result(render.salteados(rows, DIAS_SKIPS), data={"count": len(rows)})


async def _nunca_escuchado(intent, st: SessionState) -> Result:
    rows = await queries.nunca_escuchado()
    est = await queries.estado_coleccion() if not rows else None
    mbids = [r["mbid"] for r in rows[:6] if r.get("mbid")]
    if mbids:
        st.ofrecer("reproducir_releases", "los del estante", mbids=mbids)
    return Result(render.nunca_escuchado(rows, ofrecible=bool(mbids), est=est),
                  ok=bool(rows or not est or est.get("con_tracklist")),
                  data={"count": len(rows)})


async def _discografia(intent: Intent, st) -> Result:
    a, nombre = await _con_artista(intent, st)
    if not nombre:
        return Result(render.no_entendido(), ok=False)
    if not a:
        return Result(render.sin_artista(nombre), ok=False)
    rows = await queries.discografia(a["mbid"])
    return Result(render.discografia(rows, a["name"]), data={"count": len(rows)})


async def _relaciones(intent: Intent, st) -> Result:
    a, nombre = await _con_artista(intent, st)
    if not nombre:
        return Result(render.no_entendido(), ok=False)
    if not a:
        return Result(render.sin_artista(nombre), ok=False)
    rows = await queries.relaciones(a["mbid"])
    return Result(render.relaciones(rows, a["name"]), data={"count": len(rows)})


async def _efemerides_hoy(intent, st: SessionState) -> Result:
    rows = await queries.efemerides_hoy()
    mbids = [r["mbid"] for r in rows if r.get("mbid")]
    if mbids:
        st.ofrecer("reproducir_releases", "las efemérides de hoy", mbids=mbids)
    return Result(render.efemerides_hoy(rows, ofrecible=bool(mbids)),
                  data={"count": len(rows)})


async def _reproducir_releases(intent: Intent, st: SessionState) -> Result:
    """Encola discos concretos. Lo que suena es exactamente lo que se listo.

    No usa /despertador aunque parezca lo mismo: el despertador tiene su
    propia ventana (±7 dias, solo Album, sin compilations) y devolveria un
    conjunto distinto del que el usuario acaba de ver en pantalla.
    """
    if _lanzar_tracks is None:
        return Result(render.error("el reproductor no esta enganchado"), ok=False)

    mbids = intent.slots.get("mbids") or []
    etiqueta = intent.slots.get("etiqueta") or "eso"
    tracks = await queries.tracks_de_releases(mbids)
    if not tracks:
        return Result(render.sin_oferta_nada_que_poner(etiqueta), ok=False)

    resp = await _lanzar_tracks(tracks, etiqueta.capitalize(), st.room_id)
    st.tocar(last_playlist_id=str(resp.get("playlist_id") or "") or None)
    return Result(render.confirmado(resp, etiqueta, len(tracks)),
                  data={"count": len(tracks)}, actions=["playlist"])


async def _confirmar(intent: Intent, st: SessionState) -> Result:
    """"dale" es ambiguo y se resuelve por estado, no por patron.

    Con una oferta vigente confirma esa accion; sin oferta es un "seguí"
    —que es lo que significa "dale" a secas frente a un reproductor pausado.
    """
    oferta = st.tomar_oferta()
    if oferta is None:
        return await _play(intent, st)
    return await ejecutar(
        Intent(name=oferta.intent,
               slots={**oferta.slots, "etiqueta": oferta.etiqueta},
               confidence=1.0, stage=intent.stage), st)


async def _rechazar(intent, st: SessionState) -> Result:
    if st.tomar_oferta() is None:
        return Result(render.nada_que_confirmar(), ok=False)
    return Result(render.rechazado())


async def _reproducir_historial(intent: Intent, st: SessionState) -> Result:
    """"Poné algo que haya escuchado hoy" — el caso que motivo el H2.

    No pasa por el curador ni por la resolucion en YouTube: son tracks que ya
    sonaron, con `recording_mbid` ya resuelto. En un Pi 3B eso es la
    diferencia entre arrancar al instante y esperar una descarga por tema.
    """
    if _lanzar_tracks is None:
        return Result(render.error("el reproductor no esta enganchado"), ok=False)

    v = queries.ventana(intent.slots.get("cuando", ""))
    tracks = await queries.tracks_del_periodo(v)
    if not tracks:
        return Result(render.historial_vacio_para_reproducir(v.etiqueta), ok=False)

    resp = await _lanzar_tracks(tracks, f"De nuevo: {v.etiqueta}", st.room_id)
    st.tocar(last_playlist_id=str(resp.get("playlist_id") or "") or None)
    return Result(render.reproducir_historial(resp, v.etiqueta, len(tracks)),
                  data={"count": len(tracks)}, actions=["playlist"])



# ============================================================== H4: objetivos
#
# Un sesgo, no una cuota. Si el curador arma playlists peores para cumplir una
# metrica, se saltean, y los skips envenenan la senal que el Bloque B recien
# arreglo.

DEFAULTS_OBJ = {
    "coleccion":      {"target": 0.4},
    "descubrimiento": {"target": 5},
    "profundidad":    {"target": 0.5},
    "genero":         {"target": 20},
}


async def _estado_objetivos(intent, st: SessionState) -> Result:
    estados = await goals.estado(st.room_id)
    atrasado = next((e for e in estados
                     if e.get("suficiente") and not e.get("cumplido")), None)
    if atrasado:
        st.ofrecer("reproducir_objetivo", "algo para el objetivo")
    return Result(render.objetivos(estados) +
                  (render.OFRECER if atrasado else ""),
                  data={"objetivos": len(estados)})


async def _set_objetivo(intent: Intent, st: SessionState, kind: str) -> Result:
    spec = dict(DEFAULTS_OBJ.get(kind, {}))
    n = intent.slots.get("n")
    if n:
        # Los ratios se declaran en porcentaje y se guardan en 0..1.
        spec["target"] = (n / 100) if kind in ("coleccion", "profundidad") else n
    if kind == "genero":
        g = (intent.slots.get("genero") or "").strip().lower()
        if not g:
            return Result(render.no_entendido(), ok=False)
        spec["genero"], spec["tags"] = g, [g]

    goal = await goals.declarar(kind, spec, room_id=st.room_id)
    e = await goals.progreso(goal)

    # "quiero escuchar mas jazz" puede ser un objetivo o un pedido para ahora.
    # No hace falta elegir: se declara y se ofrece ponerlo.
    ofrecer = not e.get("cumplido")
    if ofrecer:
        st.ofrecer("reproducir_objetivo", "algo para ese objetivo")
    return Result(render.objetivo_declarado(kind, e)
                  + (render.OFRECER if ofrecer else ""),
                  data={"kind": kind})


async def _obj_coleccion(i, st):     return await _set_objetivo(i, st, "coleccion")
async def _obj_descubrimiento(i, st): return await _set_objetivo(i, st, "descubrimiento")
async def _obj_genero(i, st):        return await _set_objetivo(i, st, "genero")
async def _obj_profundidad(i, st):   return await _set_objetivo(i, st, "profundidad")


ALIAS_OBJ = {
    "coleccion": "coleccion", "vinilo": "coleccion", "vinilos": "coleccion",
    "el estante": "coleccion", "mis discos": "coleccion",
    "descubrimiento": "descubrimiento", "artistas nuevos": "descubrimiento",
    "descubrir": "descubrimiento",
    "profundidad": "profundidad", "albumes enteros": "profundidad",
    "discos enteros": "profundidad",
}


async def _borrar_objetivo(intent: Intent, st: SessionState) -> Result:
    que = (intent.slots.get("que") or "").strip().lower()
    kind = ALIAS_OBJ.get(que)
    if kind is None:
        # Cualquier otra cosa se interpreta como el genero declarado.
        activos = await goals.activos(st.room_id)
        gen = next((g for g in activos if g["kind"] == "genero"), None)
        kind = "genero" if gen else None
    if kind is None:
        return Result(render.objetivo_borrado(que, 0), ok=False)
    n = await goals.borrar(kind, st.room_id)
    return Result(render.objetivo_borrado(que, n))


async def _reproducir_coleccion(intent, st: SessionState) -> Result:
    """Discos del estante que no sonaron hace rato. Cero tokens: son MBIDs
    concretos de `ephemerides.weight = 1`, no hay nada que curar."""
    if _lanzar_tracks is None:
        return Result(render.error("el reproductor no esta enganchado"), ok=False)

    tracks = await queries.tracks_para_objetivo("coleccion")
    if not tracks:
        est = await queries.estado_coleccion()
        return Result(render.coleccion_vacia(est)
                      or render.sin_material_objetivo("colección en vinilo"),
                      ok=False)

    resp = await _lanzar_tracks(tracks, "De tu colección", st.room_id)
    st.tocar(last_playlist_id=str(resp.get("playlist_id") or "") or None)
    return Result(render.coleccion(resp, len(tracks)),
                  data={"count": len(tracks)}, actions=["playlist"])


async def _coleccion_de_artista(intent: Intent, st: SessionState) -> Result:
    """Un artista, pero solo lo que esta en el estante."""
    if _lanzar_tracks is None:
        return Result(render.error("el reproductor no esta enganchado"), ok=False)

    a, nombre = await _con_artista(intent, st)
    if not nombre:
        return Result(render.no_entendido(), ok=False)
    if not a:
        return Result(render.sin_artista(nombre), ok=False)

    tracks = await queries.coleccion_de_artista(a["mbid"])
    if not tracks:
        return Result(render.sin_artista_en_coleccion(a["name"]), ok=False)

    resp = await _lanzar_tracks(tracks, f"{a['name']} (tu colección)", st.room_id)
    st.tocar(last_playlist_id=str(resp.get("playlist_id") or "") or None)
    return Result(render.coleccion_artista(tracks, a["name"]),
                  data={"count": len(tracks)}, actions=["playlist"])


async def _reproducir_disco_coleccion(intent, st: SessionState) -> Result:
    """Un album entero de tu coleccion, en orden.

    Cuando alguien pone un vinilo lo pone entero; eso es lo que distingue
    tener discos de tener una playlist. Cero tokens: es un SELECT.
    """
    if _lanzar_tracks is None:
        return Result(render.error("el reproductor no esta enganchado"), ok=False)

    tracks = await queries.disco_de_coleccion()
    if not tracks:
        est = await queries.estado_coleccion()
        return Result(render.coleccion_vacia(est)
                      or render.sin_disco_coleccion(), ok=False)

    album = tracks[0].get("album") or "un disco"
    resp = await _lanzar_tracks(tracks, album, st.room_id)
    st.tocar(last_playlist_id=str(resp.get("playlist_id") or "") or None,
             last_artist=tracks[0].get("artist"))
    return Result(render.disco_coleccion(tracks), data={"count": len(tracks)},
                  actions=["playlist"])


async def _reproducir_objetivo(intent: Intent, st: SessionState) -> Result:
    """La playlist que mas mueve el objetivo mas atrasado. Cero tokens."""
    if _lanzar_tracks is None:
        return Result(render.error("el reproductor no esta enganchado"), ok=False)

    e = await goals.mas_atrasado(st.room_id)
    if not e:
        return Result(render.sin_objetivo_atrasado(), ok=False)

    tracks = await queries.tracks_para_objetivo(e["kind"], e.get("spec"))
    nombre = render.ETIQUETA_OBJ.get(e["kind"], e["kind"])
    if not tracks:
        detalle = (render.coleccion_vacia(await queries.estado_coleccion())
                   if e["kind"] == "coleccion" else None)
        return Result(detalle or render.sin_material_objetivo(nombre), ok=False)

    resp = await _lanzar_tracks(tracks, f"Objetivo: {nombre}", st.room_id)
    st.tocar(last_playlist_id=str(resp.get("playlist_id") or "") or None)
    return Result(render.objetivo_playlist(resp, e, len(tracks)),
                  data={"count": len(tracks), "kind": e["kind"]},
                  actions=["playlist"])


EJECUTORES: dict[str, Callable[[Intent, SessionState], Awaitable[Result]]] = {
    "control_play": _play,
    "control_pause": _pause,
    "control_next": _next,
    "control_prev": _prev,
    "control_stop": _stop,
    "control_replay": _replay,
    "control_vol_up": _vol_up,
    "control_vol_down": _vol_down,
    "control_vol_set": _vol_set,
    "estado_actual": _estado_actual,
    "estado_cola": _estado_cola,
    "playlist": _playlist,
    "saludo": _saludo,
    "ayuda": _ayuda,
    "repreguntar": _repreguntar,
    # --- H2 ---
    "historial_periodo": _historial_periodo,
    "historial_artista": _historial_artista,
    "top_escuchados": _top_escuchados,
    "salteados": _salteados,
    "nunca_escuchado": _nunca_escuchado,
    "discografia": _discografia,
    "relaciones": _relaciones,
    "efemerides_hoy": _efemerides_hoy,
    "reproducir_historial": _reproducir_historial,
    "reproducir_releases": _reproducir_releases,
    "confirmar": _confirmar,
    # --- H4 ---
    "estado_objetivos": _estado_objetivos,
    "set_objetivo_coleccion": _obj_coleccion,
    "set_objetivo_descubrimiento": _obj_descubrimiento,
    "set_objetivo_genero": _obj_genero,
    "set_objetivo_profundidad": _obj_profundidad,
    "borrar_objetivo": _borrar_objetivo,
    "reproducir_objetivo": _reproducir_objetivo,
    "reproducir_coleccion": _reproducir_coleccion,
    "reproducir_disco_coleccion": _reproducir_disco_coleccion,
    "coleccion_de_artista": _coleccion_de_artista,
    "rechazar": _rechazar,
    "no_entendido": _no_entendido,
}


async def ejecutar(intent: Intent, st: SessionState) -> Result:
    """Nunca levanta: un reproductor que se cae porque mpv no contesta es peor
    que uno que dice que no pudo."""
    fn = EJECUTORES.get(intent.name)
    if fn is None:
        logger.warning("intent sin ejecutor: %s", intent.name)
        return Result(render.no_entendido(), ok=False)
    try:
        res = await fn(intent, st)
        st.tocar(last_intent=intent.name)
        return res
    except MPVError as e:
        logger.warning("mpv no respondio en %s: %s", intent.name, e)
        return Result(render.MPV_CAIDO, ok=False)
    except Exception:
        logger.exception("fallo el ejecutor de %s", intent.name)
        return Result(render.error("error interno"), ok=False)
