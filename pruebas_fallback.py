"""pruebas_fallback.py — ¿Cuánto se usa realmente la red de SQLite?

POR QUÉ EXISTE. pruebas_ordinal.py midió UNA familia de preguntas (los tramos de
edad) y dio 0 % de caídas después del arreglo. Ese número no autoriza a concluir
que la red de SQLite ya no hace falta: el disparador de fondo —una expresión en
el GROUP BY o en el ORDER BY que DuckDB no acepta— puede aparecer en otras
formas de consulta que aquel banco no tocaba. Este mide el abanico.

QUÉ CUENTA. Por cada pasada: si la resolvió DuckDB nativo, si DuckDB la rechazó
y hubo que repetirla en SQLite (CAE_SQLITE), si se derivó a SQLite a propósito
(LIKE/UPPER), o si los dos motores devolvieron cifras distintas.

CÓMO. Igual que el otro banco: el SQL lo escribe el modelo de verdad, lo valida
el guard de verdad y lo ejecuta el camino de producción. El veredicto sale de
los contadores del ejecutor, no de una heurística sobre el texto del SQL.

NO_RESPONDIBLE no es un fallo: es el sistema diciendo que la variable no está en
ese censo. Se cuenta aparte para no ensuciar la tasa.

Uso:  ./venv/bin/python pruebas_fallback.py [repeticiones] [censo]
"""
import sys
import time

from comun import ejecutor
from pruebas_ordinal import DB, contadores, guard_de, motor, normalizar, por_sqlite

# Formas de consulta, no preguntas sueltas: cada una ejercita una construcción
# SQL distinta. La etiqueta es la que aparece en el informe por forma.
FORMAS = [
    ("conteo",       "¿Cuántas personas hay en Uruguay?"),
    ("por depto",    "¿Cuántas personas viven en cada departamento?"),
    ("por sexo",     "¿Cuál es la distribución de la población por sexo?"),
    ("tramos",       "¿Cómo se distribuye la población por tramo quinquenal de edad?"),
    ("decenal",      "¿Cuántas personas hay en cada grupo de edad de 10 en 10 años?"),
    ("umbral",       "¿Cuántas personas de 65 años y más hay en cada departamento?"),
    ("porcentaje",   "¿Qué porcentaje de la población es mujer en cada departamento?"),
    ("top-n",        "¿Cuáles son los 5 departamentos con más población?"),
    ("localidad",    "¿Cuántas personas viven en Paso de los Toros?"),
    ("barrio mvd",   "¿Cuántas personas viven en cada barrio de Montevideo?"),
    ("seccion",      "¿Cuántas personas hay por sección censal en Salto?"),
    ("cruce",        "¿Cómo se distribuye la población por sexo en cada departamento?"),
    ("educacion",    "¿Cómo se distribuye la población por nivel educativo?"),
    ("hogares",      "¿Cuántos hogares hay en cada departamento?"),
    ("viviendas",    "¿Cuántas viviendas hay en Uruguay?"),
]


def una_pasada(censo, pregunta):
    """Genera, valida y ejecuta una vez. Devuelve (veredicto, segundos, sql, detalle)."""
    gen = motor(censo)[0]
    validar = guard_de(censo)
    db = DB[censo]
    try:
        sql_crudo = gen(pregunta)
    except Exception as e:
        return "ERROR_MODELO", 0.0, "", str(e)[:80]

    # El motor puede declarar que la pregunta no se responde con este censo. No
    # es un fallo del fallback y no debe contaminar la tasa.
    if sql_crudo.strip().startswith("NO_RESPONDIBLE"):
        return "NO_RESPONDIBLE", 0.0, sql_crudo, ""

    try:
        sql = validar(sql_crudo)[0]
    except Exception as e:
        return "RECHAZO_GUARD", 0.0, sql_crudo, str(e)[:80]

    d0, c0 = contadores(db)
    t0 = time.time()
    try:
        filas = ejecutor.filas(db, sql)
    except Exception as e:
        return "ERROR_EJEC", time.time() - t0, sql, str(e)[:80]
    seg = time.time() - t0
    d1, c1 = contadores(db)

    if c1 > c0:
        return "CAE_SQLITE", seg, sql, "DuckDB la rechazo (reintento)"
    if d1 > d0:
        return "DERIVADA", seg, sql, "LIKE/UPPER: a SQLite a proposito"

    # Control de cifras: el mismo SQL en SQLite tiene que dar lo mismo. Es lo
    # que destapó las divergencias mudas (precedencia de ||, division entera).
    try:
        a, b = normalizar(filas), normalizar(por_sqlite(db, sql))
        if a != b:
            i = next((k for k, (x, y) in enumerate(zip(a, b)) if x != y), None)
            det = ("fila %s: %r vs %r" % (i, a[i], b[i])) if i is not None \
                  else "largos %d vs %d" % (len(a), len(b))
            return "CIFRA_DISTINTA", seg, sql, det
    except Exception as e:
        return "NATIVO", seg, sql, "SQLite no pudo correr el control: %s" % str(e)[:40]
    return "NATIVO", seg, sql, ""


