"""motor_historico.py — Motor de consultas común a los censos 1996 y 2004.

Pipeline idéntico al de 2011/2023: pregunta en español -> el LLM genera SQL ->
el guard valida -> se ejecuta contra la base en SOLO LECTURA -> supresión de
celdas chicas -> el LLM redacta la respuesta.

Es uno solo para los dos censos porque comparten reglas: ninguno es ponderado,
todas las cifras son conteos exactos. Lo específico de cada uno (base, esquema,
guard, reglas del prompt) entra por parámetro; ver consultar_1996.py y
consultar_2004.py.

SIN MAPAS: la app tiene geometrías (GeoJSON) para 2011 y 2023, no para los
marcos censales de 1996 y 2004 — la cartografía de esos años que tenemos son
planos en PDF, no geometrías. Estos motores nunca devuelven la clave 'mapa'.

La clave OpenAI la toma del entorno; no se escribe en ningún archivo.
"""
import os
import re
import sqlite3

from openai import OpenAI

import usage_log
from sql_guard_historicos import (SQLNoSeguro, UMBRAL_SUPRESION, LIMITE_MAXIMO,
                                  suprimir_celdas_chicas)

AQUI = os.path.dirname(os.path.abspath(__file__))

MODELO = os.environ.get("CENSO_MODELO", "gpt-5.5")
# Configuración por ETAPA, igual que 2011 y 2023: el SQL razona (esfuerzo alto),
# el redactor solo narra (esfuerzo 'none' = "instant").
MODELO_SQL = os.environ.get("CENSO_MODELO_SQL", MODELO)
MODELO_REDACTOR = os.environ.get("CENSO_MODELO_REDACTOR", MODELO)
ESFUERZO_SQL = os.environ.get("CENSO_ESFUERZO_SQL", "high")
ESFUERZO_REDACTOR = os.environ.get("CENSO_ESFUERZO_REDACTOR", "none")
TOPE_SQL = int(os.environ.get("CENSO_TOPE_SQL", "4000"))
TOPE_REDACTOR = int(os.environ.get("CENSO_TOPE_REDACTOR", "1600"))

# Timeout ACOTADO: sin él una respuesta LLM colgada deja el hilo worker clavado
# y wedge toda la app (incidente 2026-07-06).
client = OpenAI(timeout=60.0, max_retries=2)   # OPENAI_API_KEY del entorno

REGLAS_COMUNES = """Reglas estrictas (dialecto SQLite):
- Devolvé SOLO la consulta SQL, sin explicaciones ni markdown, y UNA SOLA sentencia.
  Si la pregunta tiene dos partes (por ejemplo hogares Y viviendas desocupadas), resolvelas
  en una única consulta con subconsultas o con SUM(CASE WHEN ... THEN 1 END); nunca devuelvas
  dos SELECT separados por punto y coma.
- Solo SELECT y SIEMPRE agregado; nunca filas individuales.
- Agregá SIEMPRE COUNT(*) AS n_crudo por celda: es el conteo que habilita la supresión
  de confidencialidad. NO lo narres, es control interno.
- PERDIDOS: excluí SIEMPRE los NULL y los códigos marcados PERDIDOS en el esquema, de
  conteos, totales y denominadores. En porcentajes el denominador excluye los perdidos.
- UNIVERSO: si la variable tiene universo declarado en su etiqueta (3 años o más, mujeres
  de 15 o más, viviendas ocupadas con moradores presentes, etc.), respetalo en el filtro
  y en el denominador.
- Si la pregunta no puede responderse con este esquema, devolvé exactamente: NO_RESPONDIBLE"""

