"""Curador con tool calling: Claude consulta el grafo antes de armar la playlist"""
import json
import logging
from anthropic import AsyncAnthropic
from app.config import settings
from app.tools import TOOL_IMPL

logger = logging.getLogger(__name__)

client = AsyncAnthropic(api_key=settings.anthropic_api_key)

MAX_TOKENS = 8192
MAX_TOOL_RESULT = 4_000   # truncado para no inflar el contexto en cada turno
MIN_PLAYLIST = 8          # piso: por debajo, la cuota de libres cede
MAX_POR_ARTISTA = 2       # techo base; sube solo si no hay variedad disponible
RESERVA_NOTA = 200        # espacio para la nota de truncamiento


def _truncar(out) -> str:
    """Serializa un tool result sin cortarlo a mitad de token JSON.

    Cortar por caracteres le entrega al modelo un JSON invalido y sin marca
    de que falta algo: lo completa de memoria. Truncamos por elementos y
    decimos explicitamente cuantos quedaron afuera.
    """
    txt = json.dumps(out, default=str)
    if len(txt) <= MAX_TOOL_RESULT:
        return txt

    if isinstance(out, list):
        items = list(out)
        while items and len(json.dumps(items, default=str)) > MAX_TOOL_RESULT - RESERVA_NOTA:
            items.pop()
        logger.info("tool result truncado: %d de %d elementos", len(items), len(out))
        return json.dumps({
            "items": items,
            "truncado": True,
            "nota": (f"Se muestran {len(items)} de {len(out)} resultados. "
                     "Afina los filtros si necesitas ver el resto."),
        }, default=str)

    if isinstance(out, dict):
        for clave in ("conectados", "items", "tracks"):
            if isinstance(out.get(clave), list):
                recorte = dict(out)
                items = list(out[clave])
                while items and len(json.dumps({**recorte, clave: items}, default=str)) > MAX_TOOL_RESULT - RESERVA_NOTA:
                    items.pop()
                recorte[clave] = items
                recorte["truncado"] = True
                recorte["nota"] = (f"Se muestran {len(items)} de {len(out[clave])} "
                                   "resultados. Afina los filtros o pedi menos.")
                logger.info("tool result truncado: %d de %d en %r",
                            len(items), len(out[clave]), clave)
                return json.dumps(recorte, default=str)

    logger.warning("tool result no truncable por elementos (%d chars)", len(txt))
    return json.dumps({
        "truncado": True,
        "nota": "El resultado era demasiado grande. Pedi menos datos.",
        "preview": txt[:MAX_TOOL_RESULT - RESERVA_NOTA],
    })