def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    solo = sys.argv[2] if len(sys.argv) > 2 else None
    censos = [solo] if solo else ["1996", "2004", "2011", "2023"]
    print("MEDICION AMPLIA DE LA RED DE SQLITE — %d formas x %d repeticiones x %d censos = %d pasadas\n"
          % (len(FORMAS), reps, len(censos), len(FORMAS) * reps * len(censos)))

    tot, por_forma, por_censo, incidentes = {}, {}, {}, []
    for censo in censos:
        for etiqueta, preg in FORMAS:
            for _ in range(reps):
                v, seg, sql, det = una_pasada(censo, preg)
                tot[v] = tot.get(v, 0) + 1
                por_forma.setdefault(etiqueta, {})[v] = por_forma.setdefault(etiqueta, {}).get(v, 0) + 1
                por_censo.setdefault(censo, {})[v] = por_censo.setdefault(censo, {}).get(v, 0) + 1
                marca = "" if v == "NATIVO" else "  <- " + det
                print("  %-5s %-12s %-14s %6.2fs%s" % (censo, etiqueta, v, seg, marca), flush=True)
                if v in ("CAE_SQLITE", "CIFRA_DISTINTA", "DERIVADA"):
                    incidentes.append((censo, etiqueta, v, det, " ".join(sql.split())))

    n = sum(tot.values())
    print("\n" + "=" * 78)
    print("RESUMEN sobre %d pasadas" % n)
    for v in sorted(tot, key=lambda k: -tot[k]):
        print("   %-16s %4d   (%5.1f%%)" % (v, tot[v], 100.0 * tot[v] / n))

    utiles = n - tot.get("NO_RESPONDIBLE", 0)
    red = tot.get("CAE_SQLITE", 0) + tot.get("DERIVADA", 0)
    print("\n   LA RED DE SQLITE SE USÓ EN %d de %d pasadas respondibles (%.1f%%)"
          % (red, utiles, 100.0 * red / utiles if utiles else 0))

    print("\nPOR CENSO")
    for c, d in por_censo.items():
        s = sum(d.values())
        print("   %-6s nativo %d/%d | cae %d | derivada %d | cifra distinta %d | no respondible %d"
              % (c, d.get("NATIVO", 0), s, d.get("CAE_SQLITE", 0),
                 d.get("DERIVADA", 0), d.get("CIFRA_DISTINTA", 0), d.get("NO_RESPONDIBLE", 0)))

    print("\nFORMAS QUE DIERON PROBLEMA")
    hubo = False
    for f, d in por_forma.items():
        mal = d.get("CAE_SQLITE", 0) + d.get("CIFRA_DISTINTA", 0) + d.get("DERIVADA", 0)
        if mal:
            hubo = True
            print("   %-12s %d de %d pasadas" % (f, mal, sum(d.values())))
    if not hubo:
        print("   ninguna")

    if incidentes:
        print("\nDETALLE DE LOS INCIDENTES")
        for c, f, v, det, sql in incidentes:
            print("   [%s/%s] %s — %s\n      %s" % (c, f, v, det, sql[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
