"""comun/diagnostico.py — Por qué una consulta volvió vacía.

Un resultado vacío se venía informando siempre igual: "no encontró ningún caso que
cumpla esas condiciones en este censo. No es un problema de confidencialidad:
sencillamente no hay registros". Eso es una afirmación SUSTANTIVA sobre el país, y
es falsa cuando lo que pasó es que un filtro no coincide con ningún valor de su
variable. El caso que lo destapó: `MUNICIPIO_136 = 'B'` —la columna guarda
'MUNICIPIO B'— devolvía cero filas, y el sistema contestaba que no hay hombres
solteros de 40 a 47 con título universitario en el Municipio B. Había 628.

Es la misma familia que los dos bugs que ya se cerraron: el falso "confidencialidad"
(que era un conteo 0 suprimido) y el falso "no hay registros". En los tres el
sistema convertía una limitación propia en un hecho sobre el censo.

Cómo distingue: prueba cada filtro POR SEPARADO contra la base.

  - Si algún filtro por sí solo no coincide con ninguna fila, ese filtro es el
    culpable y se puede nombrar, con los valores que la variable sí tiene.
  - Si todos coinciden por separado, lo que está vacío es la COMBINACIÓN, y ahí
    "no hay casos" es verdad: no hay nada que corregir y el mensaje se mantiene.

Solo corre cuando el resultado ya salió vacío, así que no cuesta nada en el camino
normal. Ante cualquier error se devuelve None: el diagnóstico es una mejora del
mensaje, nunca un motivo para no responder.
"""
import sqlglot
from sqlglot import exp

from comun import ejecutor
from comun import nomenclator as nom

# Cuántos valores válidos se listan antes de resumir. Más que esto no ayuda a
# leer: la idea es que se vea de un vistazo qué se podía haber escrito.
MAX_VALORES = 12

# Hasta cuántos valores distintos se considera que una variable es CATEGÓRICA, o
# sea que tiene un dominio cerrado y escribir algo fuera de él es un error.
#
# Sin este límite el diagnóstico decía cualquier disparate sobre las variables
# continuas: "¿cuántas personas hay de 200 años?" salía como "el filtro PERNA01 =
# 200 no coincide con ningún valor de la variable", cuando ahí no hay ningún error
# que corregir —no hay nadie de 200 años, y eso es un hecho del censo, que es
# exactamente lo que el mensaje de siempre dice bien—. La edad tiene 113 valores
# distintos; los códigos censales rara vez pasan de 30.
LIMITE_DOMINIO = 60


def _tabla_de(columna, arbol):
    """Tabla real de una columna calificada (`p.MUNICIPIO_136`), o la principal."""
    alias = {}
    for t in arbol.find_all(exp.Table):
        real = t.name or ""
        alias[real.lower()] = real
        if t.alias:
            alias[t.alias.lower()] = real
    if columna.table:
        return alias.get(columna.table.lower(), columna.table)
    principal = arbol.find(exp.Table)
    return principal.name if principal is not None else None


def _comparaciones(arbol):
    """[(columna, tabla, condición SQL)] de cada filtro de igualdad del árbol.

    Solo igualdades y listas: son las que pueden no coincidir con NADA por estar
    escrito un valor que la variable no tiene. Un rango (BETWEEN, >=) no se mira
    porque un rango vacío es una pregunta legítima, no un error de escritura.
    """
    salida = []
    for cmp_ in list(arbol.find_all(exp.EQ)) + list(arbol.find_all(exp.In)):
        col = cmp_.this if isinstance(cmp_.this, exp.Column) else None
        if col is None:
            continue
        valores = ([cmp_.expression] if isinstance(cmp_, exp.EQ)
                   else list(cmp_.expressions))
        if not valores or not all(isinstance(v, exp.Literal) for v in valores):
            continue
        salida.append((col.name, _tabla_de(col, arbol), cmp_.sql(dialect="sqlite"),
                       [v.this for v in valores]))
    return salida


def _orden(par):
    """Por código numérico cuando lo es, alfabético cuando no. Los diccionarios los
    guardan como texto, y ordenar '10' antes que '2' se lee como un error."""
    codigo = str(par[0])
    return (0, int(codigo), "") if codigo.lstrip("-").isdigit() else (1, 0, codigo)


