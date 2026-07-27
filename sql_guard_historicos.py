"""sql_guard_historicos.py — Guard sqlglot para los motores de los censos 1996 y 2004.

Misma interfaz que los guards 2011 y 2023 (validar / suprimir_celdas_chicas /
SQLNoSeguro / UMBRAL_SUPRESION / LIMITE_MAXIMO). NO toca esos guards.

Los dos censos comparten reglas —ninguno es ponderado, todas las cifras son
conteos exactos—, así que el guard es uno solo parametrizado por censo en vez de
dos archivos casi iguales. Lo que cambia por censo son las tablas, el nomenclátor
y los identificadores restringidos.

Reglas:
  a) Solo SELECT, siempre agregado; nunca filas individuales ni SELECT *.
  b) Tablas y columnas whitelisted (las que existen en la base, vía cols_XXXX.json).
  c) 1996: prohibido vincular personas_1996 con viviendas_1996 (universos distintos;
     las variables de vivienda ya vienen copiadas en la tabla de personas).
  d) Debe existir al menos un COUNT: es el n crudo por celda que habilita la
     supresión. Sin él no se puede evaluar confidencialidad -> se rechaza.
  e) Supresión estructural: celda con n crudo < UMBRAL_SUPRESION se descarta entera
     (nunca se expone un n por debajo del umbral).
  f) Identificadores libres en subconsultas/GROUP BY interno, en la proyección
     externa SOLO dentro de COUNT(DISTINCT ...).
"""
import json
import os

import sqlglot
from sqlglot import exp

UMBRAL_SUPRESION = 5
LIMITE_MAXIMO = 300

_FUNCS_PROHIBIDAS = {"load_extension", "readfile", "writefile", "edit", "fsdir", "zipfile"}
AQUI = os.path.dirname(os.path.abspath(__file__))


class SQLNoSeguro(Exception):
    """Se lanza cuando una consulta no pasa validación. NO debe ejecutarse."""


