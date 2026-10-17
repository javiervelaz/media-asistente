"""Spike: generar un tema mezclando el estilo de artistas del grafo.

Pregunta que responde: ¿la fusion que sale de Lyria suena a algo que quiero
escuchar? Si la respuesta es no, nada de esto entra al harness.

Flujo:
  1. resolve_artist() por cada nombre (local -> MusicBrainz -> hidrata).
  2. Del grafo: tags, pais, y la DECADA FUERTE (donde mas discos de estudio
     tiene), que dice mas del sonido que begin_year.
  3. Claude traduce eso a un prompt descriptivo SIN nombres de artistas.
     Lyria bloquea pedidos de voces de artistas concretos; un nombre en el
     prompt es un rechazo o una generacion generica pagada igual.
  4. Guarda local: si el prompt menciona un nombre, se reintenta una vez y
     si vuelve a pasar, se aborta antes de gastar en Lyria.
  5. Lyria via Interactions API (REST, httpx: ya es dependencia).
  6. mp3 + sidecar json en ./mezclas/; --play lo manda a mpv.

Costo por corrida: clip 30s US$0.04 (default), tema completo US$0.08.

Uso:
  python -m scripts.spike_mezcla "The Beatles" "Aerosmith"
  python -m scripts.spike_mezcla "Soda Stereo" "The Cure" --completa --play
  python -m scripts.spike_mezcla "Pink Floyd" "Kraftwerk" --instrumental
  python -m scripts.spike_mezcla "The Beatles" "Aerosmith" --dry-run

Requiere GEMINI_API_KEY en el .env (no se agrega a Settings: es un spike).
"""
import argparse
import asyncio
import base64
import json
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

from app.config import settings
from app.db import close_pool, fetch
from app.hydrator import resolve_artist

load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("spike_mezcla")

LYRIA_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
MODELO_CLIP = "lyria-3-clip-preview"
MODELO_COMPLETO = "lyria-3.5"
COSTO_USD = {MODELO_CLIP: 0.04, MODELO_COMPLETO: 0.08}
TZ = ZoneInfo(settings.harness_tz)
SALIDA = Path(os.environ.get("MEZCLAS_DIR", "mezclas"))

# Tags de MusicBrainz que no describen sonido. Mandarlos al LLM es ruido que
# despues aparece en el prompt ("british", "seen live").
TAGS_RUIDO = {"seen live", "british", "english", "american", "uk", "usa",
              "argentina", "argentine", "male vocalists", "female vocalists",
              "favorites", "favourite", "awesome", "classic", "europe",
              "liverpool", "film soundtrack", "soundtrack"}

# Paises con voz en castellano. Decidir el idioma en codigo y no dejarselo a
# Haiku: el prompt sale en ingles, y sin una instruccion explicita Lyria canta
# en ingles aunque mezcles Spinetta con Charly.
PAISES_CASTELLANO = {"AR", "ES", "MX", "CL", "UY", "CO", "PE", "VE", "CU",
                     "BO", "PY", "EC", "PR", "DO", "GT", "CR", "PA", "SV",
                     "HN", "NI"}


# === 1-2: perfil desde el grafo ===============================================

SQL_ANIOS_ESTUDIO = """
SELECT EXTRACT(YEAR FROM first_release_date)::int AS anio
FROM releases
WHERE artist_mbid = $1
  AND primary_type = 'Album'
  AND first_release_date IS NOT NULL
  -- secondary_types puede venir NULL de la migracion de ephemerides, y
  -- `x = ANY(NULL)` es NULL: sin el COALESCE se filtraria todo.
  AND NOT (COALESCE(secondary_types, '{}') && ARRAY['Compilation', 'Live'])
ORDER BY 1
"""


