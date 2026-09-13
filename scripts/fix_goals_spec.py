"""Repara los `goals.spec` que quedaron doble-encodeados.

`goals.declarar` pasaba `json.dumps(spec)` a un parametro jsonb, y
`db.init_pool` registra un codec que serializa por su cuenta: el resultado es
un string JSON guardado dentro de un jsonb.

    id  jsonb_typeof(spec)  spec->>'target'
    6   object              5        <- bien
    11  string              NULL     <- roto, y estaba ACTIVO

El codigo Python no se dio cuenta porque `goals._spec()` desenvuelve el str
antes de usarlo. Lo que se rompe en silencio es cualquier lectura desde SQL:
`spec->>'target'` devuelve NULL y el objetivo pasa a tener un target sin
sentido — la misma clase de falla muda que el bug original del codec.

El insert ya no lo hace. Esto arregla lo que quedo guardado.

Idempotente: desenvuelve solo lo que este como string.

Uso:  python -m scripts.fix_goals_spec
"""
import asyncio
import logging

from app.db import close_pool, fetch, execute

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fix-goals")

# jsonb_typeof = 'string' es exactamente un spec doble-encodeado: un spec sano
# es 'object'. `#>> '{}'` saca el texto del string JSON sin tocar los objetos,
# y el cast lo vuelve a parsear al jsonb que deberia haber sido siempre.
ARREGLO = """
UPDATE goals
SET spec = (spec #>> '{}')::jsonb
WHERE jsonb_typeof(spec) = 'string'
"""


async def main() -> None:
    try:
        await _arreglar()
    finally:
        await close_pool()


async def _arreglar() -> None:
    antes = await fetch(
        "SELECT id, kind, active, jsonb_typeof(spec) AS tipo, "
        "       spec->>'target' AS target "
        "FROM goals ORDER BY id")
    rotos = [r for r in antes if r["tipo"] != "object"]

    for r in antes:
        log.info("  id=%-3s %-15s active=%-5s tipo=%-7s target=%s",
                 r["id"], r["kind"], r["active"], r["tipo"], r["target"])

    if not rotos:
        log.info("nada que arreglar: los %d objetivos tienen spec como object",
                 len(antes))
        return

    log.warning("%d de %d objetivos con spec doble-encodeado (%d activos)",
                len(rotos), len(antes), sum(1 for r in rotos if r["active"]))

    res = await execute(ARREGLO)
    log.info("arreglo: %s", res)

    desp = await fetch(
        "SELECT id, kind, active, jsonb_typeof(spec) AS tipo, "
        "       spec->>'target' AS target "
        "FROM goals ORDER BY id")
    malos = [r for r in desp if r["tipo"] != "object" or r["target"] is None]

    for r in desp:
        log.info("  id=%-3s %-15s active=%-5s tipo=%-7s target=%s",
                 r["id"], r["kind"], r["active"], r["tipo"], r["target"])

    if malos:
        log.error("quedaron %d objetivos sin target legible desde SQL: %s",
                  len(malos), [r["id"] for r in malos])
    else:
        log.info("los %d objetivos responden a spec->>'target' desde SQL",
                 len(desp))


if __name__ == "__main__":
    asyncio.run(main())
