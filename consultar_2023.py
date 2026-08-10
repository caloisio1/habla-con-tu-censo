"""consultar_2023.py — Motor de consultas del Censo 2023 (versión ponderada del INE).

Pipeline (análogo al motor 2011): pregunta ES -> LLM genera SQL -> guard 2023 valida
-> ejecuta contra censo2023.db (SOLO LECTURA) -> supresión <5 -> LLM redacta.
Interfaz `preguntar(texto)` que usa el servicio unificado cuando el selector elige 2023.
La clave del LLM la toma del entorno; no se escribe en ningún archivo.
"""
import os, sys, re, json

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
from sql_guard_2023 import validar, suprimir_celdas_chicas, SQLNoSeguro, UMBRAL_SUPRESION, LIMITE_MAXIMO
import registro
import usage_log
from comun import ejecutor, llm, pipeline, rechazos, sinonimos

DB = os.environ.get("CENSO2023_DB", os.path.join(AQUI, "datos", "censo2023.db"))
MODELO = os.environ.get("CENSO_MODELO", llm.MODELO_POR_DEFECTO)
# Configuración por ETAPA (modelo + esfuerzo de razonamiento), sobreescribible por entorno.
# El SQL es la etapa que RAZONA (traducir la pregunta al esquema): esfuerzo alto.
# El redactor solo NARRA los resultados ya calculados: esfuerzo bajo, más barato y
# más rápido. El vocabulario (none|low|medium|high) es el de gpt-5.5.
MODELO_SQL = os.environ.get("CENSO_MODELO_SQL", MODELO)
MODELO_REDACTOR = os.environ.get("CENSO_MODELO_REDACTOR", MODELO)
ESFUERZO_SQL = os.environ.get("CENSO_ESFUERZO_SQL", "high")
ESFUERZO_REDACTOR = os.environ.get("CENSO_ESFUERZO_REDACTOR", "low")
# Con esfuerzo alto el razonamiento consume presupuesto de salida: el tope del SQL sube
# para que la consulta nunca salga vacía por finish_reason=length (incidente 2026-07-06).
TOPE_SQL = int(os.environ.get("CENSO_TOPE_SQL", "4000"))
TOPE_REDACTOR = int(os.environ.get("CENSO_TOPE_REDACTOR", "1600"))
ESQUEMA = open(os.path.join(AQUI, "esquema_llm_2023.txt"), encoding="utf-8").read()
# El cliente (proveedor, clave, timeout acotado y reintentos) vive en comun/llm.py,
# compartido por los cuatro censos.

