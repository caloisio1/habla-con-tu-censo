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
  e) HOGARES ponderados: COUNT(DISTINCT hogar_key) es un conteo de registros y NO es
     la cifra publicada de hogares. La cifra sale de sumar el ponderador una vez por
     hogar. Ver `_metrica_publicada_ok`.
  f) FUERA DE UNIVERSO: las categorías que marcan que la pregunta no correspondía
     ('Menor de 25 años' en nivel educativo) se excluyen SIEMPRE, y la exclusión la
     inyecta el guard en vez de confiarla al modelo. Ver `_excluir_fuera_de_universo`.
"""
import json, os
import sqlglot
from sqlglot import exp

from comun import orden, universo

UMBRAL_SUPRESION = 5
# Tope de filas de la TABLA de resultados. Cubre el desglose completo más grande que
# tiene el censo 2023 (4.297 segmentos censales con población): con 300 —el valor
# anterior— una pregunta por segmento devolvía 300 filas y las otras 3.997 se perdían
# sin que nada lo dijera. El tope del MAPA es otro y mucho más chico (no se pueden
# dibujar miles de polígonos legibles): vive en consultar_2023.LIMITE_MAPA.
LIMITE_MAXIMO = 5000
_DICCIONARIO = os.path.join(os.path.dirname(__file__), "diccionario_llm_2023.json")

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


def _col_libre(sel, ctes=None):
    """Primera columna de la proyección de `sel` que no está agregada ni en su
    GROUP BY; None si están todas cubiertas. Las del nomenclátor no cuentan:
    son lookup (el nombre de un código), no microdato.

    NO se afloja para las columnas que vienen de una subconsulta agregada por CROSS
    JOIN (`SUM(p.W) / h.hogares`). Se probó el 12-ago y es un callejón: DuckDB —el
    único motor desde el 11-ago— rechaza igual esa consulta porque `h.hogares` no
    está agregada, así que aflojar acá sólo cambia un rechazo con explicación por un
    error de ejecución. La forma buena de una razón se le pide al modelo en el
    prompt: se calculan las dos cifras en la MISMA subconsulta por hogar."""
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
    if resume and _col_libre(sel, ctes) is None:
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
    """La cifra publicada sale de SUMAR EL PONDERADOR, tanto para personas como
    para hogares. Cuando la salida solo arrastra un agregado calculado en un CTE,
    la métrica hay que buscarla ahí: si no, la consulta correcta se rechaza.

    COUNT(DISTINCT hogar_key) YA NO ALCANZA. Cuenta hogares censados, no hogares
    estimados: da 1.255.062 contra los 1.376.921 que publica el INE, un 9,7 %
    menos. Y el error no es parejo —la ponderación corrige la omisión censal, que
    se concentró en los estratos bajos—, así que contestar el crudo deshace esa
    corrección justo donde más pesa. Sigue siendo válido como conteo de control
    (n crudo para la supresión), nunca como la cifra que se muestra."""
    if _suma_sobre_w(sel):
        return True
    if prof > 4:
        return False
    cuerpos = _cuerpos_de_fuentes(sel, ctes)
    if not cuerpos or not all(_es_scope_agregado(c, ctes) for c in cuerpos):
        return False
    return any(_metrica_publicada_ok(c, ctes, prof + 1) for c in cuerpos)


def _tablas_directas(sel):
    """Tablas nombradas en el FROM/JOIN de ESTE ámbito, sin bajar a subconsultas.
    Distingue 'este SELECT lee los microdatos' de 'este SELECT lee lo que otro ya
    agregó', que es lo que decide dónde hay que poner el filtro de universo."""
    nombres = set()
    fuentes = []
    frm = sel.args.get("from_") or sel.args.get("from")
    if frm is not None:
        fuentes.append(frm.this)
    for j in sel.args.get("joins") or []:
        fuentes.append(j.this)
    for f in fuentes:
        if isinstance(f, exp.Table):
            nombres.add(f.name.lower())
    return nombres


