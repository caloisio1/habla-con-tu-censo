"""comun/mapa_resumen.py — El mapa RESUMEN cuando el desglose fino no se dibuja.

Un desglose por segmento censal a escala nacional son 3.756 unidades en 2023. No
entran en un mapa: a zoom de país un segmento —unas manzanas— no llega a un píxel,
y la cartografía de segmentos de 2023 son 19 archivos que suman 42 MB. Hasta ahora
eso se resolvía NO dibujando nada y explicándolo (el aviso de LIMITE_MAPA): la
tabla salía completa, pero el mapa —que es lo que la mayoría mira primero— no salía
nunca justo para las preguntas más finas.

Acá se dibuja el nivel que SÍ entra —sección censal o departamento, según de dónde
se venga—. El mapa deja de ser el desglose y pasa a ser su RESUMEN, y se dice.

Lo que importa es CÓMO se calcula ese resumen, porque la manera obvia está mal.
Sumar las filas ya publicadas da de menos: las celdas suprimidas por
confidencialidad (447 en el desglose por segmento de la población afro de 2023) ya
no están en esas filas, y lo que el LIMIT haya recortado tampoco. El mapa mostraría
cifras más chicas que la tabla y nadie se enteraría. Así que el resumen se
RECALCULA: se envuelve la consulta ya validada —sin su LIMIT— en una agregación por
el código más corto y se vuelve a ejecutar. La supresión se aplica DESPUÉS, sobre el
total de la sección, que es donde corresponde: una sección de 4.000 personas no es
una celda chica aunque se componga de segmentos que sí lo son.

Dos guardas, las dos fail-closed (antes sin resumen que con un resumen falso):

  - La métrica tiene que ser ADITIVA. Sumar conteos de personas da el conteo de la
    sección; sumar porcentajes no da nada. Se decide PARSEANDO la proyección
    (SUM/COUNT, con el ROUND o el CAST que la envuelva), no por el nombre de la
    columna: 'porcentaje' es una convención del prompt, no una garantía.
  - Los alias tienen que ser identificadores simples. Se arma SQL por concatenación
    sobre una consulta que el guard ya validó, y lo único que se le agrega son
    nombres de columna: se comprueba que lo sean.

Queda una imprecisión conocida y acotada: en 2023 la métrica es ROUND(SUM(w)) por
segmento y el resumen suma esos redondeos, en vez de volver a ponderar desde los
microdatos. El desvío es de hasta medio caso por segmento —unas pocas unidades sobre
miles—. Se prefiere así a propósito: el mapa cierra con la tabla publicada, que es
contra lo que el usuario lo va a comparar.
"""
import re

from comun import supresion

# Un identificador simple y nada más. Todo lo que no sea esto no se envuelve.
_ALIAS_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RX_LIMIT_FINAL = re.compile(r"\s+limit\s+\d+\s*$", re.I)

# 2023: geo_codigo es TEXTO y el nivel se lee del LARGO del código compuesto
# (2 depto, 4 depto+sección, 5 localidad, 7 depto+sección+segmento). Cortarlo es
# quedarse con el prefijo, y el prefijo CONSERVA los ceros a la izquierda, que es
# justo lo que se rompería si el código fuese numérico.
CORTES_2023 = {
    "segmento_2023":  ("seccion_2023", 4),
    "seccion_2023":   ("depto_2023",   2),
    "localidad_2023": ("depto_2023",   2),
}

# Históricos y 2011: geo_codigo es NUMÉRICO (dpto*100000 + secc*1000 + segm), así
# que el corte es una división entera, no un prefijo.
CORTES_HIST = {
    "segmento":    ("seccion",      1000),
    "seccion":     ("departamento",  100),
}

# Cómo se nombra cada nivel en el aviso al usuario.
NIVEL_TXT = {
    "depto_2023": "departamento", "seccion_2023": "sección censal",
    "localidad_2023": "localidad", "segmento_2023": "segmento censal",
    "barrio_2023": "barrio",
    "departamento": "departamento", "seccion": "sección censal",
    "barrio": "barrio", "barrio_hist": "barrio", "ccz": "CCZ",
    "segmento_1996": "segmento censal", "segmento_2004": "segmento censal",
}


NIVEL_PLURAL = {
    "departamento": "departamentos", "sección censal": "secciones censales",
    "localidad": "localidades", "segmento censal": "segmentos censales",
    "barrio": "barrios", "CCZ": "CCZ", "unidad": "unidades",
}


def nombre_nivel(nivel):
    return NIVEL_TXT.get(nivel, "unidad")