class Guard:
    """Validador de una base concreta. Se instancia una vez por censo."""

    def __init__(self, censo, tablas_hechos, nomenclator, keys_restringidas,
                 pares_prohibidos=()):
        self.censo = censo
        self.tablas_hechos = set(tablas_hechos)
        self.nomenclator = set(nomenclator)
        self.tablas_permitidas = self.tablas_hechos | self.nomenclator
        self.keys_restringidas = {k.lower() for k in keys_restringidas}
        # pares de tablas de hechos que no pueden aparecer juntas (universos distintos)
        self.pares_prohibidos = [set(p) for p in pares_prohibidos]
        cols = json.load(open(os.path.join(AQUI, "cols_%s.json" % censo), encoding="utf-8"))
        self.columnas_validas = {c.lower() for tabla in cols.values() for c in tabla}

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _parsear(sql):
        try:
            arboles = [a for a in sqlglot.parse(sql, read="sqlite") if a is not None]
        except Exception as e:
            raise SQLNoSeguro("No se pudo parsear la consulta: %s" % e)
        if len(arboles) != 1:
            raise SQLNoSeguro("Debe ser una sola sentencia SQL.")
        return arboles[0]

    @staticmethod
    def _cols_scope_externo(nodo, externo):
        for c in nodo.find_all(exp.Column):
            if c.find_ancestor(exp.AggFunc) is not None:
                continue
            if c.find_ancestor(exp.Select) is not externo:
                continue
            yield c

    @staticmethod
    def _ctes(arbol):
        """Mapa nombre de CTE -> tablas reales que consulta su cuerpo.

        Un CTE no es una tabla: es un nombre para una subconsulta. Hay que
        permitirlo (el modelo los usa para calcular porcentajes) pero SIN perder
        de vista qué tablas hay abajo, porque si no un `WITH x AS (SELECT ...
        FROM personas_1996) SELECT ... FROM x JOIN viviendas_1996` esquivaría la
        regla de universos distintos.
        """
        m = {}
        for cte in arbol.find_all(exp.CTE):
            nombre = (cte.alias or "").lower()
            if nombre:
                m[nombre] = {t.name.lower() for t in cte.find_all(exp.Table)}
        # un CTE puede apoyarse en otro: se expande hasta estabilizar
        for _ in range(len(m)):
            for nombre, tablas in m.items():
                expandido = set()
                for t in tablas:
                    expandido |= m.get(t, {t})
                m[nombre] = expandido
        return m

    @staticmethod
    def _tablas_por_ambito(arbol, ctes=None):
        """Tablas que cuelgan del FROM/JOIN de cada SELECT, por separado.

        Sirve para distinguir un JOIN real (dos tablas en el mismo ámbito) de dos
        subconsultas independientes (cada una con su tabla, en ámbitos distintos).
        """
        ambitos = []
        for sel in list(arbol.find_all(exp.Select)):
            aqui = set()
            # sqlglot cambió la clave del FROM entre versiones ('from' / 'from_').
            # Si se lee la equivocada esto devuelve conjuntos vacíos y el control
            # de universos deja de aplicarse EN SILENCIO: se prueban las dos.
            fuente = sel.args.get("from_") or sel.args.get("from")
            if fuente is not None:
                aqui |= {t.name.lower() for t in fuente.find_all(exp.Table)
                         if t.find_ancestor(exp.Select) is sel}
            for j in sel.args.get("joins") or []:
                aqui |= {t.name.lower() for t in j.find_all(exp.Table)
                         if t.find_ancestor(exp.Select) is sel}
            if ctes:
                expandido = set()
                for t in aqui:
                    expandido |= ctes.get(t, {t})
                aqui = expandido
            if aqui:
                ambitos.append(aqui)
        return ambitos

    @staticmethod
    def _resolver_group_by(arbol, group_exprs):
        """Expande el GROUP BY a las expresiones reales de la proyección.

        `GROUP BY 1` (ordinal) y `GROUP BY <alias de salida>` son SQL válido y
        agrupan igual que nombrar la columna, pero si se comparan literalmente
        contra la proyección no coinciden con nada y la consulta se rechaza por
        'proyección no agregada'. El LLM alterna entre las tres formas, así que
        sin esto el rechazo aparece de forma intermitente sobre consultas
        correctas.

        Devuelve las expresiones originales MÁS las que resuelven ordinales y
        alias; nunca quita nada, así que no relaja ninguna comprobación.
        """
        proyecciones = list(arbol.expressions)
        por_alias = {p.alias.lower(): p.this for p in proyecciones
                     if isinstance(p, exp.Alias) and p.alias}
        resueltas = list(group_exprs)
        for g in group_exprs:
            # ordinal: GROUP BY 1 -> primera columna de la proyección
            if isinstance(g, exp.Literal) and g.is_int:
                i = int(g.name) - 1
                if 0 <= i < len(proyecciones):
                    p = proyecciones[i]
                    resueltas.append(p.this if isinstance(p, exp.Alias) else p)
            # alias de salida: GROUP BY codigo -> la expresión que lo produce
            elif isinstance(g, exp.Column) and not g.table:
                destino = por_alias.get(g.name.lower())
                if destino is not None:
                    resueltas.append(destino)
        # las columnas que cuelgan de lo resuelto también quedan cubiertas
        extra = []
        for r in resueltas:
            if not isinstance(r, exp.Column):
                extra.extend(r.find_all(exp.Column))
        return resueltas + extra

    @staticmethod
    def _mapa_ctes(arbol):
        """nombre de CTE -> su SELECT (el cuerpo), tomado del WITH de la raíz."""
        cuerpos = {}
        con = arbol.args.get("with_") or arbol.args.get("with")
        if con:
            for cte in con.expressions:
                if isinstance(cte.this, exp.Select):
                    cuerpos[cte.alias_or_name.lower()] = cte.this
        return cuerpos

    @staticmethod
    def _cuerpos_de_fuentes(sel, ctes):
        """Los SELECT que alimentan a `sel`: cuerpos de los CTE que cita y
        subconsultas derivadas del FROM/JOIN.

        Devuelve None si alguna fuente NO es uno de esos —es decir, si el ámbito
        lee una tabla de hechos directamente—, que es el caso en que la
        proyección sí puede estar devolviendo personas.
        """
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

    @staticmethod
    def _alias_de_tablas(arbol):
        m = {}
        for t in arbol.find_all(exp.Table):
            real = t.name.lower()
            m[real] = real
            if t.alias:
                m[t.alias.lower()] = real
        return m

    @staticmethod
    def _aplicar_limite(arbol):
        lim = arbol.args.get("limit")
        if lim is None:
            return arbol.limit(LIMITE_MAXIMO)
        try:
            n = int(lim.expression.name)
        except (AttributeError, ValueError):
            raise SQLNoSeguro("LIMIT no numérico.")
        if n > LIMITE_MAXIMO:
            raise SQLNoSeguro("LIMIT excede el máximo de %d." % LIMITE_MAXIMO)
        return arbol

    def _col_libre(self, sel):
        """Primera columna de la proyección de `sel` que no está agregada ni en su
        GROUP BY; None si están todas cubiertas. Las columnas del nomenclátor no
        cuentan: son lookup (nombre de un código), no microdato."""
        alias_tab = self._alias_de_tablas(sel)
        grupo = sel.args.get("group")
        group_exprs = self._resolver_group_by(sel, grupo.expressions if grupo else [])
        group_nombres = {g.name.lower() for g in group_exprs if isinstance(g, exp.Column)}
        group_sql = {g.sql(dialect="sqlite").lower() for g in group_exprs}
        for proj in sel.expressions:
            for c in self._cols_scope_externo(proj, sel):
                if c.name.lower() in group_nombres:
                    continue
                if c.sql(dialect="sqlite").lower() in group_sql:
                    continue
                if c.table and alias_tab.get(c.table.lower()) in self.nomenclator:
                    continue
                return c
        return None

    def _es_scope_agregado(self, sel, ctes, prof=0):
        """¿Las filas que devuelve `sel` son celdas agregadas y no personas?

        Lo son si resume (GROUP BY, o una única fila de agregados) y además
        ninguna columna de su proyección se escapa de ese resumen. Es la misma
        exigencia que se le hace a la consulta de salida, aplicada a la fuente.
        """
        if prof > 4 or not isinstance(sel, exp.Select):
            return False
        resume = bool(sel.args.get("group")) or any(sel.find_all(exp.AggFunc))
        if resume and self._col_libre(sel) is None:
            return True
        # no agrega por sí mismo (o se le escapa una columna): solo sirve si lo
        # que él lee ya venía agregado (cadenas de CTE)
        cuerpos = self._cuerpos_de_fuentes(sel, ctes)
        return bool(cuerpos) and all(
            self._es_scope_agregado(c, ctes, prof + 1) for c in cuerpos)

    def _fuentes_ya_agregadas(self, sel, ctes):
        cuerpos = self._cuerpos_de_fuentes(sel, ctes)
        return bool(cuerpos) and all(self._es_scope_agregado(c, ctes) for c in cuerpos)

    def _nombres_conteo(self, sel, ctes, prof=0):
        """Nombres de salida de `sel` que son un COUNT, propagando por los CTE.

        Sin esto, cuando el conteo crudo se calcula en un CTE y la consulta de
        salida solo lo arrastra, no se identifica ninguna columna de conteo y la
        supresión de celdas chicas se dejaría de aplicar EN SILENCIO.
        """
        if prof > 4 or not isinstance(sel, exp.Select):
            return set()
        heredados = set()
        for cuerpo in (self._cuerpos_de_fuentes(sel, ctes) or []):
            heredados |= self._nombres_conteo(cuerpo, ctes, prof + 1)
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

    @staticmethod
    def _identificar_conteos(arbol):
        """Alias de las columnas de la proyección externa que son COUNT.
        A las que no tienen alias les inyecta uno determinista para poder ubicar
        su valor en las filas al aplicar la supresión."""
        nombres = []
        for i, proj in enumerate(list(arbol.expressions)):
            interno = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(interno, exp.Count):
                if isinstance(proj, exp.Alias):
                    nombres.append(proj.alias)
                else:
                    alias = "conteo_%d" % i
                    proj.replace(exp.alias_(proj.copy(), alias))
                    nombres.append(alias)
        return nombres

    # -- validación -------------------------------------------------------
    def validar(self, sql):
        """Devuelve (sql_seguro, columnas_de_conteo). Lanza SQLNoSeguro (fail-closed)."""
        arbol = self._parsear(sql)

        if not isinstance(arbol, exp.Select):
            raise SQLNoSeguro("Solo se permiten consultas SELECT.")

        for star in arbol.find_all(exp.Star):
            if star.find_ancestor(exp.AggFunc) is None:
                raise SQLNoSeguro(
                    "SELECT * prohibido: los microdatos solo se consultan agregados.")

        ctes = self._ctes(arbol)
        tablas = {t.name.lower() for t in arbol.find_all(exp.Table)}
        if not tablas:
            raise SQLNoSeguro("No se detectó tabla de origen.")
        for t in tablas:
            if t in ctes:
                continue   # nombre de CTE: lo que importa son las tablas de su cuerpo
            if t not in self.tablas_permitidas:
                raise SQLNoSeguro("Tabla no permitida: %s" % t)

        # El par prohibido se evalúa POR ÁMBITO, no sobre el árbol entero: lo que
        # no se puede es VINCULAR las dos tablas (mismo FROM/JOIN, que produciría
        # filas cruzando universos distintos). Dos subconsultas independientes
        # —"cuántos hogares y cuántas viviendas desocupadas"— son dos conteos lado
        # a lado, no una mezcla, y sí se permiten.
        for scope in self._tablas_por_ambito(arbol, ctes):
            for par in self.pares_prohibidos:
                if par <= scope:
                    raise SQLNoSeguro(
                        "Prohibido vincular %s en la misma consulta (universos "
                        "distintos); consultalas por separado."
                        % " con ".join(sorted(par)))

        alias_salida = {a.alias.lower() for a in arbol.find_all(exp.Alias) if a.alias}
        permitidas = self.columnas_validas | alias_salida
        for c in arbol.find_all(exp.Column):
            if c.name.lower() not in permitidas:
                raise SQLNoSeguro("Columna no permitida: %s" % c.name)

        for f in arbol.find_all(exp.Anonymous):
            if f.name.lower() in _FUNCS_PROHIBIDAS:
                raise SQLNoSeguro("Función no permitida: %s" % f.name)

        if not list(arbol.find_all(exp.Count)):
            raise SQLNoSeguro(
                "Consulta sin COUNT: falta el conteo crudo por celda para la supresión.")

        # solo agregados en la proyección externa; se permiten columnas del
        # nomenclátor sin agregar (son lookup, no microdato)
        ctes_cuerpos = self._mapa_ctes(arbol)
        libre = self._col_libre(arbol)
        # una columna suelta NO es una persona si la fila que la trae ya es una
        # celda agregada: es el caso del CTE que cuenta y la consulta de salida
        # que solo calcula el porcentaje sobre ese conteo.
        salida_desde_agregado = libre is not None and self._fuentes_ya_agregadas(
            arbol, ctes_cuerpos)
        if libre is not None and not salida_desde_agregado:
            raise SQLNoSeguro(
                "Proyección no agregada: la columna '%s' no está agregada ni en el "
                "GROUP BY." % libre.name)

        for proj in arbol.expressions:
            for c in proj.find_all(exp.Column):
                if c.name.lower() not in self.keys_restringidas:
                    continue
                if c.find_ancestor(exp.Select) is not arbol:
                    continue
                cnt = c.find_ancestor(exp.Count)
                dist = c.find_ancestor(exp.Distinct)
                if cnt is None or dist is None or dist.find_ancestor(exp.Count) is not cnt:
                    raise SQLNoSeguro(
                        "%s en la proyección externa solo puede aparecer dentro de "
                        "COUNT(DISTINCT ...)." % c.name)

        try:
            columnas_conteo = self._identificar_conteos(arbol)
            if salida_desde_agregado:
                heredados = set()
                for cuerpo in (self._cuerpos_de_fuentes(arbol, ctes_cuerpos) or []):
                    heredados |= self._nombres_conteo(cuerpo, ctes_cuerpos)
                for p in arbol.expressions:
                    interno = p.this if isinstance(p, exp.Alias) else p
                    nombre = p.alias if isinstance(p, exp.Alias) else getattr(p, "name", "")
                    if (nombre and isinstance(interno, exp.Column)
                            and interno.name.lower() in heredados
                            and nombre not in columnas_conteo):
                        columnas_conteo.append(nombre)
        except Exception as e:
            raise SQLNoSeguro("No se pudieron identificar los conteos: %s" % e)

        # el conteo crudo tiene que llegar a la salida: si no, no hay con qué
        # suprimir las celdas chicas y la consulta se rechaza (fail-closed)
        if salida_desde_agregado and not columnas_conteo:
            raise SQLNoSeguro(
                "La consulta arrastra celdas ya agregadas pero no expone el conteo "
                "crudo: sin él no puede aplicarse la supresión.")

        arbol = self._aplicar_limite(arbol)
        return arbol.sql(dialect="sqlite", comments=False), columnas_conteo


def suprimir_celdas_chicas(filas, columnas_conteo):
    """Descarta cada fila cuyo conteo crudo < UMBRAL_SUPRESION (control de divulgación).
    El n crudo por debajo del umbral nunca llega al usuario: se suprime la fila entera."""
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


GUARD_1996 = Guard(
    "1996",
    tablas_hechos=["personas_1996", "viviendas_1996"],
    nomenclator=["cod_departamentos", "cod_localidades_1963_2023", "cod_localidad_1996",
                 "cod_barrios_mvd", "cnuo96", "cota70"],
    keys_restringidas=["vivienda_key", "hogar_key", "vivienda"],
    pares_prohibidos=[("personas_1996", "viviendas_1996")],
)

GUARD_2004 = Guard(
    "2004",
    tablas_hechos=["censo2004"],
    nomenclator=["cod_departamentos", "cod_localidades_1963_2023",
                 "ref_localidades_2004", "cod_barrios_mvd"],
    keys_restringidas=["id_viv", "nro_person"],
)