def _apunta_al_fuera(sel, var, codigos):
    """¿Este ámbito pide EXPLÍCITAMENTE la categoría de fuera de universo?

    Si alguien escribió `WHERE NIVELEDU25MAS = '0'` está preguntando a propósito por
    los menores de 25, y agregarle la exclusión convertiría la consulta en una
    contradicción que devuelve 0 sin explicar por qué. En ese caso el filtro no se
    inyecta: la consulta es rara, pero un cero silencioso es peor."""
    donde = sel.args.get("where")
    if donde is None:
        return False
    objetivo = {str(c).lower() for c in codigos}
    for nodo in donde.find_all(exp.EQ, exp.In):
        if nodo.find_ancestor(exp.Not) is not None:
            continue
        izq = nodo.this
        if not (isinstance(izq, exp.Column) and izq.name.lower() == var):
            continue
        valores = ([nodo.expression] if isinstance(nodo, exp.EQ)
                   else list(nodo.expressions))
        for v in valores:
            if isinstance(v, exp.Literal) and str(v.name).lower() in objetivo:
                return True
    return False


def _excluir_fuera_de_universo(arbol, fuera):
    """Inyecta la exclusión de los códigos de fuera de universo en cada ámbito que
    lee personas_2023 y nombra una de esas variables. Devuelve las que aplicó.

    POR QUÉ LO HACE EL GUARD Y NO EL PROMPT. El prompt ya lo pide, pero pedirlo no
    alcanza: medido el 12-ago, la misma pregunta por nivel educativo excluía el
    código 0 en una corrida y no lo excluía en la siguiente. Un error que aparece
    una de cada dos veces no se cierra mirando la pantalla, y en la corrida mala la
    cifra sale 48,8 % baja. Acá es determinista: si la variable está en la consulta,
    el filtro está.

    `NOT IN (...)` deja afuera también los NULL —en SQL `NULL NOT IN (...)` no es
    verdadero—, que es justo lo que ya pedían las reglas para los denominadores."""
    aplicadas = []
    for sel in list(arbol.find_all(exp.Select)):
        if "personas_2023" not in _tablas_directas(sel):
            continue
        usadas = []
        for c in sel.find_all(exp.Column):
            if c.find_ancestor(exp.Select) is not sel:
                continue
            nombre = c.name.lower()
            if nombre in fuera and nombre not in usadas:
                usadas.append(nombre)
        for var in usadas:
            info = fuera[var]
            if _apunta_al_fuera(sel, var, info["codigos"]):
                continue
            if info["tipo"] == "TEXT":
                lits = ", ".join("'%s'" % c.replace("'", "''") for c in info["codigos"])
            else:
                lits = ", ".join(str(c) for c in info["codigos"])
            sel.where(sqlglot.condition("%s NOT IN (%s)" % (info["nombre"], lits),
                                        dialect="sqlite"), copy=False)
            aplicadas.append(var)
    return sorted(set(aplicadas))


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
        if _count_distinct_hogar(arbol):
            raise SQLNoSeguro(
                "Hogares sin ponderar: COUNT(DISTINCT hogar_key) cuenta hogares censados, no los "
                "publicados por el INE. La cifra de hogares se calcula sumando el ponderador una "
                "vez por hogar: FROM (SELECT hogar_key, MAX(W) AS w FROM personas_2023 WHERE "
                "hogar_key IS NOT NULL GROUP BY hogar_key) y en la salida SUM(w)."
            )
        raise SQLNoSeguro(
            "Métrica inválida: la cifra publicada sale de sumar el ponderador —SUM(W) para "
            "personas, SUM(w) sobre un W por hogar para hogares—; COUNT(*) es solo el conteo "
            "crudo para la supresión, no la cifra publicada."
        )

    # solo agregados en la proyección externa (nunca filas individuales de las tablas de hechos).
    # Se permiten columnas SIN agregar si pertenecen al NOMENCLÁTOR (lookup, no microdato).
    libre = _col_libre(arbol, ctes_cuerpos)
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

    # (f) fuera de universo: se inyecta acá, con la consulta ya validada y antes del
    # LIMIT, para que el filtro entre a todos los ámbitos que leen microdatos.
    if "personas_2023" in tablas:
        _excluir_fuera_de_universo(arbol, universo.tabla(_DICCIONARIO))

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