REGLAS = """Reglas estrictas (dialecto SQLite):
- Devolvé SOLO la consulta SQL, sin explicaciones ni markdown.
- Solo SELECT y SIEMPRE agregado; nunca filas individuales.
- OBLIGATORIO EN TODA CONSULTA, SIN EXCEPCIÓN: COUNT(*) AS n_crudo como última columna del
  SELECT (una por celda si hay GROUP BY). Va ADEMÁS de la métrica, nunca en su lugar. Sin esa
  columna el guard RECHAZA la consulta y el usuario se queda sin respuesta, aunque el resto
  del SQL sea correcto. Revisá antes de responder que la columna esté.
- PERSONAS: la cifra publicada es SUM(W) (redondeada); agregá SIEMPRE COUNT(*) AS n_crudo
  (conteo sin ponderar) para la supresión de celdas chicas. NUNCA presentes COUNT(*) como
  cantidad de personas.
- HOGARES: usá la columna derivada hogar_key directamente -> COUNT(DISTINCT hogar_key) AS hogares
  con hogar_key IS NOT NULL (ya viene NULL fuera de UNIVERSO 1/2). NO reconstruyas la clave desde
  DIRECCION_ID/VIVID/HOGID. Agregá COUNT(*) AS n_crudo.
- VIVIENDAS: consultá la tabla viviendas_2023 y usá COUNT(*) (esa tabla no tiene ponderador).
- W es el PONDERADOR (siempre válido): NO le apliques filtros de perdidos (nada de W IN (7777,...)).
- SEXO es PERPH02 (1=Varón, 2=Mujer). "Mujeres" es PERPH02=2. NO lo confundas con PERMI01,
  que es LUGAR DE NACIMIENTO: los prefijos se parecen y filtrar PERMI01=2 por "mujeres" da
  un universo equivocado (o vacío). Ante cualquier variable, verificá la etiqueta en el
  esquema antes de usar su código.
- PROHIBIDO unir o mezclar personas_2023 con viviendas_2023 (ni JOIN ni subconsulta).
- JOIN permitido solo con el nomenclátor. Para responder por NOMBRE de localidad:
    JOIN localidades_2023 l ON (personas_2023.DEPARTAMENTO||personas_2023.LOCALIDAD)=l.codloc
  y filtrá/agrupá por l.nombre. Departamento por nombre: JOIN departamentos_2023 por codigo.
- Los nombres del nomenclátor (localidades_2023.nombre, departamentos_2023.nombre) están en
  MAYÚSCULAS y SIN tildes (ej: 'PASO DE LOS TOROS', 'FLORES', 'PAYSANDU', 'BELLA UNION').
  Escribí los literales así.
- PERDIDOS: excluí SIEMPRE los NULL (y códigos perdidos) de conteos, totales y denominadores.
  En porcentajes el denominador excluye NULL (usá SUM(CASE WHEN ... THEN W END)/SUM(W) sobre
  filas con la variable no nula).
- Identificadores (vivienda_key, hogar_key, DIRECCION_ID, VIVID, HOGID, PERID, ID_HOGAR):
  libres en subconsultas/GROUP BY interno, PROHIBIDOS en el SELECT externo salvo dentro de
  COUNT(DISTINCT ...).
- TRAMOS Y CATEGORÍAS DERIVADAS (edad quinquenal o decenal, rangos, cualquier columna
  construida con CASE o con aritmética). Tres reglas que van juntas:
  1) AGRUPÁ POR EL ORDINAL de la columna proyectada -> GROUP BY 1, nunca repitiendo la
     expresión. Para que los tramos salgan en orden de edad y no alfabético ('10-14'
     antes que '5-9'), ordená con ORDER BY MIN(PERNA01). Repetir la expresión en el
     GROUP BY, o poner PERNA01 suelta en el ORDER BY, hace que la consulta se resuelva
     por el camino lento: responde igual pero tarda decenas de segundos en vez de décimas.
  2) PARENTIZÁ cada operando y convertilo con CAST(... AS TEXT) antes de concatenar con
     '||'. Sin eso, '||' se combina con '*' de forma distinta según el motor y la
     etiqueta del tramo sale como un número suelto (20) en lugar del rango ('20-24').
  3) DIVISIÓN: escribí CAST(FLOOR(PERNA01/5.0) AS INTEGER)*5, nunca (PERNA01/5)*5. La
     división entera no da el mismo resultado en todos los motores y el tramo sale
     '20.0-24.0'.
  Bien: SELECT CASE WHEN PERNA01 >= 95 THEN '95 y más'
               ELSE CAST(CAST(FLOOR(PERNA01/5.0) AS INTEGER)*5 AS TEXT) || '-' ||
                    CAST(CAST(FLOOR(PERNA01/5.0) AS INTEGER)*5+4 AS TEXT) END AS tramo,
               ROUND(SUM(W)) AS personas, COUNT(*) AS n_crudo
        FROM personas_2023 WHERE PERNA01 NOT IN (7777, 8888, 9898, 9999)
        GROUP BY 1 ORDER BY MIN(PERNA01)
  Para tramos de 10 en 10 es lo mismo cambiando 5.0 por 10.0 y el +4 por +9.
PATRONES DE MAPA (desglose geográfico): si la pregunta pide "por" o "en cada"
departamento / sección / segmento / localidad, o pide un mapa, agrupá por esa unidad
y devolvé como PRIMERA columna el CÓDIGO COMPUESTO con alias EXACTO 'geo_codigo',
además de la métrica y del COUNT(*) AS n_crudo (para la supresión):
- por departamento -> JOIN departamentos_2023 d ON DEPARTAMENTO=d.codigo ;
     SELECT d.codigo AS geo_codigo, d.nombre AS geo_nombre, <metrica>, COUNT(*) AS n_crudo ... GROUP BY d.codigo, d.nombre
- por localidad    -> JOIN localidades_2023 l ON (DEPARTAMENTO||LOCALIDAD)=l.codloc ;
     SELECT l.codloc AS geo_codigo, l.nombre AS geo_nombre, <metrica>, COUNT(*) AS n_crudo ... GROUP BY l.codloc, l.nombre
- por sección      -> geo_codigo = DEPARTAMENTO||SECCION            ... GROUP BY DEPARTAMENTO, SECCION
- por segmento     -> geo_codigo = DEPARTAMENTO||SECCION||SEGMENTO  ... GROUP BY DEPARTAMENTO, SECCION, SEGMENTO
- por barrio (Montevideo) -> personas_2023.BARRIO85 es el NOMBRE del barrio (texto), NO un código;
     JOIN barrios_mvd_2023 b ON personas_2023.BARRIO85 = b.nombre  (NUNCA BARRIO85=b.codbarrio) ;
     SELECT b.codbarrio AS geo_codigo, b.nombre AS geo_nombre, <metrica>, COUNT(*) AS n_crudo
     ... WHERE personas_2023.BARRIO85 IS NOT NULL ... GROUP BY b.codbarrio, b.nombre
     (los barrios existen solo en Montevideo; BARRIO85 ya viene NULL fuera de Montevideo).
Para departamento, localidad y barrio DEBÉS traer el NOMBRE con alias 'geo_nombre' (JOIN al nomenclátor:
departamentos_2023(codigo,nombre) / localidades_2023(codloc,nombre)), para que la respuesta narre con
nombres y no con códigos pelados. <metrica> es SUM(W) redondeada (personas), COUNT(DISTINCT hogar_key)
(hogares), COUNT(*) (viviendas, consultando viviendas_2023) o el % según la pregunta. Las mismas columnas
geográficas existen en viviendas_2023 (que también puede unirse al nomenclátor). geo_codigo (el código
compuesto) es OBLIGATORIO para el mapa.
LUGAR DE NACIMIENTO Y MIGRACIÓN INTERNA (el censo relevó lugar de nacimiento; estas preguntas SÍ se responden):
- PERMI01 = lugar de nacimiento: 1=en este departamento, 3=en otro departamento, 4=en otro país.
- Nacidos en el EXTERIOR: WHERE PERMI01=4; el país está en el código PERMI01_4. Para filtrar o
  desglosar por país, JOIN paises ON personas_2023.PERMI01_4 = paises.codigo y usá paises.nombre
  (MAYÚSCULAS sin acento: 'PARAGUAY','VENEZUELA','ESPAÑA'...). Métrica: SUM(W) + COUNT(*) AS n_crudo.
  Ej.: nacidos en Venezuela -> WHERE PERMI01=4 AND paises.nombre='VENEZUELA'.
- "nacidos en el exterior por país" -> JOIN paises ... GROUP BY paises.nombre (sin mapa).
- "nacidos en el exterior por departamento de residencia" -> WHERE PERMI01=4, patrón de mapa por departamento.
- DEPARTAMENTO de nacimiento: columna DEPTO_NACIM (texto SIN cero inicial: '1'..'19'). Códigos:
  1=MONTEVIDEO 2=ARTIGAS 3=CANELONES 4=CERRO LARGO 5=COLONIA 6=DURAZNO 7=FLORES 8=FLORIDA 9=LAVALLEJA
  10=MALDONADO 11=PAYSANDU 12=RIO NEGRO 13=RIVERA 14=ROCHA 15=SALTO 16=SAN JOSE 17=SORIANO 18=TACUAREMBO
  19=TREINTA Y TRES. (La columna de RESIDENCIA DEPARTAMENTO sí lleva cero inicial: '01'..'19'.)
- "nacidos en el departamento X que viven en Y": WHERE DEPARTAMENTO='<Y con cero>' AND DEPTO_NACIM='<X sin cero>'.
  Ej. Rivera->Montevideo: WHERE DEPARTAMENTO='01' AND DEPTO_NACIM='13'.
- "viven en un departamento distinto al que nacieron" (nacional): WHERE PERMI01=3.
- Si la pregunta no puede responderse con este esquema, devolvé exactamente: NO_RESPONDIBLE"""

