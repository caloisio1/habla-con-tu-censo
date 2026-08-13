"""
sql_guard.py — Validación de SQL generado por el LLM sobre MICRODATOS del censo,
con un PARSER REAL (sqlglot). Reemplaza el guard v3 basado en regex.

Principio: si una consulta no puede verificarse como segura, NO se ejecuta.

La tabla `personas` tiene una fila por persona, así que además de las defensas
habituales rige el control de divulgación estadística:

  A. Solo agregados: cada columna de la proyección externa es una expresión
     agregada o una columna del GROUP BY. Nunca filas individuales.
  B. Supresión de celdas chicas: las filas cuyo conteo < UMBRAL_SUPRESION se
     descartan tras ejecutar. La supresión es ESTRUCTURAL: el guard identifica
     en el árbol qué columnas de salida SON conteos y se las pasa al supresor;
     si no puede identificarlas, RECHAZA (fail-closed).

Reglas (todas sobre el árbol parseado, no sobre texto):
  1. Parsea, sentencia única, solo SELECT (UNION/DML/PRAGMA/… se rechazan).
  2. SELECT * prohibido (COUNT(*) sí, es un agregado).
  3. Tablas ⊆ {personas, localidades}; JOIN solo personas↔localidades por codloc.
  4. Columnas ⊆ diccionario.json + derivadas + keys + columnas de localidades
     (+ alias de salida de la propia consulta).
  5. Debe existir al menos un COUNT (toda consulta cuenta personas/hogares/viv).
  6. hogar_key / vivienda_key: libres en subconsultas, WHERE, JOIN y GROUP BY
     internos; en la proyección externa SOLO dentro de COUNT(DISTINCT ...).
     PERID sin restricciones especiales.
  7. LIMIT obligatorio, tope LIMITE_MAXIMO (se agrega si falta).
"""

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.simplify import simplify

from comun import orden

from app import dicc

# Celdas con menos personas que esto se suprimen (control de divulgación).
UMBRAL_SUPRESION = 5

# Techo de filas. 300 cubre el nivel geográfico más grande que se mapea.
LIMITE_MAXIMO = 300

TABLAS_PERMITIDAS = {"personas", "localidades", "paises"}

# Columnas del nomenclátor de países (nacidos en el exterior: PERMI01_4/06_4/07_4).
COLUMNAS_PAISES = {"codigo", "nombre", "nombre_oficial", "alfa3"}

# Columnas de personas que referencian un código de país (JOIN válido a paises.codigo).
_COLS_PAIS = {"permi01_4", "permi06_4", "permi07_4"}

# Whitelist de columnas (todo en minúsculas; SQLite es case-insensitive).
_COLUMNAS_VALIDAS = (dicc.columnas_personas()
                     | {c.lower() for c in dicc.COLUMNAS_LOCALIDADES}
                     | COLUMNAS_PAISES)

# Identificadores de hogar/vivienda: reidentificantes.
KEYS_RESTRINGIDAS = {"hogar_key", "vivienda_key"}

# Funciones peligrosas de SQLite (I/O, extensiones) — defensa en profundidad.
_FUNCS_PROHIBIDAS = {
    "load_extension", "readfile", "writefile", "edit", "fsdir", "zipfile",
}


class SQLNoSeguro(Exception):
    """Se lanza cuando una consulta no pasa validación. NO debe ejecutarse."""


def _parsear(sql: str) -> exp.Expression:
    try:
        arboles = [a for a in sqlglot.parse(sql, read="sqlite") if a is not None]
    except Exception as e:  # sqlglot.errors.ParseError y afines
        raise SQLNoSeguro(f"No se pudo parsear la consulta: {e}")
    if len(arboles) != 1:
        raise SQLNoSeguro("Debe ser una sola sentencia SQL.")
    return arboles[0]


def _cols_de_scope_externo(nodo: exp.Expression, externo: exp.Select):
    """Columnas de `nodo` que pertenecen al SELECT externo (no a una
    subconsulta anidada) y que NO están dentro de una función agregada."""
    for c in nodo.find_all(exp.Column):
        if c.find_ancestor(exp.AggFunc) is not None:
            continue
        if c.find_ancestor(exp.Select) is not externo:
            continue  # pertenece a una subconsulta: otro scope
        yield c


