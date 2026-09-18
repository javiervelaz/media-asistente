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
#: `\w` incluye el guion bajo, asi que "en el estante_" no matcheaba
#: "estante". En un teclado de telefono el `_` esta pegado al `?`.
_PUNTUACION = re.compile(r"[^\w\s]|_")


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

#: El estante, en las palabras que usa el usuario. Se repite en once
#: patrones: escribirlo una vez es la diferencia entre agregar un sinonimo
#: en un lugar o en once.
_ESTANTE = r"(?:coleccion|estante|vinilos?|discos)"

#: "escuchar" escrito por alguien que tipea en un telefono. Del turn_log
#: real salieron "ecuchar" y "scuchar"; la `h` y la `e` inicial son las dos
#: teclas que se pierden. No es tolerar cualquier cosa: son tres formas
#: cerradas, no un fuzzy match.
_ESCUCHAR = r"e?s?cuchar"

#: Sustantivos con los que se pregunta por ARTISTAS y no por discos. Es la
#: unica distincion determinista entre "que discos de Queen tengo" (un
#: artista) y "que artistas de jazz tengo" (un atributo): nadie pregunta
#: "que artistas de Queen tengo".
_N_ARTISTA = r"(?:artistas?|bandas?|grupos?|musicos?)"

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
        r"(?:\s+sin\s+" + _ESCUCHAR + r")?"
        r"|discos sin " + _ESCUCHAR + r"|que no escuche nunca|"
        r"vinilos sin " + _ESCUCHAR + r"|que me falta escuchar)$")),

    # El estante sin escuchar, en las formas REALES del turn_log. Son
    # consultas, no pedidos: listan y dejan la oferta.
    #
    #   Que me queda sin escuchar de la coleccion?
    #   Que artistas de la coleccion no escuche aun?
    #   que tento sin ecuchar en el estante_          <- dos tipeos
    #   sin escuchar?
    #
    # `_ESCUCHAR` tolera "escuchar", "ecuchar" y "scuchar": los tipeos no son
    # ruido, son el 12% de lo que llega por Telegram desde un telefono.
    ("nunca_escuchado", re.compile(
        r"^que\s+(?:me\s+)?(?:queda|quedan|falta|faltan|ten[gt]o|hay)\s+"
        r"(?:por\s+)?sin\s+" + _ESCUCHAR + r"?"
        r"(?:\s+(?:en|de)\s+(?:mi|mis|la|el|los)?\s*" + _ESTANTE + r")?$")),

    ("nunca_escuchado", re.compile(
        r"^que\s+(?:artistas?|bandas?|discos?|albumes?|vinilos?|cosas?)\s+"
        r"(?:de|en)\s+(?:mi|mis|la|el|los)\s+" + _ESTANTE + r"\s+"
        r"(?:no|nunca)\s+(?:me\s+)?escuche(?:\s+(?:aun|todavia|nunca))?$")),

    # "que tento sin ecuchar en el estante". El verbo puede faltar entero:
    # lo que identifica el pedido es "sin escuchar" + el estante.
    ("nunca_escuchado", re.compile(
        r"^que\s+\w+\s+sin\s+" + _ESCUCHAR + r"\s+"
        r"(?:en|de)\s+(?:mi|mis|la|el|los)\s+" + _ESTANTE + r"$")),

    # "sin escuchar?" pelado. Es corto y sin verbo, pero no es ambiguo:
    # ninguna otra cosa en el sistema se pide asi.
    ("nunca_escuchado", re.compile(
        r"^(?:que\s+queda\s+)?sin\s+" + _ESCUCHAR + r"$")),

    # --- Orden de pregunta en castellano: el verbo va al FINAL ---------------
    #
    # Los diez turnos que pagaron el clasificador entre el 2 y el 12/9 son
    # todos esta forma, y ninguno de los patrones de arriba la cubre porque
    # todos esperan el verbo adelante ("que discos tengo de X"). Preguntando
    # se dice al reves:
    #
    #   Que discos de Queen hay en la coleccion?          x4
    #   De Andres Calamaro que tenemos en la coleccion?
    #   que discos de frank sinastra tenemos en la coleccion?
    #   Que discos de REM tengo disponible para escuchar?
    #   que discos de mi coleccion estan sin escuchar?
    #   que hay en la coleccion para escuchar?
    #   Que puedo escuchar de mi coleccion?
    #
    # La distincion coleccion / discografia se resuelve por una palabra
    # presente en el texto, no por criterio del modelo: si nombra el estante
    # es `coleccion_de_artista`, si no es `discografia`. Deterministico y sin
    # ambiguedad — la regla que el catalogo ya tenia y el regex no aplicaba.

    # "que discos de mi coleccion estan sin escuchar" — va PRIMERO porque
    # "de mi coleccion" matchearia como si "mi coleccion" fuera el artista.
    ("nunca_escuchado", re.compile(
        r"^que\s+(?:discos?|albumes?|vinilos?|cosas?)\s+"
        r"(?:de\s+(?:mi|la|el)\s+(?:coleccion|estante|vinilos?)\s+)?"
        r"(?:estan\s+|quedan\s+|tengo\s+|me\s+quedan\s+)?"
        r"sin\s+escuchar$")),

    # "que hay en la coleccion para escuchar" / "que puedo escuchar de mi
    # coleccion". Es una CONSULTA, no un pedido: lista el estante sin
    # escuchar y deja la oferta, que el turno siguiente resuelve con "dale".
    # Reproducir directo romperia la regla de que preguntar no puede pisarte
    # lo que estas escuchando.
    ("nunca_escuchado", re.compile(
        r"^que\s+(?:hay|tengo|queda|me\s+queda|puedo\s+escuchar|"
        r"puedo\s+poner|escucho)\s+"
        r"(?:en|de)\s+(?:mi|mis|la|el|los)\s+"
        r"(?:coleccion|estante|vinilos?|discos)"
        r"(?:\s+para\s+escuchar|\s+sin\s+escuchar)?$")),

    # --- Preguntar no puede pisarte lo que estas escuchando ---------------
    #
    # Estos tres patrones apuntaban a `coleccion_de_artista`, que REPRODUCE.
    # "Que discos de Queen hay en la coleccion?" arrancaba a Queen encima de
    # lo que estaba sonando. Es el mismo bug que efemerides: listar y
    # reproducir son dos intents, y la forma interrogativa lista.
    #
    # El "dale" del turno siguiente reproduce EXACTAMENTE los mbids que se
    # listaron — misma regla que H2.1 y H5.

    # "que artistas de jazz tengo en la coleccion" — ATRIBUTO, no artista.
    # Va primero: si no, `artista` captura "jazz" y busca una banda que no
    # existe. La distincion es el sustantivo, no el criterio del modelo.
    ("coleccion_por_atributo", re.compile(
        r"^(?:que|cuales|cuantos|cuantas|listame|dame|mostrame|deci(?:me)?)\s+"
        + _N_ARTISTA + r"\s+"
        r"(?:de|del|de\s+la|nacidos?\s+en|que\s+sean)\s+"
        # "de los 80", "de los anios 80": el determinante se consume aca.
        # Dejarselo al backtracking del lookahead funcionaba en una frase y
        # no en la de al lado, que es la peor clase de patron.
        r"(?:los\s+|las\s+)?(?:anios?\s+)?"
        r"(?P<valor>(?!(?:mi|mis|la|el|los|las)\b)[\w\s'.&-]{2,30}?)"
        r"(?:\s+(?:tengo|hay|tenemos|tenes|ten[gt]o|puedo\s+" + _ESCUCHAR + r"))?"
        r"(?:\s+(?:en|de)\s+(?:mi|mis|la|el|los)\s+" + _ESTANTE + r")?$")),

    # "que tengo artistas nacidos en argentina tengo en mi coleccion" — el
    # usuario escribe el verbo dos veces. Sale tal cual del turn_log.
    ("coleccion_por_atributo", re.compile(
        r"^que\s+(?:ten[gt]o\s+)?" + _N_ARTISTA + r"\s+"
        r"(?:nacidos?\s+en|de|del)\s+"
        r"(?P<valor>[\w\s'.&-]{2,30}?)\s+"
        r"(?:ten[gt]o|hay|tenemos)\s+"
        r"(?:en|de)\s+(?:mi|mis|la|el|los)\s+" + _ESTANTE + r"$")),

    # "cuantos canciones de jazz tengo en mi coleccion" / "que tenemos de
    # jazz". Aca el sustantivo NO desambigua, asi que el ejecutor resuelve:
    # busca el artista primero y cae al atributo si no existe en el estante.
    ("coleccion_consulta", re.compile(
        r"^(?:que|cuales|cuantos|cuantas)\s+"
        r"(?:discos?|albumes?|temas?|canciones?|vinilos?|cosas?)\s+"
        r"(?:de|del)\s+"
        r"(?P<valor>(?!(?:mi|mis|la|el|los|las|un|una|que|para|con)\b)"
        r"[\w\s'.&-]{2,40}?)\s+"
        r"(?:ten[gt]o|hay|tenemos|tenes|quedan?|me\s+quedan)"
        r"(?:\s+(?:disponibles?|guardados?|cargados?))?"
        r"\s+(?:en|un|de)\s+(?:mi|mis|la|el|los)\s+" + _ESTANTE + r"?"
        r"(?:\s+para\s+" + _ESCUCHAR + r")?$")),

    # "que tenemos de jazz?" — sin sustantivo y sin marcador de estante.
    # "tenemos" ya implica posesion: se pregunta por lo que hay, no por lo
    # que existe en el mundo.
    ("coleccion_consulta", re.compile(
        r"^(?:que|cuanto|cuantos)\s+(?:tenemos|ten[gt]o|hay)\s+"
        r"(?:de|del)\s+"
        r"(?P<valor>(?!(?:mi|mis|la|el|los|las)\b)[\w\s'.&-]{2,30}?)$")),

    # "de Andres Calamaro que tenemos en la coleccion" — artista adelante.
    ("coleccion_consulta", re.compile(
        r"^de\s+"
        r"(?P<valor>(?!(?:mi|mis|la|el|los|las|un|una|que)\b)"
        r"[\w\s'.&-]{2,40}?)\s+"
        r"(?:que|cuales|cuantos)\s+"
        r"(?:discos?|albumes?|temas?|cosas?\s+)?"
        r"(?:ten[gt]o|hay|tenemos|tenes)"
        r"\s+(?:en|de)\s+(?:mi|mis|la|el|los)\s+" + _ESTANTE + r"$")),

    # "arctick monkeys de la coleccion" — nombre pelado + marcador. No hay
    # verbo: es lo mas corto que se puede escribir para pedir esto.
    #
    # El lookahead NO es paranoia: sin el se comia "pone algo de mi
    # coleccion" con valor="pone algo" y buscaba un artista llamado asi.
    # Un patron que empieza con texto libre tiene que declarar todo lo que
    # NO es un nombre propio, porque lo unico que lo limita es el final.
    ("coleccion_consulta", re.compile(
        r"^(?P<valor>(?!(?:que|cual|cuanto|mi|mis|la|el|los|las|un|una|"
        r"algo|mas|todo|otra|otro|pon|pone|poneme|ponele|poner|dame|"
        r"tirame|busca|buscame|buscar|arma|armame|reproduci|reproducir|"
        r"escuchar|quiero|deja|dejame|meti|sacame|borra|borrame)\b)"
        r"[\w\s'.&-]{2,40}?)\s+"
        r"(?:de|en)\s+(?:mi|mis|la|el|los)\s+" + _ESTANTE + r"$")),

    # Misma forma pero SIN marcador de estante: es la discografia del grafo,
    # no el vinilo. "que discos de REM tengo disponible para escuchar".
    ("discografia", re.compile(
        r"^(?:que|cuales|cuantos)\s+"
        r"(?:discos?|albumes?)\s+(?:de|del)\s+"
        r"(?P<artista>(?!(?:mi|mis|la|el|los|las|un|una|que|para|con)\b)"
        r"[\w\s'.&-]{2,40}?)\s+"
        r"(?:tengo|hay|tenemos|tenes|quedan?)"
        r"(?:\s+(?:disponibles?|guardados?|cargados?))?"
        r"(?:\s+para\s+escuchar)?$")),

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
    # "buscar X en mi coleccion" BUSCA; "pone X de mi coleccion" PONE. Se
    # separan porque el verbo ya dice cual de las dos cosas es, y hasta H6
    # las dos arrancaban musica: escribir "buscar" te pisaba lo que sonaba.
    ("coleccion_consulta", re.compile(
        r"^(?:busca(?:r|me)?|list(?:ame|ar)|mostrame|"
        r"que\s+tengo\s+de|tengo|hay)\s+"
        r"(?:algo\s+de\s+|temas?\s+de\s+|discos?\s+de\s+)?"
        r"(?P<valor>(?!(?:quiero|borrame|algo|los|las|un|una|el|la|de|"
        r"que|mi|mis|para|con)\b)[\w\s'.&-]{2,40}?)\s+"
        r"(?:en|de)\s+(?:mi|mis|la|el|los)\s+"
        r"(?:coleccion|vinilos?|estante|discos)$")),

    ("coleccion_de_artista", re.compile(
        r"^(?:pone(?:r|me|le)?|tirame|dame)\s+"
        r"(?:algo\s+de\s+|temas?\s+de\s+|discos?\s+de\s+)?"
        r"(?P<artista>(?!(?:quiero|borrame|algo|los|las|un|una|el|la|de|"
        r"que|mi|mis|para|con)\b)[\w\s'.&-]{2,40}?)\s+"
        r"(?:en|de)\s+(?:mi|mis|la|el|los)\s+"
        r"(?:coleccion|vinilos?|estante|discos)$")),

    # --- H5: el escape de la proactividad ---
    #
    # OJO con el orden y con el anclaje: `silencio` a secas ya significa
    # "pausá la música" y matchea en `control_pause`, mas arriba. Este patron
    # EXIGE una duracion, asi que no se pisan: "silencio" pausa, "silencio una
    # semana" calla al bot. Son dos cosas distintas y la diferencia es una
    # palabra.
    ("silenciar", re.compile(
        r"^(?:silencio|no me escribas|no me molestes|no me avises|"
        r"dejame tranquilo|dejame en paz|callate)\s+"
        r"(?:por\s+)?(?:un[ao]?\s+)?"
        r"(?:(?P<cantidad>\d{1,3})\s+)?"
        r"(?P<unidad>dias?|semanas?|mes|meses)$")),

    ("silenciar", re.compile(
        r"^(?:volve a escribirme|escribime de nuevo|escribime|"
        r"sacame el silencio|ya podes escribirme)$")),

    # --- H4: objetivos ---

    # El anclaje en $ dejaba afuera cualquier cola temporal: "como vengo
    # este mes" pago una clasificacion de Haiku por tres palabras de mas.
    # La ventana del objetivo la define `window_days`, no la frase, asi que
    # la cola se acepta y se ignora.
    ("estado_objetivos", re.compile(
        r"^(?:como\s+(?:voy|vengo|vamos|venimos)"
        r"(?:\s+con\s+(?:mis\s+|los\s+)?(?:objetivos?|eso|esto))?"
        r"(?:\s+(?:este\s+mes|esta\s+semana|este\s+ano|hoy|"
        r"hasta\s+ahora|por\s+ahora))?"
        r"|mis\s+objetivos|objetivos|estado\s+de\s+(?:mis\s+)?objetivos|"
        r"que\s+objetivos\s+tengo)$")),

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

    if "valor" in g and g["valor"]:
        return {"valor": g["valor"].strip()}

    if "que" in g and g["que"]:
        return {"que": g["que"].strip()}

    if name == "silenciar":
        unidad = (g.get("unidad") or "").strip()
        if not unidad:
            return {"dias": 0}          # "volve a escribirme"
        cantidad = int(g.get("cantidad") or 1)
        # Por prefijo y no por `rstrip("s")`: eso convertia "mes" en "me" y
        # "no me escribas por un mes" silenciaba un dia.
        if unidad.startswith("semana"):
            mult = 7
        elif unidad.startswith("mes"):
            mult = 30
        else:
            mult = 1
        return {"dias": cantidad * mult}

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