# El glosario sale del módulo compartido: los cuatro censos leen la MISMA tabla de
# sinónimos que usa el resolver, así "NBI" o "jefatura" no se interpretan distinto
# según el censo elegido.
GLOSARIO = ("\n\nGLOSARIO (sinónimos de uso corriente, compartido por los cuatro censos): "
            + sinonimos.glosario_para_prompt())
PROMPT_SQL = ("Sos un traductor de preguntas en español a SQL (SQLite) sobre el Censo 2023 "
              "de Uruguay (versión ponderada).\n\n" + ESQUEMA + "\n\n" + REGLAS + GLOSARIO)

SYS_REDACTA = (
    "Respondé la pregunta usando EXCLUSIVAMENTE los datos provistos. Sé breve y preciso. "
    "Fuente: 'Censo 2023, INE Uruguay'. Si es un porcentaje con perdidos, aclaralo "
    "(denominador = casos con respuesta válida). Si la variable no la captan los registros "
    "administrativos (FUENTE_EXT=2), aclarar que el denominador son los relevados con "
    "cuestionario. No inventes cifras.\n"
    "Formato de las cifras (español rioplatense): separador de miles con PUNTO —escribí 323.114, nunca 323114— y decimales con coma. NO le pongas separador a los años ('Censo 2023', no 'Censo 2.023') ni a los códigos de sección, localidad o barrio.\n"
    "PRESENTACIÓN: si los resultados traen MÁS DE UNA FILA, presentalos SIEMPRE en una TABLA markdown (encabezado + una fila por categoría), NUNCA como lista con viñetas ni enumerados en prosa. Con una sola fila, narrala en una oración.\n"
    "NOMBRES PROPIOS: en la base los departamentos, localidades y barrios están en MAYÚSCULAS y sin tildes; escribilos con mayúscula inicial y acentuación correcta —Montevideo, Paysandú, Río Negro, San José, Tacuarembó, Treinta y Tres, Cerro Largo, Paso de los Toros, Bella Unión—, nunca en mayúsculas sostenidas. Las preposiciones y artículos internos van en minúscula (Paso de los Toros, Treinta y Tres)."
)

