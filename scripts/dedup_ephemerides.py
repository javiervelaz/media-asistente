"""Deduplica `ephemerides` por mbid y mueve la clave natural al identificador.

El problema no era la carga, era la clave. `hydrator.hydrate_artist` hacia:

    ON CONFLICT (artist, album) DO UPDATE ...
      weight = LEAST(ephemerides.weight, EXCLUDED.weight)

La regla de merge estaba bien; la clave estaba mal. `UNIQUE (artist, album)`
compara TITULOS, asi que "Back in Black" y "Back In Black" son dos entradas
distintas del mismo disco:

    d3bc1a64-...  AC/DC  Back in Black  w=1  07-25
    d3bc1a64-...  AC/DC  Back In Black  w=1  07-25

282 filas sobrantes sobre 9.626, y 281 mbids repetidos. El dano no es el
espacio: `weight = 1` es la definicion de "esta en el estante", asi que un
disco contado dos veces sesga el ratio del objetivo de coleccion y el
termino de procedencia del scoring de `local_search`.

Ademas 42 mbids tenian `weight = 1` y otro peso a la vez (el mismo disco en
el estante y en el canon). Se conserva el MENOR, que es la procedencia mas
fuerte y lo que ya hacia el `LEAST` del upsert.

Que hace, en orden:
  1. borra los duplicados por mbid, conservando min(weight) y luego min(id);
  2. crea `UNIQUE (mbid)` — sin esto el arreglo se deshace en la proxima
     hidratacion;
  3. deja `UNIQUE (artist, album)` como estaba: el hydrator ya no la usa
     como conflict target, pero sacarla es una decision aparte.

Idempotente. Uso:  python -m scripts.dedup_ephemerides
"""
import asyncio
import logging

from app.db import close_pool, execute, fetchrow

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dedup")

CONTEO = """
SELECT count(*)                                   AS filas,
       count(DISTINCT mbid)                       AS mbids,
       count(*) FILTER (WHERE weight = 1)         AS vinilo,
       count(DISTINCT mbid) FILTER (WHERE weight = 1) AS vinilo_mbids,
       count(*) FILTER (WHERE mbid IS NULL)       AS sin_mbid
FROM ephemerides
"""

# min(weight) primero: 1 (vinilo) le gana a 2 (canon) y a 3 (descubierto),
# que es la misma regla del LEAST() del upsert. min(id) desempata por la
# fila mas vieja, que es la que tiene el created_at original.
DEDUP = """
DELETE FROM ephemerides e
USING (
  SELECT mbid,
         (array_agg(id ORDER BY weight, id))[1] AS conservar
  FROM ephemerides
  WHERE mbid IS NOT NULL
  GROUP BY mbid
  HAVING count(*) > 1
) d
WHERE e.mbid = d.mbid AND e.id <> d.conservar
"""

# El peso que sobrevive tiene que ser el menor de los que habia, no el de la
# fila que quedo: el DELETE de arriba ya conserva la de menor peso, pero si
# alguien corre el script sobre datos a medio arreglar esto lo deja firme.
INDICE = "CREATE UNIQUE INDEX IF NOT EXISTS ephemerides_mbid_key ON ephemerides (mbid)"


async def main() -> None:
    try:
        await _dedup()
    finally:
        await close_pool()


async def _dedup() -> None:
    antes = await fetchrow(CONTEO)
    log.info("antes:   %s filas / %s mbids | vinilo: %s filas / %s mbids | sin mbid: %s",
             antes["filas"], antes["mbids"], antes["vinilo"],
             antes["vinilo_mbids"], antes["sin_mbid"])

    if antes["sin_mbid"]:
        log.warning("%s filas sin mbid: no se tocan, y el UNIQUE las permite "
                    "repetidas (un indice unico admite varios NULL)",
                    antes["sin_mbid"])

    res = await execute(DEDUP)
    log.info("dedup: %s", res)

    try:
        await execute(INDICE)
        log.info("ok: UNIQUE (mbid)")
    except Exception as e:
        log.error("no pude crear UNIQUE (mbid): %s", e)
        log.error("queda un duplicado sin resolver; revisa con: "
                  "SELECT mbid, count(*) FROM ephemerides GROUP BY 1 "
                  "HAVING count(*) > 1")
        return

    desp = await fetchrow(CONTEO)
    log.info("despues: %s filas / %s mbids | vinilo: %s filas / %s mbids",
             desp["filas"], desp["mbids"], desp["vinilo"], desp["vinilo_mbids"])

    if desp["vinilo"] != desp["vinilo_mbids"]:
        log.error("el estante sigue con filas repetidas: %s filas para %s mbids",
                  desp["vinilo"], desp["vinilo_mbids"])
    else:
        log.info("el estante quedo en %s discos, uno por mbid", desp["vinilo"])


if __name__ == "__main__":
    asyncio.run(main())