def decada_fuerte(anios: list[int]) -> str | None:
    """La decada con mas discos de estudio. Empate: la mas temprana, que suele
    ser la que definio el sonido."""
    if not anios:
        return None
    conteo = Counter((a // 10) * 10 for a in anios)
    mejor = max(conteo.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    return f"{mejor}s"


def tags_utiles(tags: list[str] | None, maximo: int = 8) -> list[str]:
    return [t for t in (tags or []) if t.lower() not in TAGS_RUIDO][:maximo]


async def perfil(nombre: str) -> dict:
    a = await resolve_artist(nombre)
    if not a:
        raise LookupError(f"'{nombre}' no aparece ni en el grafo ni en "
                          "MusicBrainz. Probá con el nombre original.")
    anios = [r["anio"] for r in await fetch(SQL_ANIOS_ESTUDIO, a["mbid"])]
    p = {
        "nombre": a["name"],
        "mbid": str(a["mbid"]),
        "pais": a.get("country"),
        "activo": [a.get("begin_year"), a.get("end_year")],
        "decada_fuerte": decada_fuerte(anios),
        "discos_estudio": len(anios),
        "tags": tags_utiles(a.get("tags")),
    }
    if not p["tags"]:
        logger.warning("%s no tiene tags utiles: la mezcla va a depender de "
                       "lo que Claude sepa del artista", p["nombre"])
    return p


# === 3-4: traduccion a descriptores =========================================

SYSTEM_TRADUCTOR = """Sos productor musical. Te paso perfiles de artistas (tags de MusicBrainz, decada fuerte, pais) y tenes que escribir UN prompt para un generador de musica que fusione esos estilos.

Reglas duras:
- PROHIBIDO nombrar artistas, bandas, integrantes, discos o canciones. Ni en el prompt ni en la letra. El generador rechaza esos pedidos.
- Describi el sonido con lo que lo produce: genero y era, instrumentacion concreta, tipo de guitarra/teclado, groove y tempo (BPM aproximado), tipo de voz (registro, textura, actitud), armonias, produccion (analogica, cinta, reverb, etc.).
- La fusion tiene que tomar algo reconocible de CADA perfil, no promediarlos en un genero generico.
- Prompt en ingles, maximo 90 palabras, una sola linea.
- Si te piden instrumental, termina el prompt con "Instrumental, no vocals." e ignora idioma_voz.
- Si no es instrumental, el prompt TIENE que indicar el idioma de la voz segun idioma_voz: "Vocals and lyrics in Spanish." o "Vocals and lyrics in English.". Si idioma_voz es "es", cualquier letra que escribas va en castellano rioplatense.

Respondes SOLO con JSON valido, sin markdown:
{"prompt": "...", "titulo": "titulo corto en castellano para el archivo"}"""


def nombres_prohibidos(perfiles: list[dict]) -> list[str]:
    """Nombres a buscar en el prompt. 'The Beatles' tambien prohibe 'Beatles'."""
    out = set()
    for p in perfiles:
        n = p["nombre"].lower().strip()
        out.add(n)
        if n.startswith("the "):
            out.add(n[4:])
    return sorted(out)


def menciona_nombre(texto: str, prohibidos: list[str]) -> str | None:
    t = texto.lower()
    for n in prohibidos:
        if re.search(rf"\b{re.escape(n)}\b", t):
            return n
    return None


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].removeprefix("json").strip()
    data = json.loads(raw)
    if not isinstance(data.get("prompt"), str) or not data["prompt"].strip():
        raise ValueError("el traductor no devolvio 'prompt'")
    return data


def idioma_voz(perfiles: list[dict], forzado: str = "auto") -> str:
    """'es' si algun artista es de un pais hispanohablante, si no 'en'.

    Basta con uno: si mezclas Charly con Depeche Mode lo que queres es synth-pop
    cantado en castellano, no un tema en ingles con un teclado ochentoso.
    """
    if forzado in ("es", "en"):
        return forzado
    paises = {(p.get("pais") or "").upper() for p in perfiles}
    return "es" if paises & PAISES_CASTELLANO else "en"


def traducir(perfiles: list[dict], instrumental: bool,
             idioma: str = "en") -> dict:
    client = Anthropic(api_key=settings.anthropic_api_key)
    prohibidos = nombres_prohibidos(perfiles)
    pedido = json.dumps({"perfiles": perfiles, "instrumental": instrumental,
                         "idioma_voz": idioma}, ensure_ascii=False)
    mensajes = [{"role": "user", "content": pedido}]

    for intento in (1, 2):
        resp = client.messages.create(
            model=settings.claude_model, max_tokens=400,
            system=SYSTEM_TRADUCTOR, messages=mensajes)
        raw = resp.content[0].text
        data = _parse_json(raw)
        colado = menciona_nombre(data["prompt"], prohibidos)
        if not colado:
            return data
        logger.warning("intento %d: el prompt menciona '%s', reintento",
                       intento, colado)
        mensajes += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": f"El prompt menciona '{colado}'. "
                                        "Reescribilo sin ningun nombre propio."},
        ]
    raise ValueError("el traductor insiste en nombrar artistas; aborto antes "
                     "de pagar una generacion que Lyria va a rechazar")


# === 5: Lyria ===============================================================

class LyriaRechazo(Exception):
    """El filtro de Lyria bloqueo el pedido. Distinto de un error de red."""