def _valores_validos(censo, variable, base=None, tabla=None):
    """Códigos y etiquetas que la variable sí tiene, para poder ofrecerlos.

    Si la variable no está en el diccionario de etiquetas —las de texto libre no lo
    están: MUNICIPIO_136 guarda nombres, no códigos— se leen los valores de la
    propia columna. Ese es justamente el caso que originó todo esto, así que dejarlo
    sin respuesta sería dejar el diagnóstico a medias.
    """
    try:
        etiquetas = [e for e in nom.catalogo(censo, nom.ETIQUETA)
                     if (e.variable or "").upper() == (variable or "").upper()]
    except Exception:                                    # noqa: BLE001
        etiquetas = []
    vistos, salida = set(), []
    for e in etiquetas:
        if e.codigo in vistos:
            continue
        vistos.add(e.codigo)
        salida.append((e.codigo, e.nombre))
    if salida or not (base and tabla):
        return sorted(salida, key=_orden)
    try:
        filas = ejecutor.tuplas(base, 'SELECT DISTINCT "%s" FROM %s WHERE "%s" IS NOT NULL'
                                      % (variable, tabla, variable))
    except Exception:                                    # noqa: BLE001
        return []
    return sorted([(f[0], "") for f in filas], key=_orden)


def filtro_sin_coincidencias(base, sql, censo):
    """El primer filtro que por sí solo no coincide con ninguna fila, o None.

    Devuelve un dict con la columna, lo escrito y los valores válidos, listo para
    redactar. None significa que no hay nada que señalar: o todos los filtros
    coinciden por separado (y lo vacío es la combinación, que es un hecho real), o
    no se pudo determinar.
    """
    try:
        arbol = sqlglot.parse_one(sql, read="sqlite")
    except Exception:                                    # noqa: BLE001
        return None
    if arbol is None:
        return None

    for columna, tabla, condicion, escritos in _comparaciones(arbol):
        if not tabla:
            continue
        try:
            hay = ejecutor.tuplas(
                base, "SELECT 1 FROM %s WHERE %s LIMIT 1" % (tabla, condicion))
        except Exception:                                # noqa: BLE001
            continue      # la sonda no se pudo hacer: no se afirma nada sobre ese filtro
        if hay:
            continue
        if not _es_categorica(base, tabla, columna, escritos):
            continue      # variable continua: el vacío es un hecho, no un error
        return {"columna": columna, "tabla": tabla, "condicion": condicion,
                "escritos": escritos,
                "validos": _valores_validos(censo, columna, base, tabla)}
    return None


def _es_categorica(base, tabla, columna, escritos):
    """¿La variable tiene dominio cerrado, o es continua?

    Un valor de texto fuera del dominio siempre es un error de escritura —nadie
    pregunta por el municipio 'B' esperando que no exista—, así que ahí no hace
    falta contar. Para los valores numéricos sí: son los que pueden ser tanto un
    código inventado (PEREC04 = 3, que no existe) como un punto legítimo de una
    escala (PERNA01 = 200). Los separa el tamaño del dominio.

    Ante la duda devuelve False: se queda con el mensaje de siempre, que afirma
    menos.
    """
    if any(not str(v).strip().lstrip("-").isdigit() for v in escritos):
        return True
    try:
        distintos = ejecutor.escalar(
            base, 'SELECT COUNT(DISTINCT "%s") FROM %s' % (columna, tabla))
    except Exception:                                    # noqa: BLE001
        return False
    return bool(distintos) and distintos <= LIMITE_DOMINIO


def _listar(validos):
    return "; ".join(("%s = %s" % (c, n)) if n else str(c) for c, n in validos)


def explicar(diag):
    """El diagnóstico en una frase, para agregar al mensaje de resultado vacío."""
    if not diag:
        return ""
    escritos = ", ".join("«%s»" % e for e in diag["escritos"])
    frase = ("El filtro `%s` no coincide con ningún valor de la variable %s: %s no %s "
             "entre los valores que esa variable tiene en este censo."
             % (diag["condicion"], diag["columna"], escritos,
                "está" if len(diag["escritos"]) == 1 else "están"))
    validos = diag["validos"]
    if not validos:
        return frase
    if len(validos) <= MAX_VALORES:
        return frase + " Los valores posibles son: %s." % _listar(validos)
    return frase + (" Esa variable tiene %d valores posibles; algunos son: %s."
                    % (len(validos), _listar(validos[:MAX_VALORES])))