TOOLS = [
    {
        "name": "search_artist",
        "description": (
            "Busca un artista por nombre. Si no está en la base local lo trae "
            "de MusicBrainz y lo incorpora. Devuelve mbid, país, años activos, "
            "tags y cuántos álbumes hay cargados. Usalo siempre primero para "
            "obtener el mbid de cualquier artista que menciones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "get_artist_graph",
        "description": (
            "Artistas conectados a uno dado: miembros de banda, side projects, "
            "colaboraciones y productores, con los años de cada vínculo. "
            "Los side projects aparecen como member_of_band: si dos bandas "
            "comparten un integrante, están conectadas. Es la herramienta para "
            "playlists genealógicas y para conexiones que no son obvias."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mbid": {"type": "string"},
                "rel_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["member_of_band", "collaboration",
                                 "producer", "supporting_musician"],
                    },
                    "description": "Si lo omitís se usan todos.",
                },
                "max_hops": {
                    "type": "integer",
                    "default": 1,
                    "description": (
                        "2 salta a las bandas de los compañeros de banda. "
                        "Más alcance, más ruido."
                    ),
                },
            },
            "required": ["mbid"],
        },
    },
    {
        "name": "query_releases",
        "description": (
            "Filtra álbumes de la base por artistas, país, rango de años o tags. "
            "Devuelve candidatos verificados con fecha de primera edición. "
            "weight indica procedencia: 1 = colección de vinilos del usuario, "
            "2 = canon histórico, 3 = descubierto por uso."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "artist_mbids": {"type": "array", "items": {"type": "string"}},
                "country": {"type": "string",
                            "description": "ISO 2 letras, ej: GB, AR, US"},
                "year_from": {"type": "integer"},
                "year_to": {"type": "integer"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "get_recordings",
        "description": (
            "Tracklist de un álbum: artista, y mbid, duración y disponibilidad "
            "de cada track. Si no está en la base la trae de MusicBrainz. "
            "SOLO podés incluir en la playlist tracks que hayas visto acá: "
            "el recording_mbid es lo que permite verificar que existen. "
            "`listo: true` significa que el track ya está resuelto y arranca "
            "al instante; a igualdad de criterio curatorial, preferilos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"release_mbid": {"type": "string"}},
            "required": ["release_mbid"],
        },
    },
    {
        "name": "get_play_history",
        "description": (
            "Qué se escuchó últimamente y qué se salteó. Usalo para no repetir "
            "lo de los últimos días y para calibrar el gusto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 30}},
        },
    },
]

SYSTEM = """Sos el curador musical de Charly, un sistema de audio doméstico en Córdoba, Argentina.

Tenés acceso a una base local de artistas, álbumes y relaciones entre músicos, construida sobre MusicBrainz. La base crece sola: si search_artist no encuentra un artista, lo busca en MusicBrainz y lo incorpora. Preguntá por cualquier artista que te sirva, esté o no cargado.

Usá las herramientas para descubrir, no confíes solo en tu memoria: los datos de la base están verificados y los tuyos no.

Método:
1. Resolvé con search_artist los artistas mencionados en el pedido y obtené sus mbid.
2. Explorá el grafo con get_artist_graph o filtrá con query_releases según lo que pida el usuario.
3. Si get_artist_graph marca artistas sin discografía cargada y alguno te interesa, traelo con search_artist.
4. Consultá get_play_history para no repetir lo de los últimos días.
5. Armá la playlist.

Si get_artist_graph devuelve pocos resultados o ninguno, no completes con artistas de memoria: probá con max_hops=2, o traé con search_artist a los integrantes de la banda y consultá el grafo desde ellos. La gracia está en las conexiones verificadas, no en las asociaciones obvias del género.

Criterios de curaduría:
- Una playlist tiene una tesis, no es una lista de hits. Buscá el ángulo no obvio.
- Máximo 2 tracks por artista, salvo que el pedido sea sobre un artista puntual.
- Arco: abrí con algo que ubique, construí, poné el pico en el último tercio, bajá al cierre.
- Como mucho un tercio de temas obvios. El resto tiene que aportar algo.
- Evitá compilados, versiones en vivo y remixes salvo pedido explícito.
- Priorizá álbumes con weight 1 (la colección del usuario) cuando encajen.

No hagas más de 10 llamadas a herramientas. Cuando tengas material suficiente, cerrá.

Al terminar respondé SOLO con JSON, sin markdown ni preámbulo:
{
  "title": "nombre corto de la playlist",
  "concept": "la tesis en una frase",
  "narration": "2-3 frases para leer en voz alta antes de arrancar. Español rioplatense, tono de quien sabe de música y no la hace larga.",
  "tracks": [
    {"artist": "...", "title": "...", "recording_mbid": null, "length_ms": null,
     "rationale": "una frase: por qué está y por qué en esta posición"}
  ]
}
Antes de armar la lista final, pasá get_recordings por TODOS los álbumes de los
que vayas a sacar tracks, y elegí únicamente entre los tracks que viste ahí.
El recording_mbid es lo que hace verificable un track: sin él no hay forma de
saber si existe, y YouTube siempre devuelve algo, así que un tema inventado no
falla —suena—. Si un álbum que te interesa no tiene tracklist cargada, traela:
para eso está la herramienta.

Una playlist de 14 tracks reales vale más que una de 20 con 6 inventados.
Incluí recording_mbid y length_ms tal como te los dio get_recordings.
Mantené cada rationale en una sola frase corta: la respuesta tiene que entrar completa."""

# System como lista de bloques, con breakpoint de cache
SYSTEM_BLOCKS = [{
    "type": "text",
    "text": SYSTEM,
    "cache_control": {"type": "ephemeral"},
}]

# Breakpoint al final de las tools: cachea todos los schemas
TOOLS[-1]["cache_control"] = {"type": "ephemeral"}

def _registrar_vistos(nombre: str, args: dict, out, vistos: dict) -> None:
    """Acumula los recordings que el modelo vio de verdad en un tool result.

    Es la unica fuente de verdad sobre que puede nombrar: YouTube siempre
    encuentra *algo*, asi que un track inventado no falla ruidosamente, suena.
    Sin este registro no hay forma de distinguirlo de uno real.
    """
    if nombre != "get_recordings" or not isinstance(out, dict):
        return
    artist = out.get("artist")
    artist_mbid = out.get("artist_mbid")
    for tr in out.get("tracks") or []:
        mbid = str(tr.get("mbid") or "").strip()
        if mbid:
            vistos[mbid] = {
                "artist": artist,
                "artist_mbid": artist_mbid,
                "title": tr.get("title"),
                "length_ms": tr.get("length_ms"),
            }


def _mover_breakpoint(mensajes: list) -> None:
    """Un solo breakpoint móvil sobre el último tool_result.
    Cachea todo el prefijo acumulado de la conversación."""
    for m in mensajes:
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
    if mensajes and isinstance(mensajes[-1].get("content"), list):
        ultimo = mensajes[-1]["content"][-1]
        if isinstance(ultimo, dict):
            ultimo["cache_control"] = {"type": "ephemeral"}


async def curate(prompt: str, n_tracks: int = 20, max_turns: int = 8) -> dict:
    mensajes = [{
        "role": "user",
        "content": f"{prompt}\n\nArmá una playlist de {n_tracks} tracks.",
    }]
    uso = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    vistos: dict[str, dict] = {}   # recording_mbid -> lo que el modelo vio

    for turno in range(max_turns):
        resp = await client.messages.create(
            model=settings.curator_model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_BLOCKS,
            tools=TOOLS,
            messages=mensajes,
        )

        u = resp.usage
        uso["in"]          += u.input_tokens
        uso["out"]         += u.output_tokens
        uso["cache_read"]  += getattr(u, "cache_read_input_tokens", 0) or 0
        uso["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        mensajes.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            logger.info("tokens — in:%(in)d out:%(out)d "
                        "cache_r:%(cache_read)d cache_w:%(cache_write)d", uso)
            data = _parse(resp, vistos, n_tracks)
            # El harness lo escribe en turn_log: sin esto, el costo
            # de un turno de curacion solo existe en los logs.
            data["usage"] = uso
            return data

        resultados = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            fn = TOOL_IMPL.get(block.name)
            logger.info("tool %s(%s)", block.name, block.input)
            try:
                out = await fn(**block.input) if fn else {"error": "tool desconocida"}
            except Exception as e:
                logger.exception("tool %s falló", block.name)
                out = {"error": str(e)}
            _registrar_vistos(block.name, block.input, out, vistos)
            resultados.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": _truncar(out),
            })

        mensajes.append({"role": "user", "content": resultados})
        _mover_breakpoint(mensajes)

    logger.warning("no convergió — tokens: %s", uso)
    raise RuntimeError(f"el curador no convergió en {max_turns} turnos")