def _resolver_group_by(sel: exp.Select, group_exprs: list) -> list:
    """Expande el GROUP BY a las expresiones reales de la proyección.

    `GROUP BY 1` (ordinal) y `GROUP BY <alias de salida>` son SQL válido y
    agrupan igual que nombrar la columna, pero comparados literalmente contra la
    proyección no coinciden con nada y la consulta se rechaza por 'proyección no
    agregada'. El LLM alterna entre las tres formas, así que sin esto el rechazo
    aparece de forma intermitente sobre consultas correctas.

    Solo agrega expresiones; nunca quita, así que no relaja ninguna comprobación.
    """
    proyecciones = list(sel.expressions)
    por_alias = {p.alias.lower(): p.this for p in proyecciones
                 if isinstance(p, exp.Alias) and p.alias}
    resueltas = list(group_exprs)
    for g in group_exprs:
        if isinstance(g, exp.Literal) and g.is_int:          # GROUP BY 1
            i = int(g.name) - 1
            if 0 <= i < len(proyecciones):
                p = proyecciones[i]
                resueltas.append(p.this if isinstance(p, exp.Alias) else p)
        elif isinstance(g, exp.Column) and not g.table:      # GROUP BY <alias>
            destino = por_alias.get(g.name.lower())
            if destino is not None:
                resueltas.append(destino)
    extra = []
    for r in resueltas:
        if not isinstance(r, exp.Column):
            extra.extend(r.find_all(exp.Column))
    return resueltas + extra


def _ctes(arbol: exp.Expression) -> dict:
    """Mapa nombre de CTE -> tablas reales que consulta su cuerpo.

    Un CTE no es una tabla: es un nombre para una subconsulta. Hay que permitirlo
    (el modelo los usa para calcular porcentajes) sin perder de vista qué tablas
    hay abajo, porque el whitelist de tablas se sigue aplicando sobre esas.
    """
    m = {}
    for cte in arbol.find_all(exp.CTE):
        nombre = (cte.alias or "").lower()
        if nombre:
            m[nombre] = {tb.name.lower() for tb in cte.find_all(exp.Table)}
    for _ in range(len(m)):            # un CTE puede apoyarse en otro
        for nombre, tablas in m.items():
            expandido = set()
            for tb in tablas:
                expandido |= m.get(tb, {tb})
            m[nombre] = expandido
    return m


def _mapa_ctes(arbol: exp.Select) -> dict:
    """nombre de CTE -> su SELECT (el cuerpo), tomado del WITH de la raíz."""
    cuerpos = {}
    con = arbol.args.get("with_") or arbol.args.get("with")
    if con:
        for cte in con.expressions:
            if isinstance(cte.this, exp.Select):
                cuerpos[cte.alias_or_name.lower()] = cte.this
    return cuerpos


def _cuerpos_de_fuentes(sel: exp.Select, ctes: dict):
    """Los SELECT que alimentan a `sel`: cuerpos de los CTE que cita y
    subconsultas derivadas del FROM/JOIN. None si alguna fuente NO es uno de
    esos —o sea, si el ámbito lee la tabla de personas directamente, que es
    cuando la proyección sí puede estar devolviendo personas."""
    fuentes = []
    frm = sel.args.get("from_") or sel.args.get("from")
    if frm is not None:
        fuentes.append(frm.this)
    for j in sel.args.get("joins") or []:
        fuentes.append(j.this)
    if not fuentes:
        return None
    cuerpos = []
    for f in fuentes:
        if isinstance(f, exp.Subquery):
            cuerpo = f.this
        elif isinstance(f, exp.Table):
            cuerpo = ctes.get(f.name.lower())
        else:
            return None
        if not isinstance(cuerpo, exp.Select):
            return None
        cuerpos.append(cuerpo)
    return cuerpos


def _col_libre(sel: exp.Select):
    """Primera columna de la proyección de `sel` que no está agregada ni en su
    GROUP BY; None si están todas cubiertas."""
    grupo = sel.args.get("group")
    group_exprs = _resolver_group_by(sel, grupo.expressions if grupo else [])
    group_nombres = {g.name.lower() for g in group_exprs if isinstance(g, exp.Column)}
    group_sql = {g.sql(dialect="sqlite").lower() for g in group_exprs}
    for proj in sel.expressions:
        for c in _cols_de_scope_externo(proj, sel):
            if c.name.lower() in group_nombres:
                continue
            if c.sql(dialect="sqlite").lower() in group_sql:
                continue
            return c
    return None