def plural_nivel(nivel):
    n = nombre_nivel(nivel)
    return NIVEL_PLURAL.get(n, n + "s")


def corte_2023(nivel):
    """(nivel_destino, ancho_del_prefijo) o None si ese nivel no se resume."""
    return CORTES_2023.get(nivel)


def corte_hist(nivel):
    """(nivel_destino, divisor) o None. 'segmento_1996'/'segmento_2004' comparten
    corte: el código se compone igual en los cuatro censos."""
    if nivel and nivel.startswith("segmento_"):
        return CORTES_HIST["segmento"]
    return CORTES_HIST.get(nivel)


def es_aditiva(sql, alias):
    """¿La columna `alias` de la proyección RAÍZ se puede sumar entre unidades?

    Sí para SUM y COUNT —el conteo de una sección es la suma de los de sus
    segmentos—; no para un porcentaje, un promedio o una razón, donde sumar da un
    número sin significado. ROUND/CAST/COALESCE se atraviesan porque envuelven sin
    cambiar la aditividad de lo que hay adentro.

    Fail-closed: si el SQL no parsea, si el alias no está en la raíz o si la
    expresión es cualquier otra cosa, la respuesta es NO.
    """
    if not alias:
        return False
    try:
        import sqlglot
        from sqlglot import exp
        raiz = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return False
    if not isinstance(raiz, exp.Select):
        return False
    for proj in raiz.expressions:
        nombre = proj.alias if isinstance(proj, exp.Alias) else getattr(proj, "name", "")
        if (nombre or "").lower() != alias.lower():
            continue
        interno = proj.this if isinstance(proj, exp.Alias) else proj
        for _ in range(6):    # tope: una envoltura patológica no cuelga el pedido
            if isinstance(interno, (exp.Round, exp.Cast, exp.Coalesce, exp.Paren)):
                interno = interno.this
                continue
            break
        return isinstance(interno, (exp.Sum, exp.Count))
    return False


def _sin_limit(sql):
    return _RX_LIMIT_FINAL.sub("", (sql or "").strip().rstrip(";"))


def _columnas_a_sumar(valor_key, columnas_conteo):
    """El valor pintado más los conteos crudos que deciden la supresión, sin
    repetir: cuando la métrica ES el conteo (COUNT(*) AS personas) son la misma
    columna, y emitirla dos veces daría dos columnas con el mismo nombre."""
    return list(dict.fromkeys([valor_key] + list(columnas_conteo or [])))


def envolver(sql_seguro, geo_key, valor_key, columnas_conteo, corte, numerico):
    """SQL que agrega la consulta validada al nivel más grueso, o None.

    `corte` es el ancho del prefijo (2023, códigos de texto) o el divisor
    (históricos, códigos numéricos), según `numerico`.
    """
    columnas = _columnas_a_sumar(valor_key, columnas_conteo)
    if not _ALIAS_OK.match(geo_key or "") or not all(_ALIAS_OK.match(c or "") for c in columnas):
        return None
    if numerico:
        # CAST(... / N AS BIGINT) y no '/' a secas: en SQLite la división de
        # enteros ya es entera y en DuckDB da coma flotante. Es una de las
        # divergencias MUDAS entre los dos motores —misma consulta, otro
        # resultado, sin error—, y acá se cierra escribiendo el truncamiento.
        # Los códigos geográficos son positivos, así que truncar es piso.
        clave = 'CAST(CAST("%s" AS BIGINT) / %d AS BIGINT)' % (geo_key, corte)
    else:
        clave = 'SUBSTR(CAST("%s" AS VARCHAR), 1, %d)' % (geo_key, corte)
    sumas = ", ".join('SUM("%s") AS "%s"' % (c, c) for c in columnas)
    # Sin el LIMIT del interior: el resumen tiene que ver TODAS las unidades, que
    # es la razón de ser de recalcularlo en vez de sumar lo ya publicado.
    return ('SELECT %s AS "%s", %s FROM (%s) AS _fino GROUP BY 1'
            % (clave, geo_key, sumas, _sin_limit(sql_seguro)))


