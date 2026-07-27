"""motor_historico.py — Motor de consultas común a los censos 1996 y 2004.

Pipeline idéntico al de 2011/2023: pregunta en español -> el LLM genera SQL ->
el guard valida -> se ejecuta contra la base en SOLO LECTURA -> supresión de
celdas chicas -> el LLM redacta la respuesta.

Es uno solo para los dos censos porque comparten reglas: ninguno es ponderado,
todas las cifras son conteos exactos. Lo específico de cada uno (base, esquema,
guard, reglas del prompt) entra por parámetro; ver consultar_1996.py y
consultar_2004.py.

MAPAS, SOLO DOS NIVELES: departamento y barrio de Montevideo. Son los dos únicos
marcos cuyo trazado NO cambió, así que las geometrías que ya tiene la app (las de
2011) valen tal cual para 1996 y 2004: los límites departamentales están firmes
desde antes de 1996 y los barrios son la clasificación de 1985, la misma en los
tres censos (verificado: los 62 códigos de barrio calzan 1 a 1 con los polígonos,
y la población por código correlaciona 0,97 entre 1996 y 2004).

Secciones censales, segmentos y localidades NO se mapean: esos marcos se
redibujaron entre censos y pintar el código de 1996 sobre el polígono de 2011
daría un dato falso. La cartografía propia de esos años que tenemos son planos en
PDF, no geometrías.

La clave OpenAI la toma del entorno; no se escribe en ningún archivo.
"""
import os
import re
import sqlite3

from openai import OpenAI

import usage_log
import registro
from sql_guard_historicos import SQLNoSeguro, UMBRAL_SUPRESION, LIMITE_MAXIMO
from comun import pipeline, rechazos, sinonimos

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

_SECCIONES = None


def secciones_con_poligono():
    """codsec (dpto*100+secc) que existen en secciones_censales.geojson.

    Se usa como validación: si una consulta devuelve un código de sección sin
    polígono, NO se dibuja el mapa. Sin este control esa fila desaparecería del
    mapa sin aviso, que es peor que no tener mapa. Se lee una sola vez.
    """
    global _SECCIONES
    if _SECCIONES is None:
        import json
        ruta = os.path.join(AQUI, "app", "static", "secciones_censales.geojson")
        try:
            with open(ruta, encoding="utf-8") as fh:
                _SECCIONES = {f["properties"]["codsec"] for f in json.load(fh)["features"]}
        except (OSError, KeyError, ValueError):
            _SECCIONES = set()
    return _SECCIONES


# Código censal de departamento -> nombre tal cual está en departamentos.geojson
# (MAYÚSCULAS sin tilde). Es el mismo orden 01..19 del nomenclátor del INE.
DEPARTAMENTOS = {
    1: "MONTEVIDEO", 2: "ARTIGAS", 3: "CANELONES", 4: "CERRO LARGO", 5: "COLONIA",
    6: "DURAZNO", 7: "FLORES", 8: "FLORIDA", 9: "LAVALLEJA", 10: "MALDONADO",
    11: "PAYSANDU", 12: "RIO NEGRO", 13: "RIVERA", 14: "ROCHA", 15: "SALTO",
    16: "SAN JOSE", 17: "SORIANO", 18: "TACUAREMBO", 19: "TREINTA Y TRES",
}

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

