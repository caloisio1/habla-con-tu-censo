"""sql_guard_2023.py — Guard sqlglot para el motor Censo 2023 (versión ponderada).

Misma interfaz que el guard 2011 (validar / suprimir_celdas_chicas / SQLNoSeguro /
UMBRAL_SUPRESION), NO modifica el guard 2011. Reglas propias 2023:

  a) Prohibido JOIN/mezcla personas_2023 ↔ viviendas_2023 (cualquier scope). JOIN solo
     con nomenclátor (departamentos_2023, localidades_2023, barrios_mvd_2023).
  b) En consultas sobre personas_2023 la métrica publicada de personas debe ser SUM(W)
     (exigir ≥1 SUM que involucre W en la proyección externa) y debe existir el conteo
     CRUDO por celda (≥1 COUNT) para evaluar supresión.
  c) Supresión estructural: celda con n crudo < UMBRAL_SUPRESION se descarta (salida y
     mapas). El n crudo nunca se expone si < umbral (la fila entera se suprime).
  d) Identificadores (vivienda_key, hogar_key, DIRECCION_ID, VIVID, HOGID, PERID,
     ID_HOGAR) libres en subconsultas/GROUP BY interno; en la proyección externa SOLO
     dentro de COUNT(DISTINCT ...). (Regla 2011 replicada.)
"""
import json, os
import sqlglot
from sqlglot import exp

from comun import orden

UMBRAL_SUPRESION = 5
LIMITE_MAXIMO = 300

_COLS = json.load(open(os.path.join(os.path.dirname(__file__), "cols_2023.json")))
FACT_TABLES = {"personas_2023", "viviendas_2023"}
# paises: nomenclátor de países para nacidos en el exterior (PERMI01_4/06_4/07_4).
NOMENCLATOR = {"departamentos_2023", "localidades_2023", "barrios_mvd_2023", "paises"}
TABLAS_PERMITIDAS = FACT_TABLES | NOMENCLATOR
_COLUMNAS_VALIDAS = ({c.lower() for cols in _COLS.values() for c in cols}
                     | {"codigo", "nombre", "nombre_oficial", "alfa3"})

KEYS_RESTRINGIDAS = {"vivienda_key", "hogar_key", "direccion_id", "vivid",
                     "hogid", "perid", "id_hogar"}
_FUNCS_PROHIBIDAS = {"load_extension", "readfile", "writefile", "edit", "fsdir", "zipfile"}


class SQLNoSeguro(Exception):
    """Se lanza cuando una consulta no pasa validación. NO debe ejecutarse."""


def _parsear(sql):
    try:
        arboles = [a for a in sqlglot.parse(sql, read="sqlite") if a is not None]
    except Exception as e:
        raise SQLNoSeguro(f"No se pudo parsear la consulta: {e}")
    if len(arboles) != 1:
        raise SQLNoSeguro("Debe ser una sola sentencia SQL.")
    return arboles[0]


def _cols_scope_externo(nodo, externo):
    for c in nodo.find_all(exp.Column):
        if c.find_ancestor(exp.AggFunc) is not None:
            continue
        if c.find_ancestor(exp.Select) is not externo:
            continue
        yield c


def _tablas(arbol):
    return {t.name.lower() for t in arbol.find_all(exp.Table)}


def _suma_sobre_w(arbol):
    """True si la proyección externa tiene al menos un SUM que involucra la columna W."""
    for proj in arbol.expressions:
        for s in proj.find_all(exp.Sum):
            if any(col.name.lower() == "w" for col in s.find_all(exp.Column)):
                return True
    return False


def _count_distinct_hogar(arbol):
    """True si la proyección externa cuenta hogares: COUNT(DISTINCT hogar_key)."""
    for proj in arbol.expressions:
        for cnt in proj.find_all(exp.Count):
            if cnt.find(exp.Distinct) and any(
                col.name.lower() == "hogar_key" for col in cnt.find_all(exp.Column)):
                return True
    return False


def _alias_de_tablas(arbol):
    """Mapa alias/nombre-en-la-consulta -> nombre real de tabla (minúsculas)."""
    m = {}
    for t in arbol.find_all(exp.Table):
        real = t.name.lower()
        m[real] = real
        if t.alias:
            m[t.alias.lower()] = real
    return m


def _resolver_group_by(sel, group_exprs):
    """Expande el GROUP BY a las expresiones reales de la proyección.

    `GROUP BY 1` (ordinal) y `GROUP BY <alias de salida>` agrupan igual que
    nombrar la columna, pero comparados literalmente contra la proyección no
    coinciden con nada y la consulta se rechaza por 'proyección no agregada'. El
    LLM alterna entre las tres formas: sin esto el rechazo es intermitente sobre
    consultas correctas. Solo agrega expresiones, nunca quita."""
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