def _parse(resp, vistos: dict | None = None, n_tracks: int = 20) -> dict:
    """Extrae y valida el JSON final. Tolera preámbulo y code fences."""
    texto = "".join(b.text for b in resp.content if b.type == "text").strip()

    if not texto:
        tipos = [b.type for b in resp.content]
        raise ValueError(
            f"el curador no devolvió texto "
            f"(stop_reason={resp.stop_reason}, bloques={tipos})"
        )

    if texto.startswith("```"):
        partes = texto.split("```")
        if len(partes) > 1:
            texto = partes[1]
            if texto.startswith("json"):
                texto = texto[4:]
            texto = texto.strip()

    # Si quedó preámbulo, agarramos el objeto por llaves balanceadas
    if not texto.startswith("{"):
        inicio = texto.find("{")
        if inicio == -1:
            raise ValueError(f"no hay JSON en la respuesta: {texto[:300]}")

        nivel, fin, en_string, escape = 0, None, False, False
        for i, c in enumerate(texto[inicio:], inicio):
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
            elif c == '"':
                en_string = not en_string
            elif not en_string:
                if c == "{":
                    nivel += 1
                elif c == "}":
                    nivel -= 1
                    if nivel == 0:
                        fin = i + 1
                        break

        if fin is None:
            raise ValueError(f"JSON incompleto: {texto[inicio:inicio + 300]}")
        texto = texto[inicio:fin]

    try:
        data = json.loads(texto)
    except json.JSONDecodeError as e:
        logger.error("JSON inválido del curador:\n%s", texto[:1500])
        raise ValueError(f"el curador devolvió JSON inválido: {e}") from e

    tracks = data.get("tracks")
    if not tracks or not isinstance(tracks, list):
        raise ValueError("el curador devolvió una playlist vacía o mal formada")

    # Descartamos tracks incompletos en vez de romper toda la playlist
    validos = [t for t in tracks
               if isinstance(t, dict) and t.get("artist") and t.get("title")]
    if not validos:
        raise ValueError("ningún track tiene artist y title")
    if len(validos) < len(tracks):
        logger.warning("descartados %d tracks mal formados", len(tracks) - len(validos))

    validos, metricas = _clasificar(validos, vistos or {}, n_tracks)
    data["tracks"] = validos
    data["metrics"] = metricas

    logger.info("playlist %r: %d tracks (%d verificados, %d libres)",
                data.get("title"), len(validos),
                metricas["verificados"], metricas["libres"])
    return data