def extraer_salida(data: dict) -> tuple[bytes, str]:
    """steps[type=model_output].content[] -> (audio, texto). La respuesta trae
    la letra/estructura en bloques de texto ademas del audio."""
    audio, textos = None, []
    for step in data.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for bloque in step.get("content", []):
            if bloque.get("type") == "audio" and bloque.get("data"):
                audio = base64.b64decode(bloque["data"])
            elif bloque.get("type") == "text" and bloque.get("text"):
                textos.append(bloque["text"])
    if audio is None:
        # 200 sin audio: en la practica es un bloqueo silencioso o un cambio
        # de contrato de una API en preview. Guardamos la respuesta para ver.
        raise LyriaRechazo("respuesta sin audio: " +
                           json.dumps(data, ensure_ascii=False)[:500])
    return audio, "\n".join(textos)


async def generar(prompt: str, modelo: str) -> tuple[bytes, str]:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("falta GEMINI_API_KEY en el .env")

    # Un tema completo tarda; el default de httpx (5s) corta siempre.
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as c:
        r = await c.post(LYRIA_URL,
                         headers={"x-goog-api-key": key},
                         json={"model": modelo, "input": prompt})

    if r.status_code in (400, 403) and "safety" in r.text.lower():
        raise LyriaRechazo(r.text[:500])
    if r.status_code >= 400:
        raise RuntimeError(f"Lyria {r.status_code}: {r.text[:500]}")
    return extraer_salida(r.json())


# === 6: salida ==============================================================

def slug(texto: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")
    return s[:50] or "mezcla"


def guardar(audio: bytes, meta: dict) -> Path:
    SALIDA.mkdir(parents=True, exist_ok=True)
    base = SALIDA / f"{datetime.now(TZ):%Y%m%d-%H%M%S}-{slug(meta['titulo'])}"
    mp3 = base.with_suffix(".mp3")
    mp3.write_bytes(audio)
    base.with_suffix(".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2))
    return mp3


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("artistas", nargs="+", help="2 a 4 artistas")
    ap.add_argument("--completa", action="store_true",
                    help=f"tema completo con {MODELO_COMPLETO} (US$0.08)")
    ap.add_argument("--instrumental", action="store_true")
    ap.add_argument("--idioma", choices=("auto", "es", "en"), default="auto",
                    help="idioma de la voz; auto = castellano si algun "
                         "artista es de un pais hispanohablante")
    ap.add_argument("--play", action="store_true", help="reproducir en mpv")
    ap.add_argument("--dry-run", action="store_true",
                    help="solo mostrar el prompt, no llama a Lyria")
    args = ap.parse_args()

    if not 2 <= len(args.artistas) <= 4:
        ap.error("pasá entre 2 y 4 artistas: con uno no hay mezcla y con "
                 "mas de 4 el resultado es un genero generico")

    modelo = MODELO_COMPLETO if args.completa else MODELO_CLIP
    try:
        perfiles = [await perfil(n) for n in args.artistas]
        for p in perfiles:
            logger.info("%s | %s | %s | %s", p["nombre"], p["decada_fuerte"],
                        p["pais"], ", ".join(p["tags"]) or "sin tags")

        idioma = idioma_voz(perfiles, args.idioma)
        if not args.instrumental:
            logger.info("voz en %s", "castellano" if idioma == "es" else "ingles")
        trad = await asyncio.to_thread(traducir, perfiles, args.instrumental,
                                       idioma)
        print(f"\nPROMPT ({len(trad['prompt'].split())} palabras):\n"
              f"{trad['prompt']}\n")
        if args.dry_run:
            return 0

        logger.info("generando con %s (US$%.2f)...", modelo, COSTO_USD[modelo])
        audio, letra = await generar(trad["prompt"], modelo)

        mp3 = guardar(audio, {
            "titulo": trad.get("titulo") or "mezcla",
            "artistas": [p["nombre"] for p in perfiles],
            "mbids": [p["mbid"] for p in perfiles],
            "perfiles": perfiles,
            "prompt": trad["prompt"],
            "idioma_voz": None if args.instrumental else idioma,
            "modelo": modelo,
            "costo_usd": COSTO_USD[modelo],
            "letra_y_estructura": letra,
            "generado": datetime.now(TZ).isoformat(timespec="seconds"),
        })
        print(f"guardado: {mp3.resolve()}  ({len(audio) / 1024:.0f} KB)")
        if letra:
            print(f"\n--- letra / estructura ---\n{letra[:1500]}")

        if args.play:
            from app.player import play_path
            await play_path(mp3.resolve())
            print("reproduciendo en mpv")
        return 0

    except LyriaRechazo as e:
        logger.error("Lyria rechazo el pedido. Mirá el prompt de arriba: "
                     "probablemente quedo algo que identifica a un artista. %s", e)
        return 2
    except (LookupError, ValueError, RuntimeError) as e:
        logger.error("%s", e)
        return 1
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
