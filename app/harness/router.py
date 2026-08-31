"""Router de intenciones. Etapa 1: patrones, cero tokens.

Aca se decide el costo del sistema. En un reproductor domestico el turno modal
tiene tres palabras ("pasa esta", "subi", "que es esto") y no necesita ni un
token. Todo lo que matchee en esta etapa es gratis para siempre.

Etapa 2 (clasificador Haiku) es el bloque H3 y todavia no esta: por ahora lo
que no matchea cae en `no_entendido`, que se repregunta con una plantilla.

MANTENIMIENTO — cada dos semanas:

    SELECT text_in, count(*) FROM turn_log
    WHERE stage <> 'regex' GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

Lo que aparezca repetido ahi es un patron que falta. La etapa 1 se hace crecer
con datos, no con imaginacion.
"""
import re
import unicodedata

from app.harness.intents import FALLBACK, REGEX, Intent

#: Vocativos y muletillas que no cambian la intencion. Se sacan antes de
#: matchear para no tener que escribir cada patron cuatro veces.
_RUIDO = re.compile(
    r"^(che|charly|hey|ok|dale|por favor|porfa|porfi|pf)\b[\s,]*"
    r"|[\s,]*\b(por favor|porfa|porfi|che|charly|gracias|nomas|un cachito)$"
)

_ESPACIOS = re.compile(r"\s+")
_PUNTUACION = re.compile(r"[^\w\s]")


def normalizar(text: str) -> str:
    """minusculas, sin tildes, sin puntuacion, sin vocativos, espacios simples."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = _PUNTUACION.sub(" ", t)
    t = _ESPACIOS.sub(" ", t).strip()
    # dos pasadas: "che charly pasala porfa"
    for _ in range(2):
        nuevo = _ESPACIOS.sub(" ", _RUIDO.sub("", t)).strip()
        if nuevo == t:
            break
        t = nuevo
    return t


# --- patrones ---------------------------------------------------------------
# Orden = prioridad. Los especificos van antes que los genericos:
# "poneme el volumen en 40" tiene que ganarle a "poneme ..." de playlist.
#
# Todo con ^...$ anclado: un patron laxo que se coma una frase larga es peor
# que un fallback, porque el error es silencioso.

PATRONES: list[tuple[str, re.Pattern]] = [
    ("control_vol_set", re.compile(
        r"^(?:pone(?:me|le|lo|la)?|deja(?:me|le|lo|la)?|meti?le|volumen)"
        r"(?:\s+el)?(?:\s+volumen)?\s+(?:en|a)\s+(?P<n>\d{1,3})$"
        r"|^volumen\s+(?P<n2>\d{1,3})$")),

    ("control_vol_up", re.compile(
        r"^(?:subi(?:le|la|lo)?|mas fuerte|mas volumen|mas alto|"
        r"subi el volumen|no se escucha)(?:\s+(?P<n>\d{1,3}))?$")),

    ("control_vol_down", re.compile(
        r"^(?:baja(?:le|la|lo)?|baji(?:le|to)|mas bajo|menos volumen|"
        r"baja el volumen|muy fuerte|esta muy fuerte)(?:\s+(?P<n>\d{1,3}))?$")),

    ("control_next", re.compile(
        r"^(?:next|siguiente|pasa(?:la|le|lo)?|otra|otro|cambia(?:la|lo)?|"
        r"la que sigue|salta(?:la)?|proxima|esta no|no esta)$")),

    ("control_prev", re.compile(
        r"^(?:anterior|volve|volve atras|atras|la anterior|"
        r"el anterior|previa|prev|para atras)$")),

    ("control_replay", re.compile(
        r"^(?:de nuevo|repeti(?:la|lo)?|otra vez|desde el principio|"
        r"volve a empezar|de vuelta)$")),

    ("control_pause", re.compile(
        r"^(?:pausa|pausala|para|pare|parala|stop|frena|frenala|"
        r"silencio|callate|shh+|mute)$")),

    ("control_play", re.compile(
        r"^(?:segui|seguila|play|reanuda|continua|dale play|"
        r"volve a poner|arranca|sonido)$")),

    ("control_stop", re.compile(
        r"^(?:basta|corta(?:la)?|apaga(?:la|lo)?|terminamos|"
        r"chau|listo por hoy|stop del todo)$")),

    ("estado_actual", re.compile(
        r"^(?:que suena|que es esto|que estas tocando|cual es esta|"
        r"que tema es|quien es este|quien canta|que estoy escuchando|"
        r"que es lo que suena|info)$")),

    ("estado_cola", re.compile(
        r"^(?:que sigue|que viene|la cola|que falta|que queda|"
        r"que hay despues|lo que viene)$")),

    ("saludo", re.compile(
        r"^(?:hola|holis|buenas|buenas tardes|buenas noches|buen dia|"
        r"hey|que tal|como andas|como va|ola)$")),

    ("ayuda", re.compile(
        r"^(?:ayuda|help|que sabes hacer|que podes hacer|comandos|"
        r"opciones|que hago|menu)$")),

    # Playlist va ULTIMO: cualquier control o consulta le gana. El prompt
    # que se manda al curador es el texto ORIGINAL, no el normalizado —
    # las tildes y las mayusculas son parte del pedido curatorial.
    ("playlist", re.compile(
        r"^(?:pone(?:me|le)?|arma(?:me)?|tira(?:me)?|dame|sona(?:me)?|"
        r"quiero escuchar|quiero oir|tengo ganas de|algo de)\s+(?P<libre>.{3,})$")),

    # --- todavia sin ejecutor (H2/H4): se dejan comentados a proposito.
    # Un patron que matchea un intent sin ejecutor es peor que no matchear:
    # el turno muere en un KeyError en vez de repreguntar.
    # ("efemerides_hoy",   ...),
    # ("estado_objetivos", ...),
]


def _slots(name: str, m: re.Match) -> dict:
    g = m.groupdict()
    n = g.get("n") or g.get("n2")
    if n is None:
        return {}
    valor = max(0, min(100, int(n)))
    if name == "control_vol_set":
        return {"level": valor}
    return {"delta": valor}


def etapa1(text: str) -> Intent | None:
    """Devuelve el Intent si algun patron matchea. Cero tokens, siempre."""
    n = normalizar(text)
    if not n:
        return None
    for name, pat in PATRONES:
        m = pat.match(n)
        if m:
            return Intent(name=name, slots=_slots(name, m),
                          confidence=1.0, stage=REGEX)
    return None


def rutear(text: str) -> Intent:
    """Punto de entrada del router.

    H3 mete la etapa 2 (clasificador Haiku) entre el `or` y el fallback,
    y ese es el unico lugar del harness donde se decide gastar un token.
    """
    it = etapa1(text)
    if it is None:
        return Intent(name="no_entendido", slots={},
                      confidence=0.0, stage=FALLBACK)
    if it.name == "playlist":
        it.slots["prompt"] = (text or "").strip()
    return it