SYS_REDACTA_BASE = (
    "Respondé la pregunta usando EXCLUSIVAMENTE los datos provistos. Sé breve y preciso. "
    "No inventes cifras.\n"
    "Tu función es NARRAR los resultados provistos. NO auditás, corregís ni criticás la "
    "consulta SQL: asumila correcta y contá lo que devolvió.\n"
    "Las cifras de este censo son CONTEOS EXACTOS: narralas exactas. NO uses "
    "'aproximadamente', 'alrededor de', 'unos/unas' ni 'estimación'.\n"
    "NO escribas ninguna nota ni frase sobre celdas suprimidas, confidencialidad o secreto "
    "estadístico: el sistema agrega esa nota automáticamente al final.\n"
    "NO menciones ni narres la columna 'n_crudo' (conteo interno de control para la "
    "supresión): no aparece en la respuesta al usuario.\n"
    "NO narres las banderas de registro (per, viv, hog) ni las claves internas: son la "
    "mecánica de la tabla, no una característica de la población. Decí 'personas', "
    "'viviendas' u 'hogares' a secas.\n"
    "NO comentes sobre mapas: este censo no tiene mapas en la aplicación. Nunca digas que "
    "no podés mostrar un mapa ni que faltan geometrías; simplemente no lo menciones."
)