# La cifra de personas del Censo 2023 es SUM(W) (censo ponderado): una estimación.
# Viviendas (COUNT sobre viviendas_2023) y hogares (COUNT DISTINCT) son conteos EXACTOS.
_RX_SUMW = re.compile(r"\bsum\s*\(\s*[^)]*\bw\b", re.I)


def generar_sql(pregunta, contexto=None):
    r = llm.completar(modelo=MODELO_SQL, esfuerzo=ESFUERZO_SQL, tope=TOPE_SQL,
                      sistema=PROMPT_SQL,
                      usuario=pipeline.mensaje_usuario(pregunta, contexto))
    usage_log.registrar("2023", "sql", r.uso, MODELO_SQL, ESFUERZO_SQL)
    return pipeline.sql_generado(r.texto)


# Líneas "- NOMBRE | etiqueta | códigos" del esquema, indexadas para inyectarle al
# redactor SOLO la codificación de las variables presentes en el SQL (no las ~90).
_LINEAS_ESQUEMA = [ln.strip() for ln in ESQUEMA.splitlines() if ln.lstrip().startswith("- ")]


def unidad_conteo(columnas_conteo):
    """Unidad de análisis según el alias de conteo. En 2023 las personas son
    estimaciones ponderadas y la supresión es sobre el n crudo de registros, así
    que 'registros' es el término correcto salvo cuando se cuentan hogares/viviendas."""
    cols = {c.lower() for c in columnas_conteo}
    if "hogares" in cols:
        return "hogares"
    if "viviendas" in cols:
        return "viviendas"
    return "personas"