def _es_scope_agregado(sel, ctes: dict, prof: int = 0) -> bool:
    """¿Las filas que devuelve `sel` son celdas agregadas y no personas?

    Lo son si resume (GROUP BY, o una sola fila de agregados) y ninguna columna
    de su proyección se escapa de ese resumen; o si lo que él lee ya venía
    agregado (cadenas de CTE)."""
    if prof > 4 or not isinstance(sel, exp.Select):
        return False
    resume = bool(sel.args.get("group")) or any(sel.find_all(exp.AggFunc))
    if resume and _col_libre(sel) is None:
        return True
    cuerpos = _cuerpos_de_fuentes(sel, ctes)
    return bool(cuerpos) and all(_es_scope_agregado(c, ctes, prof + 1) for c in cuerpos)


def _fuentes_ya_agregadas(sel: exp.Select, ctes: dict) -> bool:
    cuerpos = _cuerpos_de_fuentes(sel, ctes)
    return bool(cuerpos) and all(_es_scope_agregado(c, ctes) for c in cuerpos)


def _nombres_conteo(sel, ctes: dict, prof: int = 0) -> set:
    """Nombres de salida de `sel` que son un COUNT, propagando por los CTE.

    Sin esto, cuando el conteo crudo se calcula en un CTE y la consulta de salida
    solo lo arrastra, no se identifica ninguna columna de conteo y la supresión
    de celdas chicas dejaría de aplicarse EN SILENCIO."""
    if prof > 4 or not isinstance(sel, exp.Select):
        return set()
    heredados = set()
    for cuerpo in (_cuerpos_de_fuentes(sel, ctes) or []):
        heredados |= _nombres_conteo(cuerpo, ctes, prof + 1)
    salida = set()
    for p in sel.expressions:
        interno = p.this if isinstance(p, exp.Alias) else p
        nombre = p.alias if isinstance(p, exp.Alias) else getattr(p, "name", "")
        if not nombre:
            continue
        if isinstance(interno, exp.Count):
            salida.add(nombre.lower())
        elif isinstance(interno, exp.Column) and interno.name.lower() in heredados:
            salida.add(nombre.lower())
    return salida


def _validar_join(arbol: exp.Expression) -> None:
    """JOIN permitido solo con el nomenclátor: personas↔localidades por codloc, o
    personas↔paises por un código de país (PERMI01_4/06_4/07_4 = paises.codigo).
    Se exige que la condición use exclusivamente esas columnas (no reidentifica)."""
    for j in arbol.find_all(exp.Join):
        using = j.args.get("using")
        on = j.args.get("on")
        if using:
            cols = {u.name.lower() for u in using}
        elif on is not None:
            cols = {c.name.lower() for c in on.find_all(exp.Column)}
        else:
            raise SQLNoSeguro("JOIN sin condición no permitido.")
        es_localidad = cols == {"codloc"}
        es_pais = "codigo" in cols and cols <= ({"codigo"} | _COLS_PAIS)
        if not (es_localidad or es_pais):
            raise SQLNoSeguro(
                "JOIN solo permitido con el nomenclátor: localidades por codloc "
                "o paises por PERMI01_4/PERMI06_4/PERMI07_4 = paises.codigo."
            )


def _aplicar_limite(arbol: exp.Select) -> exp.Select:
    lim = arbol.args.get("limit")
    if lim is None:
        return arbol.limit(LIMITE_MAXIMO)
    try:
        n = int(lim.expression.name)
    except (AttributeError, ValueError):
        raise SQLNoSeguro("LIMIT no numérico.")
    if n > LIMITE_MAXIMO:
        raise SQLNoSeguro(f"LIMIT excede el máximo de {LIMITE_MAXIMO}.")
    return arbol


