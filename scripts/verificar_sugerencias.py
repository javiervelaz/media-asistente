"""Verifica el bloque H5 (proactividad) contra la base real. 0 tokens.

Lo que protege, en orden de importancia:

  1. **Que el silencio funcione.** Las cuatro guardas de corte son el bloque:
     sin ellas H5 es un bot que te escribe de mas, y de eso no se vuelve. Se
     prueban inyectando filas y fechas, no esperando dias.
  2. **Que la oferta sobreviva.** Registrar, "reiniciar" y aceptar seis horas
     despues tiene que encolar exactamente los mbids de esa fila. Es la razon
     entera de que exista la tabla en vez de usar `SessionState`.
  3. **Que un segundo toque del boton no reproduzca dos veces.** En Telegram
     la gente aprieta dos veces.
  4. **Que `callback_data` entre en 64 bytes** con un id de 10 digitos.
  5. **Que la poda corte sola** un tipo con baja aceptacion.

Trabaja en una sala aparte (`__verificacion__`) y limpia lo suyo al final, en
un `finally`: una corrida fallida no puede dejar basura que despues ensucie la
tasa de aceptacion real.

Necesita Neon. La mitad de lo que puede fallar aca es SQL de seleccion, y
mockearlo seria repetir el error del `eof`: un test que confirma lo que ya
creiamos en vez de lo que la base hace.

Uso:  python -m scripts.verificar_sugerencias
"""
import asyncio
import logging
import sys
from datetime import timedelta

from app.config import settings
from app.db import close_pool, execute, fetchval
from app.harness import goals, queries, render, sugerencias as sug

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(message)s")

SALA = "__verificacion__"
_suena_real = sug._suena_ahora
FALLOS: list[str] = []


def check(cond: bool, que: str, detalle: str = "") -> None:
    print(f'  {"ok   " if cond else "FALLA"} {que}' + (f'  [{detalle}]' if detalle else ""))
    if not cond:
        FALLOS.append(que)


async def _limpiar() -> None:
    await execute("DELETE FROM sugerencias WHERE room_id = $1", SALA)
    await execute("DELETE FROM sala_prefs WHERE room_id = $1", SALA)


async def main() -> int:
    activa_original = settings.harness_sugerencia_activa
    try:
        await _correr()
    finally:
        sug._suena_ahora = _suena_real
        settings.harness_sugerencia_activa = activa_original
        try:
            await _limpiar()
        except Exception:
            print("  OJO: no pude limpiar la sala de verificacion")
        await close_pool()

    print()
    if FALLOS:
        print(f"{len(FALLOS)} FALLOS:")
        for f in FALLOS:
            print(f"  - {f}")
        return 1
    print("todo ok")
    return 0


#: `_suena_ahora` consulta mpv de verdad, y mpv es una condicion EXTERNA al
#: script: si hay musica sonando mientras corre, esa guarda corta antes que
#: las de la base y los asserts de abajo fallan por algo que no tiene nada que
#: ver con lo que prueban. Un verificador cuyo resultado depende de si estas
#: escuchando no verifica nada, asi que se neutraliza y se prueba aparte.
_SUENA = {"v": False}


async def _suena_falso() -> bool:
    return _SUENA["v"]