MAPAS (tres niveles, ver abajo): si el desglose es por DEPARTAMENTO, por SECCIÓN CENSAL o
por BARRIO DE MONTEVIDEO, agregá el código de la unidad geográfica con el alias EXACTO
'geo_codigo' y su nombre con alias 'geo_nombre' (del nomenclátor), y agrupá por el código:
  - por departamento -> CAST(dpto AS INTEGER) AS geo_codigo, d.nombre AS geo_nombre,
    con JOIN cod_departamentos d ON <tabla>.dpto = d.dpto ... GROUP BY dpto, d.nombre
  - por barrio de Montevideo -> CAST(barrio AS INTEGER) AS geo_codigo, b.nombre AS geo_nombre,
    con JOIN cod_barrios_mvd b ON CAST(<tabla>.barrio AS INTEGER) = CAST(b.barrio AS INTEGER),
    filtrando Montevideo (dpto='01') y CAST(barrio AS INTEGER) > 0 ... GROUP BY barrio, b.nombre
    El CAST en LAS DOS PATAS del join es OBLIGATORIO: el código de barrio no siempre está
    guardado con el mismo formato que el nomenclátor ('1' vs '01'), y sin CAST el join
    descarta EN SILENCIO los barrios de un dígito (Ciudad Vieja, Centro, Pocitos, Buceo...).
  - por sección censal -> CAST(dpto AS INTEGER)*100 + CAST(secc AS INTEGER) AS geo_codigo,
    d.nombre || ' — sección ' || CAST(secc AS INTEGER) AS geo_nombre,
    con JOIN cod_departamentos d ON <tabla>.dpto = d.dpto ... GROUP BY dpto, secc, d.nombre
