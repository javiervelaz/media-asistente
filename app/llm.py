"""Cliente Claude: genera playlists desde un prompt en lenguaje natural"""
import json
import logging
from typing import Any

from anthropic import Anthropic

from app.config import settings

logger = logging.getLogger(__name__)

client = Anthropic(api_key=settings.anthropic_api_key)

SYSTEM_PROMPT = """Sos un curador musical experto. El usuario te va a pedir una playlist en lenguaje natural y vos respondés ÚNICAMENTE con un objeto JSON válido, sin texto antes ni después, sin markdown, sin code fences.

El JSON tiene esta forma exacta:
{
  "title": "string corto que describa la playlist",
  "tracks": [
    {"artist": "Nombre del artista", "title": "Nombre del tema"},
    ...
  ]
}

Reglas:
- Devolvé entre 15 y 30 tracks salvo que el usuario pida un número específico.
- Mezclá hits conocidos con joyas menos obvias para que la playlist sea interesante.
- Variá los artistas: máximo 2 tracks del mismo artista en la playlist.
- Si el pedido es ambiguo, interpretalo de forma razonable y procedé.
- Nunca inventes canciones que no existan. Mejor poner menos tracks que inventar.
- Tu output completo debe ser parseable con json.loads(). Sin excepciones."""


def generate_playlist(prompt: str) -> dict[str, Any]:
    """Recibe un prompt y devuelve {title, tracks: [{artist, title}, ...]}"""
    logger.info(f"Generando playlist para: {prompt!r}")

    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    logger.debug(f"Claude raw output: {raw}")

    # Defensivo: si Claude se mandó una macana con markdown, lo limpiamos
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Claude devolvió JSON inválido: {raw}")
        raise ValueError(f"LLM returned invalid JSON: {e}") from e

    if "tracks" not in data or not isinstance(data["tracks"], list):
        raise ValueError("LLM response missing 'tracks' array")

    logger.info(f"Playlist '{data.get('title', 'sin título')}' con {len(data['tracks'])} tracks")
    return data