def _ctes(arbol):
    """Mapa nombre de CTE -> tablas reales que consulta su cuerpo. Un CTE no es
    una tabla: es un nombre para una subconsulta. Se permite (el modelo los usa
    para calcular porcentajes) sin perder de vista qué tablas hay abajo: el
    whitelist y la regla de universos se siguen aplicando sobre esas."""
    m = {}
    for cte in arbol.find_all(exp.CTE):
        nombre = (cte.alias or "").lower()
        if nombre:
            m[nombre] = {tb.name.lower() for tb in cte.find_all(exp.Table)}
    for _ in range(len(m)):
        for nombre, tablas in m.items():
            expandido = set()
            for tb in tablas:
                expandido |= m.get(tb, {tb})
            m[nombre] = expandido
    return m


def _mapa_ctes(arbol):
    """nombre de CTE -> su SELECT (el cuerpo), tomado del WITH de la raíz."""
    cuerpos = {}
    con = arbol.args.get("with_") or arbol.args.get("with")
    if con:
        for cte in con.expressions:
            if isinstance(cte.this, exp.Select):
                cuerpos[cte.alias_or_name.lower()] = cte.this
    return cuerpos


def _cuerpos_de_fuentes(sel, ctes):
    """Los SELECT que alimentan a `sel`: cuerpos de los CTE que cita y
    subconsultas derivadas del FROM/JOIN. None si alguna fuente NO es uno de
    esos —o sea, si el ámbito lee una tabla de hechos directamente."""
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


def _col_libre(sel):
    """Primera columna de la proyección de `sel` que no está agregada ni en su
    GROUP BY; None si están todas cubiertas. Las del nomenclátor no cuentan:
    son lookup (el nombre de un código), no microdato."""
    alias_tab = _alias_de_tablas(sel)
    grupo = sel.args.get("group")
    group_exprs = _resolver_group_by(sel, grupo.expressions if grupo else [])
    group_nombres = {g.name.lower() for g in group_exprs if isinstance(g, exp.Column)}
    group_sql = {g.sql(dialect="sqlite").lower() for g in group_exprs}
    for proj in sel.expressions:
        for c in _cols_scope_externo(proj, sel):
            if c.name.lower() in group_nombres:
                continue
            if c.sql(dialect="sqlite").lower() in group_sql:
                continue
            if c.table and alias_tab.get(c.table.lower()) in NOMENCLATOR:
                continue
            return c
    return None


def _es_scope_agregado(sel, ctes, prof=0):
    """¿Las filas que devuelve `sel` son celdas agregadas y no personas? Lo son
    si resume (GROUP BY, o una sola fila de agregados) y ninguna columna de su
    proyección se escapa de ese resumen; o si lo que él lee ya venía agregado."""
    if prof > 4 or not isinstance(sel, exp.Select):
        return False
    resume = bool(sel.args.get("group")) or any(sel.find_all(exp.AggFunc))
    if resume and _col_libre(sel) is None:
        return True
    cuerpos = _cuerpos_de_fuentes(sel, ctes)
    return bool(cuerpos) and all(_es_scope_agregado(c, ctes, prof + 1) for c in cuerpos)


def _fuentes_ya_agregadas(sel, ctes):
    cuerpos = _cuerpos_de_fuentes(sel, ctes)
    return bool(cuerpos) and all(_es_scope_agregado(c, ctes) for c in cuerpos)


def _nombres_conteo(sel, ctes, prof=0):
    """Nombres de salida de `sel` que son un COUNT, propagando por los CTE. Sin
    esto, si el conteo crudo se calcula en un CTE y la salida solo lo arrastra,
    no se identifica ninguna columna de conteo y la supresión de celdas chicas
    dejaría de aplicarse EN SILENCIO."""
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


def _metrica_publicada_ok(sel, ctes, prof=0):
    """La cifra de personas sale de SUM(W) (o de COUNT(DISTINCT hogar_key)).
    Cuando la salida solo arrastra un agregado calculado en un CTE, la métrica
    hay que buscarla ahí: si no, la consulta correcta se rechaza."""
    if _suma_sobre_w(sel) or _count_distinct_hogar(sel):
        return True
    if prof > 4:
        return False
    cuerpos = _cuerpos_de_fuentes(sel, ctes)
    if not cuerpos or not all(_es_scope_agregado(c, ctes) for c in cuerpos):
        return False
    return any(_metrica_publicada_ok(c, ctes, prof + 1) for c in cuerpos)


def _aplicar_limite(arbol):
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


def _identificar_conteos(arbol):
    """Nombres de salida de las columnas de la proyección externa que SON COUNT/COUNT(DISTINCT).
    A las sin alias les inyecta uno determinista para localizar su valor en las filas."""
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