class Motor:
    def __init__(self, censo, db_env, db_default, esquema, guard, reglas, fuente,
                 unidad_por_defecto="personas"):
        self.censo = censo
        self.db = os.environ.get(db_env, os.path.join(AQUI, "datos", db_default))
        self.guard = guard
        self.fuente = fuente
        self.unidad_por_defecto = unidad_por_defecto
        self.esquema = open(os.path.join(AQUI, esquema), encoding="utf-8").read()
        self.prompt_sql = (
            "Sos un traductor de preguntas en español a SQL (SQLite) sobre el Censo %s "
            "de Uruguay.\n\n%s\n\n%s\n%s" % (censo, self.esquema, REGLAS_COMUNES, reglas))
        # Líneas "- NOMBRE | etiqueta | códigos" para inyectarle al redactor solo la
        # codificación de las variables que aparecen en el SQL, no las ~110.
        self._lineas = [ln.strip() for ln in self.esquema.splitlines()
                        if ln.lstrip().startswith("- ")]

    # -- etapas LLM -------------------------------------------------------
    def generar_sql(self, pregunta):
        r = client.chat.completions.create(
            model=MODELO_SQL, reasoning_effort=ESFUERZO_SQL,
            max_completion_tokens=TOPE_SQL,
            messages=[{"role": "system", "content": self.prompt_sql},
                      {"role": "user", "content": pregunta}])
        usage_log.registrar(self.censo, "sql", getattr(r, "usage", None),
                            MODELO_SQL, ESFUERZO_SQL)
        return r.choices[0].message.content.strip()

    # Mecánica interna de la tabla: no son categorías que le interesen a quien
    # pregunta. Si entran en la leyenda, el redactor las narra ("registro de
    # persona = Sí") y la respuesta queda hablando de la estructura del dato.
    INTERNAS = {"viv", "hog", "per", "hogar_key", "vivienda_key", "n_crudo"}

    def leyenda_codificaciones(self, sql):
        sql_low = sql.lower()
        out = []
        for ln in self._lineas:
            nombre = ln[2:].split("|", 1)[0].strip()
            if nombre.lower() in self.INTERNAS:
                continue
            if nombre and re.search(r"\b%s\b" % re.escape(nombre.lower()), sql_low):
                out.append(ln)
        return "\n".join(out)

    def unidad_conteo(self, columnas_conteo, sql):
        cols = {c.lower() for c in columnas_conteo}
        sql_low = (sql or "").lower()
        if "hogares" in cols or "hogar_key" in sql_low or "hog=1" in sql_low.replace(" ", ""):
            return "hogares"
        if "viviendas" in cols or "viviendas_1996" in sql_low or "viv=1" in sql_low.replace(" ", ""):
            return "viviendas"
        return self.unidad_por_defecto

    def redactar(self, pregunta, sql, filas, suprimidas, columnas_conteo, truncado=False):
        unidad = self.unidad_conteo(columnas_conteo, sql)
        nota = ("\nNota: %d celda(s) con menos de %d %s fueron suprimidas por "
                "confidencialidad." % (suprimidas, UMBRAL_SUPRESION, unidad)
                if suprimidas else "")
        leyenda = self.leyenda_codificaciones(sql)
        aviso_trunc = ("\nATENCIÓN: los resultados están RECORTADOS por un límite de filas "
                       "(LIMIT): NO son el universo completo. No afirmes que un valor es el "
                       "máximo, el mínimo, el mayor ni el único; describí solo lo que muestran "
                       "las filas." if truncado else "")
        sys_prompt = (
            SYS_REDACTA_BASE
            + "\nFuente: '%s'." % self.fuente
            + "\nUnidad de análisis de esta consulta: %s; nombrala al narrar, no "
              "'personas' por defecto." % unidad
            + aviso_trunc
            + (("\nCodificaciones de esta consulta (respetalas al narrar):\n" + leyenda)
               if leyenda else "")
        )
        r = client.chat.completions.create(
            model=MODELO_REDACTOR, reasoning_effort=ESFUERZO_REDACTOR,
            max_completion_tokens=TOPE_REDACTOR,
            messages=[{"role": "system", "content": sys_prompt},
                      {"role": "user",
                       "content": "Pregunta: %s\nSQL: %s\nResultados: %s"
                                  % (pregunta, sql, filas)}])
        usage_log.registrar(self.censo, "redactor", getattr(r, "usage", None),
                            MODELO_REDACTOR, ESFUERZO_REDACTOR)
        return r.choices[0].message.content.strip() + nota

    # -- pipeline ---------------------------------------------------------
    @staticmethod
    def _ocultar_n_crudo(filas, columnas_conteo):
        """El n crudo no se muestra: revelaría lo que la supresión oculta."""
        quitar = {c.lower() for c in columnas_conteo
                  if c.lower() in ("n_crudo", "n") or c.lower().startswith("conteo_")}
        return [{k: v for k, v in f.items() if k.lower() not in quitar} for f in filas]

    def preguntar(self, texto):
        sql_crudo = self.generar_sql(texto)
        if sql_crudo.strip() == "NO_RESPONDIBLE":
            return {"ok": False, "sql": None, "veredicto": "NO_RESPONDIBLE",
                    "respuesta": "Esa pregunta no puede responderse con las variables "
                                 "disponibles del Censo %s." % self.censo}
        try:
            sql_seguro, columnas_conteo = self.guard.validar(sql_crudo)
        except SQLNoSeguro as e:
            return {"ok": False, "sql": sql_crudo, "veredicto": "RECHAZADO: %s" % e,
                    "respuesta": "Consulta rechazada por seguridad: %s" % e}

        con = sqlite3.connect("file:%s?mode=ro" % self.db, uri=True)
        con.row_factory = sqlite3.Row
        try:
            filas = [dict(f) for f in con.execute(sql_seguro).fetchall()]
        finally:
            con.close()
        n_raw = len(filas)

        filas, suprimidas = suprimir_celdas_chicas(filas, columnas_conteo)
        if not filas:
            return {"ok": False, "sql": sql_seguro, "veredicto": "OK",
                    "celdas_suprimidas": suprimidas,
                    "respuesta": "La consulta no devolvió resultados publicables"
                                 + (" (celdas suprimidas por confidencialidad)."
                                    if suprimidas else ".")}

        return {"ok": True, "sql": sql_seguro, "veredicto": "OK",
                "respuesta": self.redactar(texto, sql_seguro, filas, suprimidas,
                                           columnas_conteo,
                                           truncado=n_raw >= LIMITE_MAXIMO),
                "datos": self._ocultar_n_crudo(filas, columnas_conteo),
                "celdas_suprimidas": suprimidas}