def _clasificar(tracks: list[dict], vistos: dict,
                n_tracks: int = 20) -> tuple[list[dict], dict]:
    """Separa lo que el modelo vio en un tool result de lo que puso de memoria.

    Los `libres` no se descartan de entrada —a veces el modelo acierta y la
    base esta incompleta— pero se acotan por cuota y van al final: una
    alucinacion en el track 18 molesta mucho menos que en el track 2.
    """
    verificados, libres = [], []
    for t in tracks:
        mbid = str(t.get("recording_mbid") or "").strip()
        ref = vistos.get(mbid) if mbid else None
        if ref:
            t["origen"] = "verificado"
            # El modelo a veces transcribe mal el titulo: mandamos el de la base
            t["title"] = ref["title"] or t["title"]
            t["artist"] = ref["artist"] or t["artist"]
            t["artist_mbid"] = ref.get("artist_mbid")
            if t.get("length_ms") is None:
                t["length_ms"] = ref["length_ms"]
            verificados.append(t)
        else:
            t["origen"] = "libre"
            if mbid:
                logger.warning("mbid que no salio de ningun tool result: %s — %s",
                               t.get("artist"), t.get("title"))
                t["recording_mbid"] = None   # no ensuciamos track_resolutions
            libres.append(t)

    if not vistos:
        # El modelo nunca llego a un get_recordings: la verificacion no estuvo
        # disponible, no es que haya inventado todo. Recortar aca dejaria la
        # playlist en un track por una falla de tools. Medimos y dejamos pasar.
        logger.warning("ningun get_recordings en la sesion: no aplico cuota, "
                       "%d tracks van sin verificar", len(libres))
        recortados = libres
    else:
        cupo = settings.curator_max_libres
        permitidos = len(tracks) if cupo >= 1 else int(len(tracks) * cupo)
        recortados = libres[:permitidos]

        # Piso: un cover ocasional molesta menos que una cola que se queda seca.
        # No aplica con cupo 0: si el operador pidio modo estricto, es estricto.
        # El piso nunca puede ser mayor que el material disponible.
        piso = min(MIN_PLAYLIST, len(tracks)) if cupo > 0 else 0
        faltan = piso - (len(verificados) + len(recortados))
        if faltan > 0 and len(libres) > len(recortados):
            extra = libres[len(recortados):len(recortados) + faltan]
            logger.warning("piso de %d tracks: dejo entrar %d libres de mas",
                           piso, len(extra))
            recortados += extra

        for t in libres[len(recortados):]:
            logger.info("fuera por cuota (sin respaldo): %s — %s",
                        t.get("artist"), t.get("title"))

    salida = verificados + recortados
    n_antes = len(salida)
    salida = _acotar_densidad(salida, vistos, n_tracks)

    return salida, {
        "verificados": sum(1 for t in salida if t["origen"] == "verificado"),
        "libres": sum(1 for t in salida if t["origen"] == "libre"),
        "descartados_por_cuota": len(libres) - len(recortados),
        "descartados_por_densidad": n_antes - len(salida),
        "vistos_en_tools": len(vistos),
    }