def validar(sql):
    """Valida un SQL del LLM para el motor 2023. Devuelve (sql_seguro, columnas_de_conteo).
    Lanza SQLNoSeguro si algo no puede verificarse (fail-closed)."""
    arbol = _parsear(sql)

    if not isinstance(arbol, exp.Select):
        raise SQLNoSeguro("Solo se permiten consultas SELECT.")

    for star in arbol.find_all(exp.Star):
        if star.find_ancestor(exp.AggFunc) is None:
            raise SQLNoSeguro("SELECT * prohibido: los microdatos solo se consultan agregados.")

    tablas = _tablas(arbol)
    if not tablas:
        raise SQLNoSeguro("No se detectó tabla de origen.")
    nombres_cte = _ctes(arbol)
    for t in tablas:
        if t in nombres_cte:
            continue   # nombre de CTE: lo que importa son las tablas de su cuerpo
        if t not in TABLAS_PERMITIDAS:
            raise SQLNoSeguro(f"Tabla no permitida: {t}")

    # (a) prohibido mezclar las dos tablas de hechos (JOIN o subconsulta, cualquier scope)
    if FACT_TABLES <= tablas:
        raise SQLNoSeguro(
            "Prohibido vincular personas_2023 con viviendas_2023 (universos distintos; §3.2 INE)."
        )

    # columnas whitelisted (+ alias de salida propios)
    alias_salida = {a.alias.lower() for a in arbol.find_all(exp.Alias) if a.alias}
    permitidas = _COLUMNAS_VALIDAS | alias_salida
    for c in arbol.find_all(exp.Column):
        if c.name.lower() not in permitidas:
            raise SQLNoSeguro(f"Columna no permitida: {c.name}")

    for f in arbol.find_all(exp.Anonymous):
        if f.name.lower() in _FUNCS_PROHIBIDAS:
            raise SQLNoSeguro(f"Función no permitida: {f.name}")

    # (b/raw-n) debe existir al menos un COUNT (conteo crudo para supresión)
    if not list(arbol.find_all(exp.Count)):
        raise SQLNoSeguro("Consulta sin COUNT: falta el conteo crudo por celda para la supresión.")

    # (b) personas -> métrica publicada por SUM(W) o hogares por COUNT(DISTINCT hogar_key);
    #     nunca un COUNT(*) crudo como cifra de personas.
    ctes_cuerpos = _mapa_ctes(arbol)
    if "personas_2023" in tablas and not _metrica_publicada_ok(arbol, ctes_cuerpos):
        raise SQLNoSeguro(
            "Métrica de personas inválida: usá SUM(W) (personas) o COUNT(DISTINCT hogar_key) "
            "(hogares); COUNT(*) es solo el conteo crudo para la supresión, no la cifra publicada."
        )

    # solo agregados en la proyección externa (nunca filas individuales de las tablas de hechos).
    # Se permiten columnas SIN agregar si pertenecen al NOMENCLÁTOR (lookup, no microdato).
    libre = _col_libre(arbol)
    # Una columna suelta NO es una persona si la fila que la trae ya es una celda
    # agregada: es el caso del CTE que cuenta y la consulta de salida que solo
    # calcula el porcentaje sobre ese conteo.
    salida_desde_agregado = libre is not None and _fuentes_ya_agregadas(arbol, ctes_cuerpos)
    if libre is not None and not salida_desde_agregado:
        raise SQLNoSeguro(
            f"Proyección no agregada: la columna '{libre.name}' no está agregada ni en el GROUP BY."
        )

    # (d) identificadores en proyección externa: solo dentro de COUNT(DISTINCT ...)
    for proj in arbol.expressions:
        for c in proj.find_all(exp.Column):
            if c.name.lower() not in KEYS_RESTRINGIDAS:
                continue
            if c.find_ancestor(exp.Select) is not arbol:
                continue
            cnt = c.find_ancestor(exp.Count)
            dist = c.find_ancestor(exp.Distinct)
            if cnt is None or dist is None or dist.find_ancestor(exp.Count) is not cnt:
                raise SQLNoSeguro(
                    f"{c.name} en la proyección externa solo puede aparecer dentro de COUNT(DISTINCT ...)."
                )

    # (c) supresión estructural: identificar columnas de conteo (fail-closed)
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

    # El conteo crudo tiene que llegar a la salida: sin él no puede aplicarse la
    # supresión de celdas chicas, así que se rechaza (fail-closed).
    if salida_desde_agregado and not columnas_conteo:
        raise SQLNoSeguro(
            "La consulta arrastra celdas ya agregadas pero no expone el conteo crudo: "
            "sin él no puede aplicarse la supresión."
        )

    arbol = _aplicar_limite(arbol)
    arbol = orden.desempatar(arbol)
    return arbol.sql(dialect="sqlite", comments=False), columnas_conteo


def suprimir_celdas_chicas(filas, columnas_conteo):
    """Descarta cada fila cuyo conteo crudo < UMBRAL_SUPRESION (control de divulgación).
    El n crudo < umbral nunca llega al usuario: se suprime la fila entera."""
    claves = {c.lower() for c in columnas_conteo}
    seguras, suprimidas = [], 0
    for fila in filas:
        conteos = [v for k, v in fila.items()
                   if k.lower() in claves and isinstance(v, int) and not isinstance(v, bool)]
        if conteos and min(conteos) < UMBRAL_SUPRESION:
            suprimidas += 1
        else:
            seguras.append(fila)
    return seguras, suprimidas
