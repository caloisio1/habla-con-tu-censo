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
    def _tablas_por_ambito(arbol):
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
            if aqui:
                ambitos.append(aqui)
        return ambitos

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

        tablas = {t.name.lower() for t in arbol.find_all(exp.Table)}
        if not tablas:
            raise SQLNoSeguro("No se detectó tabla de origen.")
        for t in tablas:
            if t not in self.tablas_permitidas:
                raise SQLNoSeguro("Tabla no permitida: %s" % t)

        # El par prohibido se evalúa POR ÁMBITO, no sobre el árbol entero: lo que
        # no se puede es VINCULAR las dos tablas (mismo FROM/JOIN, que produciría
        # filas cruzando universos distintos). Dos subconsultas independientes
        # —"cuántos hogares y cuántas viviendas desocupadas"— son dos conteos lado
        # a lado, no una mezcla, y sí se permiten.
        for scope in self._tablas_por_ambito(arbol):
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
        alias_tab = self._alias_de_tablas(arbol)
        grupo = arbol.args.get("group")
        group_exprs = grupo.expressions if grupo else []
        group_nombres = {g.name.lower() for g in group_exprs if isinstance(g, exp.Column)}
        group_sql = {g.sql(dialect="sqlite").lower() for g in group_exprs}
        for proj in arbol.expressions:
            for c in self._cols_scope_externo(proj, arbol):
                if c.name.lower() in group_nombres:
                    continue
                if c.sql(dialect="sqlite").lower() in group_sql:
                    continue
                if c.table and alias_tab.get(c.table.lower()) in self.nomenclator:
                    continue
                raise SQLNoSeguro(
                    "Proyección no agregada: la columna '%s' no está agregada ni en el "
                    "GROUP BY." % c.name)

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
        except Exception as e:
            raise SQLNoSeguro("No se pudieron identificar los conteos: %s" % e)

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