# ── las tres tasas del mercado de trabajo (2011) ─────────────────────────────
# Mismo criterio que en 2023 (`sql_guard_2023._tasas_del_mercado_de_trabajo`), pero la
# CODIFICACIÓN DE 2011 ES DISTINTA y no se puede copiar:
#
#   1 = Menor de 12 años   2 = Ocupados   3 = Desocupados buscan trabajo por primera vez
#   4 = Desocupados propiamente dichos    5 = Inactivos jubilados/pensionistas
#   6 = Inactivos otras causas            8 = No relevado (perdido)
#
# Los desocupados son DOS códigos, no uno: la PEA es (2,3,4) y trasladar el mapa de 2023
# —donde desocupados es sólo el 3— habría contado nada más que a los que buscan trabajo
# por primera vez (21.212 de 99.938) y publicado 1,35 % en vez de 6,35 %.
#
# PET = 12 años y más, por la misma razón que en 2023 (Carlos, 13-ago-2026): 'Menor de
# 12 años' quiere decir 11 o menos, así que el universo relevado empieza en los 12.
# Verificado sobre la base 2011: `pobpcoac=1` no tiene a NADIE de 12 o más, y los 4.346
# menores de 12 con otro código son todos 'No relevado' (8), que ya es perdido. Y no hay
# ningún activo por debajo de 12, así que el piso no le toca el numerador a ninguna tasa.
_POBPCOAC = "pobpcoac"
_EDAD = "edad"
_PISO_PET = 12
_TASAS = {
    ("3", "4"): ("%s IN (2, 3, 4)" % _POBPCOAC, "desocupación sobre la PEA"),
    ("2", "3", "4"): ("%s >= %d" % (_EDAD, _PISO_PET), "actividad sobre la PET"),
    ("2",): ("%s >= %d" % (_EDAD, _PISO_PET), "empleo sobre la PET"),
}


def _codigos_del_numerador(nodo):
    """Los códigos de pobpcoac que suma este numerador, o None si no tiene esa forma.

    Reconoce SUM(CASE WHEN pobpcoac IN (3,4) THEN 1 ELSE 0 END) y COUNT(CASE WHEN ...),
    que son las dos formas que escribe el modelo en 2011 (acá no hay ponderador: la
    base es el censo completo y se cuenta con COUNT/SUM de 1)."""
    agregados = [a for a in nodo.find_all(exp.Sum, exp.Count)]
    if len(agregados) != 1:
        return None
    casos = list(agregados[0].find_all(exp.Case))
    if len(casos) != 1:
        return None
    condiciones = list(casos[0].find_all(exp.EQ, exp.In))
    if len(condiciones) != 1:
        return None
    cond = condiciones[0]
    if not (isinstance(cond.this, exp.Column) and cond.this.name.lower() == _POBPCOAC):
        return None
    valores = ([cond.expression] if isinstance(cond, exp.EQ) else list(cond.expressions))
    if not all(isinstance(v, exp.Literal) for v in valores) or not valores:
        return None
    return tuple(sorted(str(v.name) for v in valores))


def _es_denominador_poblacion(nodo):
    """¿El denominador es la población entera del ámbito? COUNT(*) o COUNT(col) sin CASE."""
    if list(nodo.find_all(exp.Case)):
        return False
    agregados = list(nodo.find_all(exp.Sum, exp.Count))
    return len(agregados) == 1


def _neutralizar_pobpcoac(sel):
    """Saca del WHERE las condiciones que restringen pobpcoac, dejando el resto.

    Esto NO existe en el guard de 2023 y acá hace falta: el modelo escribe el
    denominador de la tasa de actividad como `WHERE pobpcoac IN (2,3,4,5,6)` —"los que
    tienen respuesta válida"—, que deja afuera a los 97.967 'No relevado' de 12 y más y
    da 59,90 % en vez de 57,75 %. Es el mismo defecto que en 2023 corregía la exención
    del fuera de universo: la PET la define la EDAD, no la variable de actividad.
    Agregar el filtro de edad con AND no alcanzaría, porque el filtro restrictivo
    seguiría ahí. El resto del WHERE (el ámbito geográfico, por ejemplo) se conserva."""
    donde = sel.args.get("where")
    if donde is None:
        return
    for cond in list(donde.find_all(exp.EQ, exp.In, exp.NEQ)):
        col = cond.this
        if isinstance(col, exp.Column) and col.name.lower() == _POBPCOAC:
            cond.replace(exp.true())