def leyenda_codificaciones(sql):
    """Codificaciones (etiqueta + códigos) SOLO de las variables presentes en el
    SQL ejecutado, tomadas del esquema 2023. Acota el costo en tokens por consulta."""
    sql_low = sql.lower()
    out = []
    for ln in _LINEAS_ESQUEMA:
        nombre = ln[2:].split("|", 1)[0].strip()
        if nombre and re.search(rf"\b{re.escape(nombre.lower())}\b", sql_low):
            out.append(ln)
    return "\n".join(out)


def redactar(pregunta, sql, filas, suprimidas, columnas_conteo, truncado=False,
             interpretaciones=(), contexto=None):
    unidad = unidad_conteo(columnas_conteo)
    palabra = unidad if unidad in ("hogares", "viviendas") else "registros"
    nota = (f"\nNota: {suprimidas} celda(s) con menos de {UMBRAL_SUPRESION} {palabra} "
            "fueron suprimidas por confidencialidad." if suprimidas else "")
    leyenda = leyenda_codificaciones(sql)
    es_ponderada = bool(_RX_SUMW.search(sql or ""))
    # (c) Ninguna cifra se matiza en la frase. Las de personas salen del censo
    # ponderado, pero son las cifras PUBLICADAS por el INE: decir "aproximadamente
    # 619 personas" las presenta como una conjetura nuestra y no como el dato
    # oficial que son. La ponderación se declara una vez, en la nota al pie que
    # agrega main.py (PONDERACION_2023), que es donde corresponde: el método se
    # documenta, no se repite como hedge en cada oración.
    if es_ponderada:
        regla_cifra = ("\nLas cantidades de PERSONAS salen del censo ponderado del INE y son las "
                       "cifras PUBLICADAS: redondealas y narralas COMO SON, en afirmativo. "
                       "PROHIBIDO usar 'aproximadamente', 'alrededor de', 'unos/unas', 'cerca de', "
                       "'estimación', 'estimado', 'se estima' o cualquier otro matiz de "
                       "incertidumbre. Escribí 'viven 619 personas', no 'viven aproximadamente "
                       "619 personas'.")
    else:
        regla_cifra = ("\nLas cifras de esta consulta son CONTEOS EXACTOS (viviendas u hogares): "
                       "narralas EXACTAS. NO uses 'aproximadamente', 'alrededor de', 'unos/unas' "
                       "ni 'estimación'.")
    # (d) Resultado recortado por el LIMIT: no narrar extremos como si fueran del universo.
    aviso_trunc = ("\nATENCIÓN: los resultados están RECORTADOS por un límite de filas (LIMIT): NO "
                   "son el universo completo. No afirmes que un valor es el máximo, el mínimo, el "
                   "mayor, el menor ni el único; describí solo lo que muestran las filas."
                   if truncado else "")
    sys_prompt = (
        SYS_REDACTA
        + regla_cifra
        + "\nCÓDIGOS: cuando venga la leyenda de codificaciones, nombrá SIEMPRE la etiqueta "
          "y nunca el número pelado. Esa leyenda es el diccionario de la variable: ya lo "
          "tenés acá. NO inventes limitaciones —no digas que no revisaste el diccionario ni "
          "que te falta el codebook— y no ofrezcas buscar archivos ni 'lanzar otra consulta': "
          "no ejecutás nada, solo narrás lo que ya está en este mensaje."
        + "\nTu función es NARRAR los resultados provistos. NO auditás, corregís ni "
          "critiques la consulta SQL: asumila correcta y contá lo que devolvió."
        + "\nNO comentes sobre disponibilidad de mapas (el sistema agrega esa aclaración "
          "por separado): NUNCA digas que no podés mostrar un mapa ni que faltan geometrías."
        + "\n(b) NO escribas ninguna nota, aclaración ni frase sobre celdas suprimidas, "
          "confidencialidad o secreto estadístico: el sistema agrega esa nota automáticamente "
          "al final; no la escribas vos ni la repitas."
        + f"\nUnidad de análisis de esta consulta: {unidad}; nombrala al narrar, no 'personas' por defecto."
        + "\nSi los resultados traen 'geo_nombre' (nombre de la unidad geográfica), narrá con ese "
          "NOMBRE, nunca con el código pelado ('geo_codigo'). No menciones la columna geo_codigo."
        + "\nNO menciones ni narres la columna 'n_crudo' (conteo interno de control sin ponderar, "
          "solo para la supresión): no aparece en la respuesta al usuario."
        + aviso_trunc
        + (("\nCodificaciones de esta consulta (respetalas al narrar):\n" + leyenda)
           if leyenda else "")
        + pipeline.instruccion_redactor(interpretaciones, contexto)
    )
    # El redactor solo NARRA: con esfuerzo bajo el razonamiento no llega a agotar el
    # presupuesto ni a vaciar la respuesta en preguntas de mapa. El tope holgado
    # (1600) es justamente ese margen: no bajarlo sin volver a medir.
    r = llm.completar(modelo=MODELO_REDACTOR, esfuerzo=ESFUERZO_REDACTOR,
                      tope=TOPE_REDACTOR, sistema=sys_prompt,
                      usuario=f"Pregunta: {pregunta}\nSQL: {sql}\nResultados: {filas}"
                              + pipeline.totales_para_redactor(filas, columnas_conteo))
    usage_log.registrar("2023", "redactor", r.uso, MODELO_REDACTOR, ESFUERZO_REDACTOR)
    texto = pipeline.asegurar_declaracion(r.texto, interpretaciones, contexto)
    return texto + nota


