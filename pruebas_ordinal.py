"""pruebas_ordinal.py — Banco A/B de la regla de agrupación por ordinal.

QUE MIDE. El modelo a veces agrupa por una expresion DISTINTA de la que proyecta
(agrupa por `(edad/5)*5` y proyecta la etiqueta `'0-4'`). SQLite lo acepta porque
es permisivo; DuckDB pide el SQL estandar y la consulta cae a SQLite: correcta,
pero decenas de veces mas lenta. Este banco cuenta cuantas veces pasa, ANTES y
DESPUES de agregarle la regla al prompt.

COMO LO MIDE. No simula: genera el SQL con el modelo de verdad, lo pasa por el
guard de verdad y lo ejecuta por el camino de produccion (comun/ejecutor). El
veredicto sale de los contadores del propio ejecutor, no de una heuristica.

POR QUE REPITE. El SQL lo escribe un modelo y no es determinista: la MISMA
pregunta a veces sale bien y a veces mal. Una sola pasada no mide nada. Por eso
cada pregunta se genera N veces y lo que se reporta es una tasa.

CONTROL DE CIFRAS. Mientras existan las bases .db, cada resultado se compara fila
por fila contra el que devuelve SQLite con el mismo SQL. Es un control de
LABORATORIO, no parte del sistema: si esas bases ya no estan, se saltea y el resto
del banco sigue midiendo. La validacion que queda es la de bateria_censos.py,
contra las cifras publicadas por el INE.

Uso:  ./venv/bin/python pruebas_ordinal.py [repeticiones]
"""
import os
import sys
import time

from comun import ejecutor

DB = {"1996": "datos/censo1996.db", "2004": "datos/censo2004.db",
      "2011": "datos/censo.db", "2023": "datos/censo2023.db"}

# Preguntas que EMPUJAN al modelo a agrupar por una expresion derivada: tramos,
# rangos y categorias construidas con CASE. No son preguntas cualquiera: son el
# terreno donde el defecto aparece.
PREGUNTAS = [
    ("1996", "¿Cómo se distribuye la población por tramo quinquenal de edad?"),
    ("1996", "¿Cuántas personas hay en cada grupo de edad de 10 en 10 años?"),
    ("2004", "¿Cómo se distribuye la población por tramo quinquenal de edad?"),
    ("2004", "¿Cuántas personas hay por grupo de edad de 10 en 10 años?"),
    ("2011", "¿Cómo se distribuye la población por tramo quinquenal de edad?"),
    ("2011", "¿Cuántas personas hay en cada grupo decenal de edad?"),
    ("2023", "¿Cómo se distribuye la población por tramo quinquenal de edad?"),
    ("2023", "¿Cuántas personas hay en cada grupo de edad de 10 en 10 años?"),
]


def motor(censo):
    """Devuelve (generar_sql, guard) del censo, como los usa produccion."""
    if censo == "2023":
        import consultar_2023 as m
        return m.generar_sql, m.GUARD if hasattr(m, "GUARD") else None
    if censo == "2011":
        from app import main as m
        return m.generar_sql, None
    mod = __import__("consultar_%s" % censo)
    return mod._motor.generar_sql, mod._motor.guard


def guard_de(censo):
    """Devuelve la funcion validar() del censo. Los cuatro guards tienen la misma
    interfaz pero no la misma forma: 2011 y 2023 la exponen a nivel de modulo, los
    historicos como metodo de una instancia por censo."""
    if censo == "2023":
        import sql_guard_2023 as g
        return g.validar
    if censo == "2011":
        from app.sql_guard import validar
        return validar
    mod = __import__("consultar_%s" % censo)
    return mod._motor.guard.validar


def por_sqlite(db, sql):
    """Control cruzado contra la base SQLite, o None si esa base ya no está.

    ERA el patrón: mientras hubo dos motores, comparar fila por fila fue lo que
    destapó las divergencias mudas -precedencia del ||, división entera-. Con un
    solo motor el control pierde su razón de ser: comparar DuckDB contra DuckDB
    no prueba nada, y las .db se generaban con el MISMO cargar.py, así que un
    error de carga se copiaba idéntico a las dos y ninguna comparación lo veía.

    Devuelve None en vez de fallar para que el banco siga corriendo cuando las
    bases ya no existan; quien llama distingue "no hay control" de "el control
    pasó". La validación que queda es la que importa: las cifras ancla de
    bateria_censos.py, contrastadas contra los totales publicados por el INE."""
    if not os.path.exists(db):
        return None
    import sqlite3
    cx = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        return cx.execute(sql).fetchall()
    finally:
        cx.close()