def resumir(sql_seguro, nivel, geo_key, valor_key, columnas_conteo, ejecutar,
            limite, numerico, clave_de=None, excluir=()):
    """Devuelve (mapa_resumen, suprimidas) o (None, 0).

    `ejecutar(sql) -> filas` inyecta el acceso a la base (así esto se prueba sin
    base). `clave_de(codigo, nivel)` traduce el código agregado a la clave que
    espera el GeoJSON —en los históricos el departamento se pinta por NOMBRE— y
    devuelve None si ese código no tiene polígono, en cuyo caso no se dibuja nada.

    Se sube de nivel las veces que haga falta: si el resumen por sección todavía
    no entrara, se prueba por departamento. En la práctica alcanza con un paso
    (231 secciones), pero el que decide es el resultado, no el pronóstico.

    El corte SIEMPRE se aplica sobre el código original, no sobre el del paso
    anterior: en texto porque el ancho del prefijo es absoluto (4 sección, 2
    departamento), y en numérico porque los divisores se COMPONEN (segmento ->
    sección es /1.000 y sección -> departamento otro /100: del segmento al
    departamento son /100.000, no /100).
    """
    if not es_aditiva(sql_seguro, valor_key):
        return None, 0            # un mapa de porcentajes sumados sería falso
    tabla_cortes = corte_hist if numerico else corte_2023
    actual, origen, divisor = nivel, nivel, 1
    for _ in range(len(CORTES_2023) + len(CORTES_HIST)):
        paso = tabla_cortes(actual)
        if not paso:
            return None, 0        # ya no hay nivel más grueso que probar
        destino, corte = paso
        divisor *= corte
        sql = envolver(sql_seguro, geo_key, valor_key, columnas_conteo,
                       divisor if numerico else corte, numerico)
        if sql is None:
            return None, 0
        try:
            filas = ejecutar(sql)
        except Exception:
            return None, 0        # el resumen es un extra: nunca rompe la respuesta
        filas, suprimidas, _vacias = supresion.suprimir_celdas_chicas(
            filas, columnas_conteo)
        # Códigos que el mapa fino DESCARTA sin dejar de dibujarse (la zona
        # contestada de Rincón de Artigas). Se descartan también acá y en el mismo
        # sentido: el prefijo de un código excluido sigue siendo un código excluido.
        filas = [f for f in filas if str(f.get(geo_key)) not in set(excluir)]
        if not filas:
            return None, 0
        if len(filas) > limite:
            actual = destino      # todavía no entra: se sube un nivel más
            continue
        datos = []
        for f in filas:
            cod = f.get(geo_key)
            if cod is None:
                return None, 0
            clave = clave_de(cod, destino) if clave_de else str(cod)
            if clave is None:
                return None, 0    # sin polígono: no se dibuja NADA, como en el fino
            datos.append({"clave": clave, "valor": f.get(valor_key)})
        if not datos:
            return None, 0
        return {"nivel": destino, "datos": datos, "suprimidas": suprimidas,
                "resumen": {"de": nombre_nivel(origen), "a": nombre_nivel(destino)}}, suprimidas
    return None, 0


def aviso(unidades, nivel_fino, nivel_resumen=None, motivo="escala"):
    """El texto que acompaña la respuesta.

    Ninguna variante menciona un tope: un número de corte no le dice nada a nadie y
    encima se confunde con el viejo límite de FILAS, que era un bug y ya no existe.
    Lo que se explica es POR QUÉ el desglose no se dibuja, y son dos razones
    distintas: no se distingue a esa escala ('escala') o la cartografía de segmentos
    —reconstruida desde las planchas del INE— no cubre todo ('cartografia').

    Y se dice que las cifras del resumen se recalculan, porque no coinciden con la
    suma de la tabla y alguien la va a sumar: la tabla no publica las celdas de menos
    de cinco casos y el total de la sección sí las incluye. Las dos cifras están
    bien, pero solo si se sabe qué es cada una.
    """
    fino, n = nombre_nivel(nivel_fino), "{:,}".format(unidades).replace(",", ".")
    if motivo == "cartografia":
        cabeza = ("\n\n_Nota: la cartografía de %s de este censo se reconstruyó desde "
                  "las planchas del INE y no cubre todos los del desglose (%s), así "
                  "que " % (plural_nivel(nivel_fino), n))
    else:
        cabeza = ("\n\n_Nota: el desglose por %s tiene %s unidades y a esa escala no se "
                  "distinguen en un mapa, así que " % (fino, n))
    if nivel_resumen:
        grueso = nombre_nivel(nivel_resumen)
        return (cabeza + "el mapa se dibuja por %s. Es un resumen: cada %s muestra su "
                "total recalculado sobre TODOS sus %s, incluidos los que la tabla no "
                "publica por confidencialidad. El detalle por %s está en la tabla._"
                % (grueso, grueso, plural_nivel(nivel_fino), fino))
    return (cabeza + "no se dibuja ninguno. El detalle está en la tabla; acotá la "
            "pregunta a un ámbito menor —un departamento o una sección— para ver "
            "también el mapa._")
