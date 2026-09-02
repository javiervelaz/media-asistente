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

#: Verbos con los que se pide musica. Se sacan del prompt antes de mandarlo:
#: `local_search` hace trigram contra `artists.name`, y "pone the beatles"
#: matchea peor que "the beatles" — tanto que devolvia a John Lennon por el
#: grafo de relaciones en vez de a los Beatles.
_VERBO_PEDIDO = re.compile(
    r"^\s*(?:pon[eé]r?(?:me|le|lo|la)?|tira(?:me)?|dame|"
    r"reproduc[ií](?:r|rme|me)?|busca(?:r|rme|me)?|arma(?:r|me)?|"
    r"son[aá](?:me)?|quiero\s+escuchar|"
    r"quiero\s+o[ií]r|tengo\s+ganas\s+de|escuchar)\s+"
    r"(?:algo\s+de\s+|un\s+poco\s+de\s+|musica\s+de\s+|m[uú]sica\s+de\s+)?",
    re.IGNORECASE)

_ESPACIOS = re.compile(r"\s+")
_PUNTUACION = re.compile(r"[^\w\s]")


def sin_verbo(text: str) -> str:
    """Saca el verbo de pedido conservando tildes y mayusculas.

    Se aplica al texto ORIGINAL porque el resultado va al curador y a
    `local_search`: el normalizado perderia los acentos de los nombres.
    """
    limpio = _VERBO_PEDIDO.sub("", text or "", count=1).strip()
    # Si el verbo era todo el mensaje, no hay pedido que mandar.
    return limpio or (text or "").strip()