def _ocultar_n_crudo(filas, columnas_conteo):
    """Rule c: el n crudo no se muestra al usuario (evita revelar lo que la supresión oculta).
    Las filas que sobreviven ya tienen n>=5; igual quitamos las columnas de conteo crudo de la
    salida final salvo que sean la métrica publicada (hogares/viviendas)."""
    quitar = {c.lower() for c in columnas_conteo if c.lower() in ("n_crudo", "n", "conteo_1", "conteo_0")}
    return [{k: v for k, v in f.items() if k.lower() not in quitar} for f in filas]


# Zona contestada (Rincón de Artigas): NUNCA se mapea. La base no publica datos para
# estos códigos y el GeoJSON tampoco los trae; esto es defensa en profundidad.
_CONTESTADA = {"0200", "0200000", "02000"}
# Longitud del código compuesto geo_codigo -> nivel de mapa 2023.
_NIVEL_POR_LEN = {2: "depto_2023", 4: "seccion_2023", 5: "localidad_2023", 7: "segmento_2023"}
_NIVEL_TXT = {"depto_2023": "departamento", "seccion_2023": "sección censal",
              "localidad_2023": "localidad", "segmento_2023": "segmento censal",
              "barrio_2023": "barrio"}


def _contar_unidades_geo(sql_seguro):
    """Cuenta las unidades geográficas REALES (sin el LIMIT) de una consulta de mapa,
    para el aviso anti-truncamiento. Envuelve el SQL ya validado en un COUNT(*)."""
    base = re.sub(r"\s+limit\s+\d+\s*$", "", sql_seguro, flags=re.I)
    return ejecutor.escalar(DB, f"SELECT COUNT(*) FROM ({base})")