def _tasas_del_mercado_de_trabajo(arbol):
    """Cada tasa sobre SU denominador, hecho cumplir por la FORMA de la razón.

    Deliberadamente angosto, igual que en 2023: dispara sólo cuando la proyección tiene
    UNA razón cuyo numerador suma exclusivamente uno de los tres conjuntos de códigos y
    cuyo denominador es la población entera del ámbito. Un desglose por condición de
    actividad (GROUP BY pobpcoac) no entra, y no debe: ahí los inactivos son la
    respuesta."""
    for sel in list(arbol.find_all(exp.Select)):
        if "personas" not in {t.name.lower() for t in sel.find_all(exp.Table)}:
            continue
        razones = [d for proj in sel.expressions for d in proj.find_all(exp.Div)]
        if len(razones) != 1:
            continue
        div = razones[0]
        codigos = _codigos_del_numerador(div.this)
        if codigos not in _TASAS or not _es_denominador_poblacion(div.expression):
            continue
        condicion, _ = _TASAS[codigos]
        _neutralizar_pobpcoac(sel)
        sel.where(sqlglot.condition(condicion, dialect="sqlite"), copy=False)
        # La neutralización deja TRUE en lugar de la condición borrada; simplify lo
        # limpia. Es cosmético pero el SQL se le muestra al usuario, y un
        # `WHERE TRUE AND edad >= 12` invita a preguntar qué se sacó de ahí.
        simplify(sel.args["where"])
        return True
    return False


_contador_alias = 0


def _identificar_conteos(arbol: exp.Select) -> list[str]:
    """Devuelve los nombres de salida de las columnas de la proyección externa
    que SON un COUNT/COUNT(DISTINCT). A las que no tienen alias les inyecta uno
    determinista para poder localizar su valor en las filas resultantes."""
    global _contador_alias
    nombres = []
    for i, proj in enumerate(list(arbol.expressions)):
        interno = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(interno, exp.Count):
            if isinstance(proj, exp.Alias):
                nombres.append(proj.alias)
            else:
                alias = f"conteo_{i}"
                proj.replace(exp.alias_(proj.copy(), alias))
                nombres.append(alias)
    return nombres