def normalizar(text: str) -> str:
    """minusculas, sin tildes, sin puntuacion, sin vocativos, espacios simples."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = _PUNTUACION.sub(" ", t)
    t = _ESPACIOS.sub(" ", t).strip()
    # dos pasadas: "che charly pasala porfa"
    original = t
    for _ in range(2):
        nuevo = _ESPACIOS.sub(" ", _RUIDO.sub("", t)).strip()
        if nuevo == t:
            break
        t = nuevo

    # Si sacar el ruido dejo la frase vacia, el "ruido" ERA el mensaje:
    # "dale", "ok" y "che" son muletillas al principio de una orden, pero
    # solas son una respuesta. Sin esto, "dale" se normaliza a "" y no hay
    # forma de confirmar nada.
    return t or original


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
        r"^(?:subi(?:r|le|la|lo)?|mas fuerte|mas volumen|mas alto|"
        r"subi el volumen|subir volumen|no se escucha)(?:\s+(?P<n>\d{1,3}))?$")),

    ("control_vol_down", re.compile(
        r"^(?:baja(?:r|le|la|lo)?|baji(?:le|to)|mas bajo|menos volumen|"
        r"baja el volumen|bajar volumen|muy fuerte|esta muy fuerte)"
        r"(?:\s+(?P<n>\d{1,3}))?$")),

    ("control_next", re.compile(
        r"^(?:next|siguiente|proxim[ao]|pasa(?:r|la|le|lo)?|otra|otro|"
        r"cambia(?:r|la|lo)?|la que sigue|salta(?:r|la)?|saltear|"
        r"proxima|esta no|no esta)$")),

    ("control_prev", re.compile(
        r"^(?:anterior|volve|volve atras|atras|la anterior|"
        r"el anterior|previa|prev|para atras)$")),

    ("control_replay", re.compile(
        r"^(?:de nuevo|repeti(?:la|lo)?|otra vez|desde el principio|"
        r"volve a empezar|de vuelta)$")),

    ("control_pause", re.compile(
        r"^(?:pausa|pausar|pausala|para|parar|pare|parala|stop|"
        r"para la musica|frena la musica|"
        r"frena|frenar|frenala|silencio|callate|shh+|mute|"
        r"corta la musica|apaga la musica)$")),

    ("control_play", re.compile(
        r"^(?:segui|seguir|seguila|play|reanuda|reanudar|continua|"
        r"continuar|dale play|arranca|arrancar|sonido)$")),

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

    # Respuesta a una oferta ("¿lo pongo?"). Van antes de los controles
    # porque "dale" es ambiguo: con oferta vigente confirma, sin oferta el
    # ejecutor lo trata como play. La desambiguacion es por ESTADO, no por
    # patron — un regex no puede saber si hubo una pregunta antes.
    ("confirmar", re.compile(
        r"^(?:dale|si|sisi|si dale|ok|oka|obvio|claro|hacelo|ponelo|"
        r"pone eso|poneme eso|va|de una|bueno|listo|por que no|"
        r"me gusta|sale)$")),

    ("rechazar", re.compile(
        r"^(?:no|nah|no gracias|dejalo|ahora no|mejor no|paso|"
        r"no por ahora|otra cosa)$")),

    # --- H2: lectura de la base. Todo esto era ~19k tokens por turno. ---
    #
    # Van ANTES de playlist: "poneme algo que haya escuchado hoy" tiene que
    # ganarle al patron generico de pedido curatorial, que se lo comeria
    # entero y lo mandaria al curador — que no puede responderlo.

    # Exige un marcador explicito de reproduccion: o un verbo de poner, o
    # que la frase arranque con "algo". Sin eso, "que escuche hoy" —que es una
    # CONSULTA— matchearia aca y te pondria musica en vez de responderte.
    ("reproducir_historial", re.compile(
        r"^(?:pone(?:me|lo|la|le)?|tirame|dame|repeti(?:me)?|"
        r"volve a poner|volvamos a poner)\s+"
        r"(?:algo|temas?|musica|lo)?\s*(?:de\s+lo\s+)?que\s+"
        r"(?:haya\s+|hayamos\s+|ya\s+)?"
        r"(?:escuchad[oa]|escuche|escuchamos|sono|puse)\b(?P<cuando>.*)$")),

    ("reproducir_historial", re.compile(
        r"^algo\s+(?:de\s+lo\s+)?que\s+(?:haya\s+|hayamos\s+|ya\s+)?"
        r"(?:escuchad[oa]|escuche|escuchamos|sono|puse)\b(?P<cuando>.*)$")),

    ("reproducir_historial", re.compile(
        r"^(?:pone(?:me|lo|la|le)?|tirame|dame|repeti(?:me)?|volve a poner)\s+"
        r"lo\s+(?:de|que)\s+(?P<cuando>.+)$")),

    ("historial_artista", re.compile(
        r"^(?:que|cuanto|cuantas veces)\s+"
        r"(?:escuche|escuchamos|puse|sono)\s+de\s+(?P<artista>.+)$")),

    ("historial_periodo", re.compile(
        r"^(?:que|cual(?:es)?)\s+(?:escuche|escuchamos|sono|puse|"
        r"estuve escuchando|vengo escuchando)\b(?P<cuando>.*)$")),

    ("top_escuchados", re.compile(
        r"^(?:(?:que|a quien|quien)\s+(?:escucho|escuchamos)\s+mas.*"
        r"|mis mas escuchados|top|top artistas|mas escuchados|"
        r"lo que mas escucho|lo mas escuchado|"
        r"(?:canciones|temas|artistas|bandas|discos)\s+mas\s+escuchad[oa]s?|"
        r"(?:canciones|temas|artistas|bandas)\s+que\s+mas\s+escuch[eo])$")),

    ("salteados", re.compile(
        r"^(?:que\s+(?:me\s+)?salte[oa].*|que\s+me\s+salteo|"
        r"que no me gusta|mis skips|que salteo siempre)$")),

    ("nunca_escuchado", re.compile(
        r"^(?:que\s+(?:tengo|hay)\s+(?:en\s+)?(?:vinilo|el estante)"
        r"(?:\s+sin\s+escuchar)?"
        r"|discos sin escuchar|que no escuche nunca|"
        r"vinilos sin escuchar|que me falta escuchar)$")),

    ("discografia", re.compile(
        r"^(?:que\s+discos\s+(?:tengo|hay|tenes)\s+de|discografia\s+de|"
        r"discos\s+de|albumes\s+de)\s+(?P<artista>.+)$")),

    ("relaciones", re.compile(
        r"^(?:quien(?:es)?\s+toc(?:o|aron)\s+(?:con|en)|"
        r"con\s+quien(?:es)?\s+toc(?:o|aron)|"
        r"relaciones\s+de|vinculos\s+de|quien(?:es)?\s+conoce)"
        r"\s+(?P<artista>.+)$")),

    ("efemerides_hoy", re.compile(
        r"^(?:efemerides|que se cumple hoy|que paso un dia como hoy|"
        r"un dia como hoy|que paso hoy|"
        r"aniversarios?|que se festeja hoy|que cumple anos hoy)$")),

    # Pedir la coleccion directo, sin pasar por objetivos ni por un listado.
    # Va antes de `playlist` porque "pone algo de mi coleccion" matchea el
    # patron generico de pedido, y ahi terminaba en el curador — que ni
    # siquiera sabe que tenes en el estante.
    # UN DISCO entero, en orden. Va antes que `reproducir_coleccion`:
    # "poneme un vinilo" es poner un disco, no catorce temas sueltos.
    ("reproducir_disco_coleccion", re.compile(
        r"^(?:pone(?:r|me|le|lo|la)?|tirame|dame|quiero escuchar|"
        r"reproduci(?:r|me)?|escuchar)?\s*"
        r"(?:un|una|algun)\s+"
        r"(?:disco|album|vinilo|lp)"
        r"(?:\s+entero|\s+completo)?"
        r"(?:\s+(?:de\s+|del\s+)?(?:mi|mis|la|el)?\s*"
        r"(?:coleccion|vinilos?|estante|discos)?)?$")),

    ("reproducir_disco_coleccion", re.compile(
        r"^(?:pone(?:me|le|lo|la)?|tirame|dame)?\s*"
        r"(?:un\s+)?(?:disco|album|vinilo)\s+(?:entero|completo)$")),

    # Temas sueltos de la coleccion: variedad en vez de un disco.
    ("reproducir_coleccion", re.compile(
        r"^(?:pone(?:r|me|le|lo|la)?|tirame|dame|quiero escuchar|"
        r"reproduci(?:r|me)?|escuchar|busca(?:r|me)?)?\s*"
        r"(?:algo|temas?|musica|un poco)?\s*"
        r"(?:de\s+|del\s+|de\s+la\s+)?"
        r"(?:mi|mis|el|los|la)?\s*"
        r"(?:coleccion|vinilos?|estante|discos)$")),

    # Cruce artista + coleccion: aparecio en turn_log y no existia.
    #
    # El patron es ESTRICTO a proposito: el artista es texto libre en el
    # medio de la frase, que es justo donde un regex se vuelve fragil. Con
    # el verbo y el articulo opcionales se comia "quiero escuchar mas de mi
    # coleccion" y "borrame el objetivo de vinilo". Lo que no entra acá lo
    # agarra el clasificador, que para esto es mejor herramienta.
    ("coleccion_de_artista", re.compile(
        r"^(?:busca(?:r|me)?|pone(?:r|me|le)?|tirame|dame|"
        r"que\s+tengo\s+de|tengo|hay)\s+"
        r"(?:algo\s+de\s+|temas?\s+de\s+|discos?\s+de\s+)?"
        r"(?P<artista>(?!(?:quiero|borrame|algo|los|las|un|una|el|la|de|"
        r"que|mi|mis|para|con)\b)[\w\s'.&-]{2,40}?)\s+"
        r"(?:en|de)\s+(?:mi|mis|la|el|los)\s+"
        r"(?:coleccion|vinilos?|estante|discos)$")),

    # --- H4: objetivos ---

    ("estado_objetivos", re.compile(
        r"^(?:como voy|mis objetivos|como vengo|objetivos|"
        r"como voy con (?:mis )?(?:los )?objetivos|estado de objetivos|"
        r"como vengo con eso)$")),

    ("borrar_objetivo", re.compile(
        r"^(?:borra|saca|olvida|cancela|elimina)(?:me|te)?\s+"
        r"(?:el\s+)?objetivo\s+(?:de\s+)?(?P<que>.+)$")),

    # "quiero escuchar mas de mi coleccion" / "mas vinilo"
    ("set_objetivo_coleccion", re.compile(
        r"^(?:quiero\s+)?(?:escuchar\s+)?mas\s+"
        r"(?:de\s+)?(?:mi\s+)?(?:coleccion|vinilo|vinilos|el estante|"
        r"mis discos)(?:\s+(?P<n>\d{1,3})\s*%?)?$")),

    ("set_objetivo_descubrimiento", re.compile(
        r"^(?:quiero\s+)?(?:descubrir|conocer)\s+"
        r"(?:(?P<n>\d{1,3})\s+)?(?:artistas?|bandas?|cosas?)"
        r"(?:\s+nuev[oa]s?|\s+no\s+escuchad[oa]s?|\s+que\s+no\s+conozco)?"
        r"(?:\s+por\s+\w+)?$")),

    ("set_objetivo_profundidad", re.compile(
        r"^(?:quiero\s+)?(?:escuchar\s+)?(?:mas\s+)?"
        r"(?:albumes?|discos)\s+enteros?(?:\s+(?P<n>\d{1,3})\s*%?)?$")),

    # "quiero escuchar mas jazz" — va DESPUES de coleccion/profundidad para
    # que "mas vinilo" y "mas discos enteros" no caigan aca.
    ("set_objetivo_genero", re.compile(
        r"^(?:quiero\s+)?escuchar\s+mas\s+(?P<genero>[a-z0-9 \-]{3,30})$")),

    # Playlist va ULTIMO: cualquier control o consulta le gana. El prompt
    # que se manda al curador es el texto ORIGINAL, no el normalizado —
    # las tildes y las mayusculas son parte del pedido curatorial.
    ("playlist", re.compile(
        r"^(?:pone(?:r|me|le|lo|la)?|arma(?:me)?|tira(?:me)?|dame|"
        r"sona(?:me)?|reproduci(?:r|me)?|reproduce|busca(?:r|me)?|"
        r"quiero escuchar|quiero oir|tengo ganas de|escuchar|algo de)"
        r"\s+(?P<libre>.{3,})$")),

    # --- todavia sin ejecutor (H2/H4): se dejan comentados a proposito.
    # Un patron que matchea un intent sin ejecutor es peor que no matchear:
    # el turno muere en un KeyError en vez de repreguntar.
    # ("efemerides_hoy",   ...),
    # ("estado_objetivos", ...),
]


def _slots(name: str, m: re.Match) -> dict:
    g = m.groupdict()

    if "artista" in g and g["artista"]:
        return {"artista": g["artista"].strip()}

    if "genero" in g and g["genero"]:
        return {"genero": g["genero"].strip()}

    if "que" in g and g["que"]:
        return {"que": g["que"].strip()}

    if name.startswith("set_objetivo"):
        n = g.get("n")
        return {"n": int(n)} if n else {}

    if "cuando" in g:
        # Puede venir vacio ("que escuche" pelado): la ventana por defecto
        # es hoy, que es de lo que habla el usuario el 90% de las veces.
        return {"cuando": (g.get("cuando") or "").strip()}

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
            slots = _slots(name, m)
            if name == "playlist":
                # El prompt sale del texto ORIGINAL sin el verbo: el
                # normalizado perderia las tildes de los nombres propios.
                slots["prompt"] = sin_verbo(text)
            return Intent(name=name, slots=slots, confidence=1.0, stage=REGEX)
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
    return it
