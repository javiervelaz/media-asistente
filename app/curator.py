"""Curador con tool calling: Claude consulta el grafo antes de armar la playlist"""
import json
import logging

from anthropic import AsyncAnthropic

from app.config import settings
from app.tools import TOOL_IMPL

logger = logging.getLogger(__name__)

client = AsyncAnthropic(api_key=settings.anthropic_api_key)

MAX_TOKENS = 8192
MAX_TOOL_RESULT = 12_000   # truncado para no inflar el contexto en cada turno

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
            "Tracklist de un álbum, con mbid y duración de cada track. "
            "Si no está en la base la trae de MusicBrainz. Incluir el "
            "recording_mbid y el length_ms en la playlist final mejora mucho "
            "la precisión de la búsqueda en YouTube."
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

No hagas más de 6 llamadas a herramientas. Cuando tengas material suficiente, cerrá.

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
Incluí recording_mbid y length_ms solo si los sacaste de get_recordings. Si no, dejalos en null.
Mantené cada rationale en una sola frase corta: la respuesta tiene que entrar completa."""


async def curate(prompt: str, n_tracks: int = 20, max_turns: int = 8) -> dict:
    """Corre el loop de tool calling y devuelve la playlist parseada."""
    mensajes: list[dict] = [{
        "role": "user",
        "content": f"{prompt}\n\nArmá una playlist de {n_tracks} tracks.",
    }]

    for turno in range(max_turns):
        try:
            resp = await client.messages.create(
                model=settings.curator_model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
                tools=TOOLS,
                messages=mensajes,
            )
        except Exception as e:
            logger.exception("error llamando a la API en el turno %d", turno)
            raise RuntimeError(f"API de Anthropic: {e}") from e

        logger.info(
            "turno %d: stop_reason=%s, in=%d out=%d tokens",
            turno, resp.stop_reason,
            resp.usage.input_tokens, resp.usage.output_tokens,
        )

        if resp.stop_reason == "max_tokens":
            raise RuntimeError(
                f"respuesta truncada en {resp.usage.output_tokens} tokens. "
                f"Subí MAX_TOKENS (actual: {MAX_TOKENS}) o bajá n_tracks."
            )

        mensajes.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            return _parse(resp)

        resultados = []
        for block in resp.content:
            if block.type != "tool_use":
                continue

            fn = TOOL_IMPL.get(block.name)
            logger.info("tool %s(%s)", block.name, block.input)

            if fn is None:
                out = {"error": f"tool desconocida: {block.name}"}
            else:
                try:
                    out = await fn(**block.input)
                except TypeError as e:
                    logger.error("argumentos inválidos para %s: %s", block.name, e)
                    out = {"error": f"argumentos inválidos: {e}"}
                except Exception as e:
                    logger.exception("tool %s falló", block.name)
                    out = {"error": str(e)}

            payload = json.dumps(out, default=str, ensure_ascii=False)
            if len(payload) > MAX_TOOL_RESULT:
                payload = payload[:MAX_TOOL_RESULT] + '..."[truncado]"'
                logger.warning("resultado de %s truncado", block.name)

            resultados.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": payload,
            })

        mensajes.append({"role": "user", "content": resultados})

    raise RuntimeError(f"el curador no convergió en {max_turns} turnos")


def _parse(resp) -> dict:
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
    data["tracks"] = validos

    logger.info("playlist %r: %d tracks", data.get("title"), len(validos))
    return data