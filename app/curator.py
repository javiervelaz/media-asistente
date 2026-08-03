"""Curador con tool calling: Claude consulta el grafo antes de armar la playlist"""
import json
import logging

from anthropic import AsyncAnthropic

from app.config import settings
from app.tools import TOOL_IMPL

logger = logging.getLogger(__name__)

client = AsyncAnthropic(api_key=settings.anthropic_api_key)

TOOLS = [
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
                    "description": "2 salta a bandas de compañeros de banda. Ojo con el ruido.",
                },
            },
            "required": ["mbid"],
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
     "rationale": "por qué está y por qué en esta posición"}
  ]
}
Incluí recording_mbid y length_ms solo si los sacaste de get_recordings. Si no, dejalos en null."""

MAX_TOOL_RESULT = 12_000   # truncado para no inflar el contexto en cada turno


async def curate(prompt: str, n_tracks: int = 20, max_turns: int = 8) -> dict:
    mensajes = [{
        "role": "user",
        "content": f"{prompt}\n\nArmá una playlist de {n_tracks} tracks.",
    }]

    for turno in range(max_turns):
        resp = await client.messages.create(
            model=settings.curator_model,
            max_tokens=4096,
            system=SYSTEM,
            tools=TOOLS,
            messages=mensajes,
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
            try:
                out = await fn(**block.input) if fn else {"error": "tool desconocida"}
            except Exception as e:
                logger.exception("tool %s falló", block.name)
                out = {"error": str(e)}
            resultados.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(out, default=str)[:MAX_TOOL_RESULT],
            })
        mensajes.append({"role": "user", "content": resultados})

    raise RuntimeError(f"el curador no convergió en {max_turns} turnos")


def _parse(resp) -> dict:
    texto = "".join(b.text for b in resp.content if b.type == "text").strip()
    if texto.startswith("```"):
        texto = texto.split("```")[1]
        if texto.startswith("json"):
            texto = texto[4:]
        texto = texto.strip()

    data = json.loads(texto)
    if not data.get("tracks"):
        raise ValueError("el curador devolvió una playlist vacía")

    logger.info("playlist %r: %d tracks", data.get("title"), len(data["tracks"]))
    return data