def _clave_artista(t: dict) -> str:
    """artist_mbid si lo hay; si no, el nombre normalizado.

    El mbid evita que 'Los Palmeras' y 'los palmeras' cuenten como dos.
    """
    mbid = t.get("artist_mbid")
    if mbid:
        return str(mbid)
    return (t.get("artist") or "").strip().lower()


def _acotar_densidad(tracks: list[dict], vistos: dict,
                     n_tracks: int) -> list[dict]:
    """Impone el maximo de tracks por artista, respetando la secuencia.

    El prompt ya lo pide, pero es una regla que el modelo cumple solo cuando
    hay material variado: en un dominio con pocos artistas hidratados colapsa
    al que tiene mas discos. Y es un fallo mudo, porque esos tracks de mas
    estan verificados: el ratio da 100% y la playlist igual esta mal.

    El techo se adapta a la variedad que el propio curador encontro. Si vio 10
    artistas y le pidieron 12 tracks, 2 por artista alcanza de sobra. Si vio
    uno solo —un pedido monografico, "ponete algo de Sumo"— acotar a 2 seria
    romper justo lo que pidieron, asi que el techo sube.
    """
    if not vistos:
        return tracks

    artistas_vistos = len({v.get("artist_mbid") or v.get("artist")
                           for v in vistos.values()
                           if v.get("artist_mbid") or v.get("artist")})
    if artistas_vistos <= 1:
        return tracks     # monografico: el tope no tiene sentido

    # techo = lo que haria falta para llenar la playlist repartiendo parejo
    necesario = -(-max(n_tracks, 1) // artistas_vistos)   # ceil
    techo = max(MAX_POR_ARTISTA, necesario)

    cuenta: dict[str, int] = {}
    salida, fuera = [], []
    for t in tracks:
        k = _clave_artista(t)
        if cuenta.get(k, 0) >= techo:
            fuera.append(t)
            continue
        cuenta[k] = cuenta.get(k, 0) + 1
        salida.append(t)

    if fuera:
        logger.info("densidad: techo %d por artista (%d artistas vistos), "
                    "%d tracks afuera", techo, artistas_vistos, len(fuera))
        for t in fuera:
            logger.debug("  fuera por densidad: %s — %s",
                         t.get("artist"), t.get("title"))
    return salida