def construir_mapa_2023(filas, columnas_conteo, suprimidas, sql=""):
    """Replica construir_mapa (2011) para 2023: si el SQL emitió 'geo_codigo'
    (código compuesto), arma {nivel, datos:[{clave,valor}], suprimidas}. El nivel se
    infiere por la longitud del código (2 depto, 4 sección, 5 localidad, 7 segmento).
    Excepción: el barrio (codbarrio, 7 díg) COLISIONA en largo con el segmento, así que
    se detecta por el JOIN a barrios_mvd_2023 en el SQL (no por longitud).
    Las celdas suprimidas (n<5) ya no están en 'filas' -> el frontend las pinta en gris."""
    if not filas:
        return None
    ejemplo = filas[0]
    geo_key = next((k for k in ejemplo if k.lower() == "geo_codigo"), None)
    if geo_key is None:
        return None
    if "barrios_mvd_2023" in (sql or "").lower():
        nivel = "barrio_2023"   # codbarrio (7 díg) colisiona con segmento -> desambiguar por el JOIN
    else:
        nivel = _NIVEL_POR_LEN.get(len(str(ejemplo[geo_key])))
    if nivel is None:
        return None
    crudos = {"n_crudo", "n", "conteo_1", "conteo_0"}
    def es_num(v): return isinstance(v, (int, float)) and not isinstance(v, bool)
    cand = [k for k, v in ejemplo.items()
            if k != geo_key and es_num(v) and k.lower() not in crudos]
    pref = ("personas", "hogares", "viviendas", "porcentaje", "pct", "proporcion", "porc")
    valor_key = next((k for k in cand if any(p in k.lower() for p in pref)), None) \
        or (cand[0] if cand else None)
    if valor_key is None:
        return None
    datos = [{"clave": str(f[geo_key]), "valor": f[valor_key]}
             for f in filas if str(f[geo_key]) not in _CONTESTADA]
    return {"nivel": nivel, "datos": datos, "suprimidas": suprimidas}