async def _correr() -> None:
    await _limpiar()
    ahora = queries.ahora()
    sug._suena_ahora = _suena_falso

    print("\n0 · todas las consultas del bloque PREPARAN contra Neon")
    # asyncpg prepara cada statement, y Postgres infiere los tipos de los
    # parametros por contexto. Un cast faltante falla al PREPARAR, no al
    # ejecutar: ningun dato de prueba lo evita y no se ve hasta produccion.
    # `started_at > $1 - make_interval(...)` hacia que Postgres dedujera que
    # $1 era un interval y reventaba con
    #   operator does not exist: timestamp with time zone > interval
    # Esto lo agarra sin tocar una sola fila.
    consultas = {
        "escucha reciente": sug.SQL_ESCUCHA_RECIENTE,
        "silencio":
            "SELECT silencio_hasta FROM sala_prefs WHERE room_id = $1",
        "ya se mando hoy":
            "SELECT id FROM sugerencias WHERE room_id = $1 AND enviada_at >= $2 "
            "ORDER BY enviada_at DESC LIMIT 1",
        "pendiente":
            "SELECT id, enviada_at FROM sugerencias WHERE room_id = $1 "
            "AND aceptada IS NULL ORDER BY enviada_at DESC LIMIT 1",
        "mbids recientes":
            "SELECT DISTINCT unnest(mbids) AS mbid FROM sugerencias "
            "WHERE enviada_at > now() - make_interval(days => $1)",
        "ultimo kind":
            "SELECT kind FROM sugerencias WHERE room_id = $1 AND enviada_at > $2 "
            "ORDER BY enviada_at DESC LIMIT 1",
        "estante": sug.SQL_ESTANTE,
    }
    for nombre, q in consultas.items():
        try:
            await execute(f"PREPARE __v AS {q}")
            await execute("DEALLOCATE __v")
            check(True, f"prepara: {nombre}")
        except Exception as e:
            check(False, f"prepara: {nombre}", str(e).split("\n")[0])

    print("\n1 · callback_data entra en los 64 bytes de Telegram")
    for i in (1, 1234, 9999999999):
        cb = sug.callback_data(i, True)
        check(len(cb.encode()) <= 64, f"callback_data({i}) cabe",
              f"{cb} = {len(cb.encode())} bytes")

    print("\n2 · el bloque apagado no manda, pero el preview igual muestra")
    settings.harness_sugerencia_activa = False
    s, motivo = await sug.elegir(SALA, ignorar_hora=True)
    check(s is None and "apagado" in motivo, "apagado -> no manda", motivo)

    # El preview es lo que se mira para decidir si prender el bloque: exigir
    # que ya este prendido lo dejaba inservible.
    s, motivo = await sug.elegir(SALA, ignorar_hora=True, ignorar_apagado=True)
    check("apagado" not in motivo,
          "con el bloque apagado, el preview igual evalua contenido", motivo)

    settings.harness_sugerencia_activa = True

    print("\n2.5 · la guarda de mpv: no interrumpir lo que esta sonando")
    _SUENA["v"] = True
    s, motivo = await sug.elegir(SALA, ignorar_hora=True)
    check(s is None and "escuchando ahora" in motivo,
          "sonando -> no manda", motivo)
    _SUENA["v"] = False

    # La real, contra el mpv que haya: lo unico que se exige es que NO levante
    # y devuelva un bool. Falla abierto por diseño — una guarda que no puede
    # verificar no puede apagar el bloque.
    try:
        real = await _suena_real()
        check(isinstance(real, bool), "_suena_ahora() real devuelve bool", str(real))
    except Exception as e:
        check(False, "_suena_ahora() real no levanta", str(e))

    print("\n3 · la guarda de hora usa harness_tz, no la del proceso")
    otra = (settings.harness_sugerencia_hora + 3) % 24
    falsa = ahora.replace(hour=otra)
    s, motivo = await sug.elegir(SALA, ahora=falsa)
    check(s is None and "no es la hora" in motivo, "fuera de hora -> no manda", motivo)

    print("\n4 · que se mandaria hoy (sin guardas de hora)")
    s, motivo = await sug.elegir(SALA, ignorar_hora=True)
    if s is None:
        print(f"        sin sugerencia: {motivo}")
        print("        (valido: el silencio es una salida correcta)")
    else:
        texto = render.sugerencia(s)
        check(texto.count("\n") + 1 <= 3, "el mensaje no pasa de 3 lineas")
        check("!" not in texto and "\u00a1" not in texto,
              "sin signos de admiracion")
        print(f"        [{s.kind}] {s.etiqueta}")
        for l in texto.split("\n"):
            print(f"        | {l}")

    print("\n5 · prioridad: la efemeride del estante gana si existe")
    recientes = await sug._mbids_recientes()
    efem = await sug._efemeride(recientes)
    if efem is None:
        print("        hoy no hay efemeride con weight=1; nada que comparar")
    else:
        s2, _ = await sug.elegir(SALA, ignorar_hora=True)
        check(s2 is not None and s2.kind == "efemeride",
              "con efemeride disponible, gana efemeride",
              f"eligio {s2.kind if s2 else None}")

    print("\n6 · una por dia")
    sid = await sug.registrar(
        sug.Sugerencia(kind="estante", etiqueta="prueba", mbids=["mbid-falso-1"]),
        SALA)
    s, motivo = await sug.elegir(SALA, ignorar_hora=True)
    check(s is None and "ya se mando una hoy" in motivo,
          "con una de hoy, no manda otra", motivo)

    print("\n7 · la oferta sobrevive al reinicio y al paso de las horas")
    # "Reiniciar" es no tener NADA en memoria: `responder` solo recibe un id.
    fila = await sug.responder(sid, True)
    check(fila is not None, "una sugerencia de antes se puede contestar")
    check(fila and list(fila["mbids"]) == ["mbid-falso-1"],
          "devuelve exactamente los mbids que se ofrecieron",
          str(fila and list(fila["mbids"])))

    print("\n8 · el segundo toque del boton no hace nada")
    otra_vez = await sug.responder(sid, True)
    check(otra_vez is None, "responder dos veces devuelve None (idempotente)")

    print("\n9 · no apilar: una sin contestar frena la siguiente")
    await _limpiar()
    ayer = ahora - timedelta(days=1)
    await execute(
        "INSERT INTO sugerencias (room_id, kind, etiqueta, mbids, enviada_at) "
        "VALUES ($1,'estante','pendiente','{}',$2)", SALA, ayer)
    s, motivo = await sug.elegir(SALA, ignorar_hora=True)
    check(s is None and "sin contestar" in motivo,
          "con una pendiente reciente, no manda", motivo)

    print("\n10 · pasado el plazo de gracia, vuelve a intentar")
    await _limpiar()
    viejo = ahora - timedelta(days=sug.DIAS_SIN_APILAR + 2)
    await execute(
        "INSERT INTO sugerencias (room_id, kind, etiqueta, mbids, enviada_at) "
        "VALUES ($1,'estante','vieja','{}',$2)", SALA, viejo)
    s, motivo = await sug.elegir(SALA, ignorar_hora=True)
    check("sin contestar" not in motivo,
          "una ignorada vieja no calla al bot para siempre", motivo)

    print("\n10.5 · la guarda de escucha se mide en HORAS, no en dias")
    # La version anterior preguntaba por el dia entero. Contra los datos
    # reales —escucha completa los 6 de 6 dias medidos— eso silenciaba el
    # bloque TODOS los dias: H5 no habria mandado un solo mensaje y el log lo
    # habria explicado como correcto.
    await _limpiar()
    horas = settings.harness_sugerencia_gracia_horas
    hubo_hoy = await fetchval(
        "SELECT 1 FROM play_history WHERE completed AND started_at >= $1 LIMIT 1",
        ahora.replace(hour=0, minute=0, second=0, microsecond=0))
    hubo_reciente = await fetchval(sug.SQL_ESCUCHA_RECIENTE, ahora, horas)
    print(f"        escucha completa hoy: {bool(hubo_hoy)} · "
          f"en las ultimas {horas} h: {bool(hubo_reciente)}")
    if hubo_hoy and not hubo_reciente:
        s, motivo = await sug.elegir(SALA, ignorar_hora=True)
        check("escuchaste hace menos" not in motivo,
              "con escucha de hoy pero no reciente, SI manda", motivo)
    else:
        print("        (hoy no distingue los dos casos; el assert no aplica)")

    print("\n11 · silencio")
    await _limpiar()
    await sug.silenciar(SALA, ahora + timedelta(days=7))
    s, motivo = await sug.elegir(SALA, ignorar_hora=True)
    check(s is None and "silenciado" in motivo, "silenciado -> no manda", motivo)
    await sug.silenciar(SALA, ahora - timedelta(days=1))
    s, motivo = await sug.elegir(SALA, ignorar_hora=True)
    check("silenciado" not in motivo, "silencio vencido -> vuelve a mandar", motivo)

    print("\n12 · la poda corta sola un tipo con baja aceptacion")
    await _limpiar()
    n = settings.harness_sugerencia_min_envios
    for i in range(n):
        await execute(
            "INSERT INTO sugerencias (room_id, kind, etiqueta, mbids, aceptada, "
            "respondida_at) VALUES ($1,'__podable__',$2,'{}',false, now())",
            SALA, f"rechazada {i}")
    podados = await sug.tipos_podados()
    check("__podable__" in podados,
          f"un tipo con {n} rechazos queda podado", str(sorted(podados)))
    await execute("DELETE FROM sugerencias WHERE kind = '__podable__'")

    print("\n13 · material real disponible")
    estante = await fetchval(
        """
        SELECT count(*) FROM ephemerides e
        WHERE e.weight = 1 AND e.mbid IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM recordings rc
            JOIN play_history ph ON ph.recording_mbid = rc.mbid
            WHERE rc.release_mbid = e.mbid::uuid AND ph.completed
              AND ph.started_at > now() - make_interval(days => $1))
        """, settings.harness_sugerencia_estante_dias)
    print(f"        discos del estante sin escuchar: {estante}")
    check(estante > 0, "hay material en el estante para sugerir")

    efems = [r for r in await queries.efemerides_hoy(limite=8) if r.get("weight") == 1]
    print(f"        efemerides del estante hoy: {len(efems)}")
    for r in efems[:3]:
        print(f"          · {r['artist']} — {r['album']} ({r.get('anio')})")

    e = await goals.mas_atrasado(SALA if False else "telegram")
    if e:
        print(f"        objetivo mas atrasado: {e['kind']} "
              f"{e['actual']:.0f}/{e['target']:.0f} "
              f"(muestra {e['muestra']}, suficiente={e['suficiente']})")
        if not e["suficiente"]:
            print("        -> no se va a sugerir: muestra por debajo de "
                  f"MUESTRA_MINIMA ({goals.MUESTRA_MINIMA}). Correcto.")
    else:
        print("        sin objetivos pendientes")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