NO uses geo_codigo en ningún otro corte geográfico (segmento, zona, localidad): la
aplicación no tiene la cartografía de esos marcos para este censo.
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
    "Los PORCENTAJES redondealos a un decimal (por ejemplo 6,1%), nunca los escribas con "
    "todos los decimales que trae el cálculo.\n"
    "Formato de las cifras (español rioplatense): separador de miles con PUNTO —escribí 323.114, nunca 323114— y decimales con coma. NO le pongas separador a los años ('Censo 2023', no 'Censo 2.023') ni a los códigos de sección, localidad o barrio.\n"
    "PRESENTACIÓN: si los resultados traen MÁS DE UNA FILA, presentalos SIEMPRE en una TABLA markdown (encabezado + una fila por categoría), NUNCA como lista con viñetas ni enumerados en prosa. Con una sola fila, narrala en una oración.\n"
    "NOMBRES PROPIOS: en la base los departamentos, localidades y barrios están en MAYÚSCULAS y sin tildes; escribilos con mayúscula inicial y acentuación correcta —Montevideo, Paysandú, Río Negro, San José, Tacuarembó, Treinta y Tres, Cerro Largo, Paso de los Toros, Bella Unión—, nunca en mayúsculas sostenidas. Las preposiciones y artículos internos van en minúscula (Paso de los Toros, Treinta y Tres).\n"
    "MAPAS: si el desglose es por departamento o por barrio de Montevideo, el frontend DIBUJA "
    "el mapa solo; para cualquier otro corte geográfico no hay mapa. En los dos casos: NUNCA "
    "digas que no podés mostrar un mapa ni que faltan geometrías, simplemente no lo menciones.\n"
    "Si los resultados traen 'geo_nombre', narrá con ese NOMBRE; NO menciones ni narres la "
    "columna 'geo_codigo' (es el código interno para el mapa)."
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
            "de Uruguay.\n\n%s\n\n%s\n%s\n\nGLOSARIO (sinónimos de uso corriente, "
            "compartido por los cuatro censos): %s"
            % (censo, self.esquema, REGLAS_COMUNES, reglas, sinonimos.glosario_para_prompt()))
        # Líneas "- NOMBRE | etiqueta | códigos" para inyectarle al redactor solo la
        # codificación de las variables que aparecen en el SQL, no las ~110.
        self._lineas = [ln.strip() for ln in self.esquema.splitlines()
                        if ln.lstrip().startswith("- ")]

    # -- etapas LLM -------------------------------------------------------
    def generar_sql(self, pregunta, contexto=None):
        r = client.chat.completions.create(
            model=MODELO_SQL, reasoning_effort=ESFUERZO_SQL,
            max_completion_tokens=TOPE_SQL,
            messages=[{"role": "system", "content": self.prompt_sql},
                      {"role": "user",
                       "content": pipeline.mensaje_usuario(pregunta, contexto)}])
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

    def redactar(self, pregunta, sql, filas, suprimidas, columnas_conteo, truncado=False,
                 interpretaciones=(), contexto=None):
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
            + pipeline.instruccion_redactor(interpretaciones, contexto)
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
        texto = pipeline.asegurar_declaracion(
            r.choices[0].message.content.strip(), interpretaciones, contexto)
        return texto + nota

    # -- mapa -------------------------------------------------------------
    @staticmethod
    def construir_mapa(sql, filas, suprimidas):
        """Si el SQL emitió 'geo_codigo', arma {nivel, datos:[{clave,valor}]}.

        Solo dos niveles (ver el encabezado del módulo). El nivel se decide por la
        cláusula GROUP BY, no por el valor del código: 1..19 y 1..62 se solapan y
        confundirlos pintaría el mapa equivocado.

        La clave de departamento es el NOMBRE en mayúsculas sin tilde, que es como
        vienen los polígonos de departamentos.geojson; la de barrio es el código
        'nro' del GeoJSON de barrios (el join por nombre no sirve: cada base los
        abrevia distinto).
        """
        if not filas:
            return None
        ejemplo = filas[0]
        geo_key = next((k for k in ejemplo if k.lower() == "geo_codigo"), None)
        if geo_key is None:
            return None
        m = re.search(r"group\s+by\s+(.+?)(?:\s+order\s+by\b|\s+limit\b|$)",
                      sql, re.IGNORECASE | re.DOTALL)
        clausula = m.group(1).lower() if m else ""
        # Un corte MÁS FINO que el nivel dibujable (segmento, zona, localidad)
        # pintado como departamento sería un mapa FALSO: mejor tabla sin mapa.
        if re.search(r"\b(segm|zona|loc|ccz|secpol)\b", clausula):
            return None
        if re.search(r"\bbarrio\b", clausula):
            nivel, validos = "barrio_hist", set(range(1, 63))
        elif re.search(r"\bsecc\b", clausula):
            # Sección censal: el marco es el mismo en los 4 censos (mismos códigos
            # departamento por departamento), así que valen los polígonos de 2011.
            nivel, validos = "seccion", secciones_con_poligono()
        elif re.search(r"\bdpto\b", clausula):
            nivel, validos = "departamento", set(DEPARTAMENTOS)
        else:
            return None   # fail-closed: sin corte reconocible no se dibuja nada
        if not validos:
            return None

        ignorar = {geo_key.lower(), "geo_nombre", "n_crudo", "n"}
        valor_key = None
        for pref in ("personas", "hogares", "viviendas"):
            if pref in ejemplo and isinstance(ejemplo[pref], (int, float)) \
                    and not isinstance(ejemplo[pref], bool):
                valor_key = pref
                break
        if valor_key is None:
            valor_key = next((k for k, v in ejemplo.items()
                              if k.lower() not in ignorar
                              and isinstance(v, (int, float)) and not isinstance(v, bool)),
                             None)
        if valor_key is None:
            return None

        datos = []
        for f in filas:
            try:
                cod = int(str(f[geo_key]).strip())
            except (TypeError, ValueError):
                return None       # un código ilegible = no se sabe qué se pintaría
            if cod not in validos:
                return None       # sin polígono para ese código: no se dibuja NADA
                                  # (una unidad que falta en silencio es peor que
                                  #  no tener mapa)
            clave = DEPARTAMENTOS.get(cod) if nivel == "departamento" else cod
            if clave is None:
                return None
            datos.append({"clave": clave, "valor": f[valor_key]})
        if not datos:
            return None
        return {"nivel": nivel, "datos": datos, "suprimidas": suprimidas}

    # -- pipeline ---------------------------------------------------------
    @staticmethod
    def _ocultar_n_crudo(filas, columnas_conteo):
        """El n crudo no se muestra: revelaría lo que la supresión oculta."""
        quitar = {c.lower() for c in columnas_conteo
                  if c.lower() in ("n_crudo", "n") or c.lower().startswith("conteo_")}
        return [{k: v for k, v in f.items() if k.lower() not in quitar} for f in filas]

    def preguntar(self, texto):
        # 1. Indicador ambiguo o variable no relevada: se responde SIN llamar al
        #    modelo. Es el paso más barato del pipeline y el que evita inventar
        #    una definición por el usuario.
        corta, contexto = pipeline.antes(texto, self.censo)
        if corta is not None:
            registro.no_respondible(self.censo, texto, corta.get("motivo", "ambigua"))
            return dict(corta, veredicto=corta.get("motivo", "AMBIGUA"))

        # Nivel A: si esta misma pregunta ya se tradujo, se ahorra la llamada que
        # razona (la cara). El SQL igual pasa por el post-paso y por el guard.
        sql_crudo = pipeline.sql_cacheado(texto, self.censo, contexto)
        if sql_crudo is None:
            sql_crudo = self.generar_sql(texto, contexto)
            pipeline.recordar_sql(texto, self.censo, contexto, sql_crudo)
        if sql_crudo.strip() == "NO_RESPONDIBLE":
            registro.no_respondible(self.censo, texto)
            return {"ok": False, "sql": None, "veredicto": "NO_RESPONDIBLE",
                    "respuesta": "Esa pregunta no puede responderse con las variables "
                                 "disponibles del Censo %s." % self.censo}

        # 2. Entidades nombradas y nomenclátor cruzado. Puede terminar acá si hay
        #    que preguntar, y nunca se rotula como confidencialidad.
        try:
            sql_crudo, interpretaciones, alternativas = pipeline.sobre_sql(
                sql_crudo, self.censo, texto)
        except pipeline.EntidadNoResuelta as e:
            registro.no_respondible(self.censo, texto, e.rechazo.codigo)
            return dict(rechazos.a_respuesta(e.rechazo, sql=None),
                        veredicto=e.rechazo.codigo)

        try:
            sql_seguro, columnas_conteo = self.guard.validar(sql_crudo)
        except SQLNoSeguro as e:
            registro.rechazo(self.censo, texto, e, sql_crudo)
            return {"ok": False, "sql": sql_crudo, "veredicto": "RECHAZADO: %s" % e,
                    "respuesta": "Consulta rechazada por seguridad: %s" % e}

        # Nivel B: si este SQL exacto ya se ejecutó y redactó, no se repite ni la
        # consulta a la base ni la llamada al redactor.
        listo = pipeline.resultado_cacheado(sql_seguro, self.censo, alternativas)
        if listo is not None:
            return dict(listo, sql=sql_seguro, veredicto="OK")

        con = sqlite3.connect("file:%s?mode=ro" % self.db, uri=True)
        con.row_factory = sqlite3.Row
        try:
            filas = [dict(f) for f in con.execute(sql_seguro).fetchall()]
        finally:
            con.close()
        n_raw = len(filas)

        # 3. Supresión con la regla corregida: el cero NO es confidencialidad.
        unidad = self.unidad_conteo(columnas_conteo, sql_seguro)
        filas, suprimidas, vacias, rechazo = pipeline.sobre_filas(
            filas, columnas_conteo, unidad)
        if rechazo is not None:
            return dict(rechazos.a_respuesta(rechazo, sql=sql_seguro),
                        veredicto="OK", celdas_suprimidas=suprimidas)

        respuesta = {"ok": True, "sql": sql_seguro, "veredicto": "OK",
                     "respuesta": self.redactar(texto, sql_seguro, filas, suprimidas,
                                                columnas_conteo,
                                                truncado=n_raw >= LIMITE_MAXIMO,
                                                interpretaciones=interpretaciones,
                                                contexto=contexto),
                     "datos": self._ocultar_n_crudo(filas, columnas_conteo),
                     "celdas_suprimidas": suprimidas}
        # Otras lecturas posibles del nombre consultado (departamento vs ciudad):
        # se ofrecen junto a la respuesta, no en lugar de ella.
        if alternativas:
            respuesta["opciones"] = alternativas
        # El mapa se arma con las filas YA suprimidas: lo que no se publica en la
        # tabla tampoco se pinta.
        mapa = self.construir_mapa(sql_seguro, filas, suprimidas)
        if mapa:
            respuesta["mapa"] = mapa
        pipeline.recordar_resultado(sql_seguro, self.censo, respuesta)
        return respuesta