def validar(sql: str) -> tuple[str, list[str]]:
    """Valida un SQL del LLM. Devuelve (sql_seguro, columnas_de_conteo).
    Lanza SQLNoSeguro si algo no puede verificarse."""
    arbol = _parsear(sql)

    # 1. Solo SELECT (Union, Insert, Delete, Drop, Pragma, ... no son exp.Select).
    if not isinstance(arbol, exp.Select):
        raise SQLNoSeguro("Solo se permiten consultas SELECT.")

    # 2. SELECT * prohibido (permitido solo dentro de un agregado: COUNT(*)).
    for star in arbol.find_all(exp.Star):
        if star.find_ancestor(exp.AggFunc) is None:
            raise SQLNoSeguro(
                "SELECT * prohibido: los microdatos solo se consultan agregados."
            )

    # 3. Tablas whitelisted + JOIN solo personas↔localidades por codloc.
    nombres_cte = _ctes(arbol)
    tablas = list(arbol.find_all(exp.Table))
    if not tablas:
        raise SQLNoSeguro("No se detectó tabla de origen.")
    for t in tablas:
        if t.name.lower() in nombres_cte:
            continue   # nombre de CTE: lo que importa son las tablas de su cuerpo
        if t.name.lower() not in TABLAS_PERMITIDAS:
            raise SQLNoSeguro(f"Tabla no permitida: {t.name}")
    _validar_join(arbol)

    # 4. Columnas whitelisted (+ alias de salida definidos en la consulta).
    alias_salida = {a.alias.lower() for a in arbol.find_all(exp.Alias) if a.alias}
    permitidas = _COLUMNAS_VALIDAS | alias_salida
    for c in arbol.find_all(exp.Column):
        if c.name.lower() not in permitidas:
            raise SQLNoSeguro(f"Columna no permitida: {c.name}")

    # 4b. Funciones peligrosas (I/O, extensiones).
    for f in arbol.find_all(exp.Anonymous):
        if f.name.lower() in _FUNCS_PROHIBIDAS:
            raise SQLNoSeguro(f"Función no permitida: {f.name}")

    # 5. Debe contar: al menos un COUNT en algún lugar del árbol.
    if not list(arbol.find_all(exp.Count)):
        raise SQLNoSeguro(
            "Consulta no agregada: toda consulta debe contar (COUNT), "
            "nunca devolver registros individuales."
        )

    # 6. Solo agregados: cada columna libre de la proyección externa debe estar
    #    en el GROUP BY. (Núcleo del control: nunca filas individuales.)
    ctes_cuerpos = _mapa_ctes(arbol)
    libre = _col_libre(arbol)
    # Una columna suelta NO es una persona si la fila que la trae ya es una celda
    # agregada: es el caso del CTE que cuenta y la consulta de salida que solo
    # calcula el porcentaje sobre ese conteo.
    salida_desde_agregado = libre is not None and _fuentes_ya_agregadas(arbol, ctes_cuerpos)
    if libre is not None and not salida_desde_agregado:
        raise SQLNoSeguro(
            f"Proyección no agregada: la columna '{libre.name}' no está agregada "
            "ni en el GROUP BY (devolvería filas individuales)."
        )

    # 7. hogar_key / vivienda_key en la proyección externa: solo COUNT(DISTINCT).
    for proj in arbol.expressions:
        for c in proj.find_all(exp.Column):
            if c.name.lower() not in KEYS_RESTRINGIDAS:
                continue
            if c.find_ancestor(exp.Select) is not arbol:
                continue  # dentro de una subconsulta: libre
            # Solo válido dentro de COUNT(DISTINCT ...): en sqlglot el DISTINCT
            # es un nodo Distinct hijo del Count (Count.this = Distinct(col)).
            cnt = c.find_ancestor(exp.Count)
            dist = c.find_ancestor(exp.Distinct)
            if cnt is None or dist is None or dist.find_ancestor(exp.Count) is not cnt:
                raise SQLNoSeguro(
                    f"{c.name} en la proyección externa solo puede aparecer "
                    "dentro de COUNT(DISTINCT ...)."
                )

    # 8. Supresión estructural: identificar columnas de conteo (fail-closed).
    try:
        columnas_conteo = _identificar_conteos(arbol)
        if salida_desde_agregado:
            heredados = set()
            for cuerpo in (_cuerpos_de_fuentes(arbol, ctes_cuerpos) or []):
                heredados |= _nombres_conteo(cuerpo, ctes_cuerpos)
            for p in arbol.expressions:
                interno = p.this if isinstance(p, exp.Alias) else p
                nombre = p.alias if isinstance(p, exp.Alias) else getattr(p, "name", "")
                if (nombre and isinstance(interno, exp.Column)
                        and interno.name.lower() in heredados
                        and nombre not in columnas_conteo):
                    columnas_conteo.append(nombre)
    except Exception as e:
        raise SQLNoSeguro(f"No se pudieron identificar los conteos: {e}")

    # El conteo crudo tiene que llegar a la salida: si no, no hay con qué
    # suprimir las celdas chicas y se rechaza (fail-closed).
    if salida_desde_agregado and not columnas_conteo:
        raise SQLNoSeguro(
            "La consulta arrastra celdas ya agregadas pero no expone el conteo "
            "crudo: sin él no puede aplicarse la supresión."
        )

    # 8b. Las tres tasas del mercado de trabajo, cada una sobre su denominador.
    _tasas_del_mercado_de_trabajo(arbol)

    # 9. LIMIT obligatorio y acotado.
    arbol = _aplicar_limite(arbol)
    arbol = orden.desempatar(arbol)

    return arbol.sql(dialect="sqlite", comments=False), columnas_conteo


def suprimir_celdas_chicas(
    filas: list[dict], columnas_conteo: list[str]
) -> tuple[list[dict], int]:
    """Control de divulgación: descarta cada fila cuyo conteo esté por debajo de
    UMBRAL_SUPRESION. Las columnas de conteo las identifica el guard sobre el
    árbol (por nombre de salida), no por heurística de nombres."""
    claves = {c.lower() for c in columnas_conteo}
    seguras, suprimidas = [], 0
    for fila in filas:
        conteos = [
            v for k, v in fila.items()
            if k.lower() in claves and isinstance(v, int) and not isinstance(v, bool)
        ]
        if conteos and min(conteos) < UMBRAL_SUPRESION:
            suprimidas += 1
        else:
            seguras.append(fila)
    return seguras, suprimidas