#: Palabras de control de una sola pieza, para el rescate por tipeo. Es un
#: set CERRADO y corto a proposito: la correccion difusa es la clase de cosa
#: que empieza arreglando "enxt" y termina mandando "next" cuando pediste
#: otra cosa.
_CONTROL_TIPEABLE = {
    "next": "control_next", "siguiente": "control_next",
    "pausa": "control_pause", "parar": "control_pause",
    "stop": "control_pause", "play": "control_play",
    "anterior": "control_prev", "volumen": "control_vol_set",
}

#: Cuanto se parece tiene que ser. 0.72 deja entrar "enxt"->"next" (0.75) y
#: deja afuera "otra"->"stop". Debajo de esto empieza a adivinar.
_UMBRAL_TIPEO = 0.72


def _control_difuso(n: str) -> Intent | None:
    """Rescata el tipeo de un comando de control, a cero tokens.

    "enxt" costo 3358 tokens de clasificador para terminar en `control_next`.
    Una sola palabra corta que no matcheo nada no puede ser un pedido
    curatorial: o es un comando mal tipeado o no es nada.

    Solo corre con UN token de hasta 12 letras, y solo contra
    `_CONTROL_TIPEABLE`. Un nombre de artista ("u2", "abba") no se parece a
    ninguna de esas palabras, asi que no se lo roba.
    """
    if " " in n or not (2 <= len(n) <= 12):
        return None
    from difflib import SequenceMatcher
    mejor, puntaje = None, 0.0
    for palabra, intent in _CONTROL_TIPEABLE.items():
        r = SequenceMatcher(None, n, palabra).ratio()
        if r > puntaje:
            mejor, puntaje = intent, r
    if mejor and puntaje >= _UMBRAL_TIPEO:
        # `confidence` guarda el parecido: si aparecen correcciones malas en
        # el turn_log, se ven por acá sin tener que reproducir el turno.
        return Intent(name=mejor, slots={}, confidence=round(puntaje, 2),
                      stage=REGEX)
    return None


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
    return _control_difuso(n)


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