def preguntar(texto, verbose=False):
    # Indicador ambiguo o variable no relevada: se resuelve SIN llamar al modelo.
    corta, contexto = pipeline.antes(texto, "2023")
    if corta is not None:
        registro.no_respondible("2023", texto, corta.get("motivo", "ambigua"))
        return dict(corta, veredicto=corta.get("motivo", "AMBIGUA"))

    # Nivel A de caché: ahorra la llamada que razona. El SQL igual pasa por el
    # post-paso de entidades y por el guard.
    sql_crudo = pipeline.sql_cacheado(texto, "2023", contexto)
    if sql_crudo is None:
        sql_crudo = generar_sql(texto, contexto)
        pipeline.recordar_sql(texto, "2023", contexto, sql_crudo)
    if sql_crudo.strip() == "NO_RESPONDIBLE":
        registro.no_respondible("2023", texto)
        return {"ok": False, "respuesta": "Esa pregunta no puede responderse con las variables disponibles.",
                "sql": None, "veredicto": "NO_RESPONDIBLE"}

    # Entidades nombradas: resuelve, reescribe o pregunta. Nunca rotula como
    # confidencialidad lo que es un nombre no reconocido.
    try:
        sql_crudo, interpretaciones, alternativas = pipeline.sobre_sql(
            sql_crudo, "2023", texto)
    except pipeline.EntidadNoResuelta as e:
        registro.no_respondible("2023", texto, e.rechazo.codigo)
        return dict(rechazos.a_respuesta(e.rechazo, sql=None), veredicto=e.rechazo.codigo)

    try:
        sql_seguro, columnas_conteo = validar(sql_crudo)
    except SQLNoSeguro as e:
        registro.rechazo("2023", texto, e, sql_crudo)
        return {"ok": False, "respuesta": f"Consulta rechazada por seguridad: {e}",
                "sql": sql_crudo, "veredicto": f"RECHAZADO: {e}"}

    # Nivel B: mismo SQL ya ejecutado y redactado -> ni base ni redactor.
    listo = pipeline.resultado_cacheado(sql_seguro, "2023", alternativas)
    if listo is not None:
        return dict(listo, sql=sql_seguro, veredicto="OK")

    try:
        filas = ejecutor.filas(DB, sql_seguro)
    except ejecutor.ConsultaIncoherente as e:
        # El SQL compara texto con numero. SQLite lo contestaria con una cifra
        # falsa (0 % en todos lados); se rechaza en vez de contestar mal.
        return dict(rechazos.a_respuesta(rechazos.procesamiento(str(e)), sql=sql_seguro),
                    veredicto="RECHAZADO: consulta incoherente")
    n_geo_raw = len(filas)   # filas antes de supresión: detecta si el mapa quedó truncado por el LIMIT

    # Supresión con la regla corregida (1 <= n < 5): un conteo CERO no es un
    # secreto estadístico, es la ausencia de casos, y se dice como tal.
    filas, suprimidas, vacias, rechazo = pipeline.sobre_filas(
        filas, columnas_conteo, unidad_conteo(columnas_conteo))
    if rechazo is not None:
        return dict(rechazos.a_respuesta(rechazo, sql=sql_seguro),
                    veredicto="OK", celdas_suprimidas=suprimidas)

    salida = _ocultar_n_crudo(filas, columnas_conteo)
    resultado = {"ok": True, "sql": sql_seguro, "veredicto": "OK",
                 "respuesta": redactar(texto, sql_seguro, filas, suprimidas, columnas_conteo,
                                       truncado=n_geo_raw >= LIMITE_MAXIMO,
                                       interpretaciones=interpretaciones, contexto=contexto),
                 "datos": salida, "celdas_suprimidas": suprimidas}
    # Otras lecturas posibles del nombre consultado: se ofrecen junto a la respuesta.
    if alternativas:
        resultado["opciones"] = alternativas
    mapa = construir_mapa_2023(filas, columnas_conteo, suprimidas, sql_seguro)
    if mapa and mapa["datos"]:
        # Anti-truncamiento: nunca mostrar un mapa nacional recortado en silencio.
        total = _contar_unidades_geo(sql_seguro) if n_geo_raw >= LIMITE_MAXIMO else len(mapa["datos"])
        if total > LIMITE_MAXIMO:
            nivel_txt = _NIVEL_TXT.get(mapa["nivel"], "unidad")
            resultado["respuesta"] += (
                f"\n\n_Nota: el desglose por {nivel_txt} tiene {total} unidades y supera el máximo de "
                f"{LIMITE_MAXIMO} que se pueden mapear a la vez, por lo que NO se muestra el mapa (sería un "
                f"recorte parcial). Acotá la pregunta a un ámbito menor —un departamento o una sección— para verlo._")
        else:
            resultado["mapa"] = mapa
    pipeline.recordar_resultado(sql_seguro, "2023", resultado)
    return resultado


if __name__ == "__main__":
    pregunta = " ".join(sys.argv[1:]) or "¿Cuántas personas viven en Uruguay?"
    res = preguntar(pregunta, verbose=True)
    print("PREGUNTA :", pregunta)
    print("SQL      :", res.get("sql"))
    print("VEREDICTO:", res.get("veredicto"))
    print("SUPRIMIDAS:", res.get("celdas_suprimidas", 0))
    d = res.get("datos")
    if d is not None:
        print("DATOS    :", json.dumps(d[:12], ensure_ascii=False, default=str))
    print("RESPUESTA:", res.get("respuesta"))