def normalizar(filas):
    """Lleva las dos formas a una sola para poder compararlas.

    ejecutor.filas() devuelve dicts {columna: valor}; sqlite3.fetchall() devuelve
    tuplas. Comparar los dos crudos da SIEMPRE distinto aunque las cifras calcen
    — es lo que ensucio la primera corrida. Se comparan los VALORES en orden de
    columna, y los numeros enteros escritos como float (DuckDB devuelve 73377.0
    donde SQLite devuelve 73377) se llevan a int.
    """
    def val(v):
        if isinstance(v, float) and v == int(v):
            return int(v)
        try:                      # Decimal, que DuckDB usa en algunos AVG
            from decimal import Decimal
            if isinstance(v, Decimal):
                return int(v) if v == int(v) else float(v)
        except Exception:
            pass
        return v

    out = []
    for f in filas:
        vals = f.values() if isinstance(f, dict) else f
        out.append(tuple(val(v) for v in vals))
    return out


def contadores(db):
    """(ambiguas, rechazos) de esa base, según el propio ejecutor.

    Antes eran (derivadas, caidas): consultas mandadas a SQLite a propósito y
    consultas repetidas en SQLite tras un fallo. Ya no hay a dónde caer, así que
    las mismas dos situaciones ahora son rechazos y el veredicto se lee igual."""
    e = ejecutor.estado()
    import os
    k = os.path.abspath(db)
    return (e.get("ambiguas", {}).get(k, 0), e.get("rechazos", {}).get(k, 0))


def una_pasada(censo, pregunta):
    """Genera, valida y ejecuta. Devuelve (veredicto, segundos, sql, detalle)."""
    gen = motor(censo)[0]
    validar = guard_de(censo)
    db = DB[censo]
    try:
        sql_crudo = gen(pregunta)
    except Exception as e:
        return "ERROR_MODELO", 0.0, "", str(e)[:80]
    try:
        sql = validar(sql_crudo)[0]
    except Exception as e:
        return "RECHAZO_GUARD", 0.0, sql_crudo, str(e)[:80]

    d0, c0 = contadores(db)
    t0 = time.time()
    try:
        filas = ejecutor.filas(db, sql)
    except Exception as e:
        # Sin red, lo que antes era un reintento silencioso ahora es una
        # excepcion. Los contadores del ejecutor dicen POR QUE.
        seg = time.time() - t0
        d1, c1 = contadores(db)
        if c1 > c0:
            return "RECHAZADA", seg, sql, "DuckDB no pudo: %s" % str(e)[:60]
        if d1 > d0:
            return "AMBIGUA", seg, sql, "LIKE/UPPER: rechazada a proposito"
        return "ERROR_EJEC", seg, sql, str(e)[:80]
    seg = time.time() - t0

    # ¿la cifra es la misma que da SQLite con el mismo SQL? Solo si esa base
    # todavia existe: es un control de laboratorio, no parte del sistema.
    esperado = por_sqlite(db, sql)
    if esperado is None:
        return "NATIVO", seg, sql, ""
    a, b = normalizar(filas), normalizar(esperado)
    if a != b:
        dif = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), None)
        det = "fila %s: %r vs %r" % (dif, a[dif], b[dif]) if dif is not None \
              else "largos %d vs %d" % (len(a), len(b))
        return "CIFRA_DISTINTA", seg, sql, det
    return "NATIVO", seg, sql, ""


def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print("BANCO A/B — agrupacion por ordinal   (%d repeticiones por pregunta)\n" % reps)
    print("  %-5s %-52s %-14s %8s" % ("censo", "pregunta", "veredicto", "seg"))
    print("  " + "-" * 88)
    solo = sys.argv[2] if len(sys.argv) > 2 else None   # correr un solo censo
    tot = {}
    lentas = []
    divergentes = []
    for censo, preg in PREGUNTAS:
        if solo and censo != solo:
            continue
        for i in range(reps):
            v, seg, sql, det = una_pasada(censo, preg)
            tot[v] = tot.get(v, 0) + 1
            marca = "" if v == "NATIVO" else "  <- " + det
            print("  %-5s %-52s %-14s %8.2f%s" % (censo, preg[:52] if i == 0 else "", v, seg, marca))
            if v in ("RECHAZADA", "AMBIGUA", "ERROR_EJEC"):
                lentas.append((censo, preg, seg, sql))
            if v == "CIFRA_DISTINTA":
                divergentes.append((censo, preg, det, sql))
    print("\n  RESUMEN sobre %d pasadas:" % sum(tot.values()))
    for v in sorted(tot, key=lambda k: -tot[k]):
        print("    %-15s %3d   (%4.1f%%)" % (v, tot[v], 100.0 * tot[v] / sum(tot.values())))
    if lentas:
        print("\n  las que NO se pudieron responder:")
        for c, p, s, sql in lentas:
            print("    %s · %s · %.2fs" % (c, p[:44], s))
            print("      " + " ".join(sql.split())[:150])
    if divergentes:
        print("\n  las que dieron CIFRA DISTINTA entre motores:")
        for c, p, det, sql in divergentes:
            print("    %s · %s\n      %s" % (c, p[:44], det))
            print("      SQL: " + " ".join(sql.split()))
    return 1 if (tot.get("CIFRA_DISTINTA") or tot.get("RECHAZADA")) else 0


if __name__ == "__main__":
    sys.exit(main())
