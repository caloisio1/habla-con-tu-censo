"""generar_diccionarios.py — Arma el diccionario consultable de cada censo.

Las fuentes ya existen y son las MISMAS que ve el modelo al generar el SQL, así
que el diccionario que se publica y el que usa el motor no pueden divergir:

  1996 / 2004 / 2023 -> esquema_llm_<censo>.txt   (líneas '- nombre | etiqueta | códigos')
  2011               -> datos/diccionario.json    (145 variables con value_labels del .sav)

Salida: app/static/dicc/dicc_<censo>.json, que el frontend carga a demanda.

Correr desde la raíz del repo:  python3 datos/generar_diccionarios.py
"""

import json
import os
import re
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
SALIDA = os.path.join(RAIZ, "app", "static", "dicc")

# Códigos que son PERDIDOS (no una categoría): se marcan aparte para que quien
# consulta sepa que van excluidos de conteos y denominadores.
#
# La clasificación es la MISMA función que usa el motor para armar el prompt
# (app/dicc.py), no una copia: si el panel dijera "categoría válida" donde el
# motor excluye, la documentación estaría describiendo un censo que no es el que
# se consulta. Antes había acá un regex propio que se salteaba 'No relevado' e
# 'Ignorado' (el \b no cierra contra la 'o' final) y marcaba de más por el \bSE\b
# de 'se la prestaron': 107 códigos de 2011 discrepaban del motor.
sys.path.insert(0, RAIZ)
from app.dicc import es_label_perdido  # noqa: E402


def _codigos(texto):
    """'1=Sí; 2=No; 9:NR' -> [{codigo, etiqueta, perdido}]."""
    out = []
    for parte in re.split(r"[;|]", texto or ""):
        parte = parte.strip()
        if not parte:
            continue
        m = re.match(r"^([^=:]{1,12})\s*[=:]\s*(.+)$", parte)
        if not m:
            continue
        cod, etq = m.group(1).strip(), m.group(2).strip()
        out.append({"codigo": cod, "etiqueta": etq, "perdido": es_label_perdido(etq)})
    return out


# El desplegable es un diccionario para LEER, no un esquema para programar: ahí la
# tabla se llama «Personas», no `personas_2023`. El nombre técnico se conserva en el
# campo `tabla` (el frontend lo pone como tooltip) porque es el que aparece en el SQL
# de las respuestas. El sufijo del año además sobra: cada censo vive en su propia base.
TITULOS_TABLA = {
    "censo2004": "Personas, hogares y viviendas",   # una sola tabla, con banderas per/hog/viv
    "columnas derivadas": "Columnas derivadas",
    "nomenclátor": "Nomenclátor",
}


# Advertencias que el esquema del modelo lleva pegadas a la etiqueta —'(atención:
# …)'—. El modelo las necesita ahí, en la misma línea; la página no: en el título
# tapan el nombre de la variable. Se separan y se muestran al abrir la variable.
RE_AVISO = re.compile(r"\s*\((atención|ojo)\s*:\s*(.+)\)\s*$", re.I)


def _separar_aviso(etiqueta):
    m = RE_AVISO.search(etiqueta or "")
    if not m:
        return etiqueta, ""
    aviso = m.group(2).strip()
    return etiqueta[:m.start()].strip(), "Atención: " + aviso[:1].upper() + aviso[1:]


def titulo_tabla(nombre):
    if nombre in TITULOS_TABLA:
        return TITULOS_TABLA[nombre]
    base = re.sub(r"_(19|20)\d{2}$", "", nombre)   # personas_2023 -> personas
    return base[:1].upper() + base[1:]


def desde_esquema(censo):
    """Lee esquema_llm_<censo>.txt y devuelve (notas, tablas)."""
    ruta = os.path.join(RAIZ, "esquema_llm_%s.txt" % censo)
    lineas = open(ruta, encoding="utf-8").read().splitlines()
    notas, tablas, actual = [], [], None
    for ln in lineas:
        s = ln.strip()
        if not s:
            continue
        m = re.match(r"^(TABLA|NOMENCL[ÁA]TOR)\s*:?\s*(.*)$", s)
        if m:
            nombre = (m.group(2) or "nomenclátor").split("(")[0].strip().rstrip(":") or "nomenclátor"
            actual = {"tabla": nombre, "titulo": titulo_tabla(nombre),
                      "descripcion": s, "variables": []}
            tablas.append(actual)
            continue
        if s.startswith("- ") and actual is not None:
            partes = [p.strip() for p in s[2:].split("|")]
            nombre = partes[0]
            # Bajo NOMENCLÁTOR las líneas son 'tabla(col1, col2, ...)': son tablas
            # de referencia, no variables consultables. Se listan aparte.
            m2 = re.match(r"^([a-z_0-9]+)\s*\((.+)\)$", nombre, re.I)
            if m2 and actual["tabla"] == "nomenclátor":
                actual.setdefault("tablas_ref", []).append(
                    {"nombre": m2.group(1), "columnas": [c.strip() for c in m2.group(2).split(",")]})
                continue
            etiqueta = partes[1] if len(partes) > 1 else ""
            codigos = _codigos(partes[2] if len(partes) > 2 else "")
            # Una advertencia sobre la variable NO es parte de su nombre: en el
            # título queda un cartel ilegible. Va adentro, al desplegar.
            etiqueta, aviso = _separar_aviso(etiqueta)
            var = {"nombre": nombre, "etiqueta": etiqueta, "codigos": codigos}
            if aviso:
                var["descripcion"] = aviso
            actual["variables"].append(var)
        elif actual is None:
            notas.append(s)
    return notas, tablas


# Cifras de población de 2011, las tres distintas y las tres necesarias. La que
# encabeza la ficha es la PUBLICADA por el INE: es la única que quien consulta
# puede cotejar contra un documento oficial. Las otras dos explican por qué un
# COUNT(*) sobre la base no da exactamente esa.
POB_2011_INE = "3.286.314"    # contabilizada (censada + imputadas por moradores ausentes)
POB_2011_CENSADA = "3.252.091"
POB_2011_IMPUTADAS = "34.223"
POB_2011_SAV = "3.285.877"    # filas del archivo público de microdatos
POB_2011_BASE = "3.285.877"   # cargadas en datos/censo.db: desde el 28-jul-2026 son
                              # TODAS las del archivo (ya no se descarta ninguna fila)


def desde_diccionario_2011():
    d = json.load(open(os.path.join(AQUI, "diccionario.json"), encoding="utf-8"))
    variables = []
    for v in d["variables"]:
        codigos = [{"codigo": k, "etiqueta": e, "perdido": es_label_perdido(e)}
                   for k, e in (v.get("value_labels") or {}).items()]
        variables.append({"nombre": v["nombre"], "etiqueta": v.get("etiqueta") or "",
                          "tipo": v.get("tipo", ""), "codigos": codigos})
    # (columna, nombre corto, descripción). El nombre corto es el TÍTULO en el
    # panel, igual que en las 145 crudas; la descripción se despliega abajo. Antes
    # la descripción entera hacía de título y, al pasar por limpiar(), los nombres
    # de columna del INE terminaban en minúscula ('derivada de dpto').
    derivadas = [
        ("departamento", "Departamento",
         "Nombre del departamento, en MAYÚSCULAS y sin tilde (derivada de DPTO)."),
        ("sexo", "Sexo", "'Hombres' | 'Mujeres' (derivada de PERPH02)."),
        ("edad", "Edad", "Años cumplidos, de 0 a 111 (derivada de PERNA01)."),
        ("asc_afro", "Mención de ascendencia afro o negra",
         "'Si' | 'No' | NULL (derivada de PERER01_1). Es la MENCIÓN: quien se declara "
         "afrodescendiente aunque también declare otra ascendencia. Para contar "
         "afrodescendientes se usa esta variable, no asc_principal."),
        ("asc_principal", "Ascendencia principal",
         "'Afro o Negra', 'Asiática o Amarilla', 'Blanca', 'Indígena', 'Otra', "
         "'Ninguna' o NULL (derivada de PERER02). La responden todas las personas que "
         "declararon al menos una ascendencia; NULL son las que no declararon ninguna. "
         "'Ninguna' significa que declaró varias y no eligió una principal. Es DISTINTA "
         "de la mención: hay menos personas con ascendencia principal afro que con "
         "mención de ascendencia afro."),
        ("nbi", "Cantidad de NBI del hogar",
         "De 0 a 3, TOPEADA: el 3 significa '3 o más', no existe el 4. Es del hogar y "
         "se repite en cada integrante (derivada de NBI_CANTIDAD)."),
        ("hogar_key", "Identificador de hogar",
         "ID_VIVIENDA || '-' || HOGID. Clave interna del proyecto: para contar hogares, "
         "COUNT(DISTINCT hogar_key)."),
        ("vivienda_key", "Identificador de vivienda",
         "= ID_VIVIENDA. Clave interna del proyecto: para contar viviendas, "
         "COUNT(DISTINCT vivienda_key)."),
        ("codsec", "Código de sección censal", "departamento*100 + SECC."),
        ("codloc", "Código de localidad",
         "departamento*1000 + LOC. Es la clave del JOIN con la tabla localidades."),
    ]
    tablas = [
        {"tabla": "personas", "titulo": titulo_tabla("personas"),
         "descripcion": "Una fila por persona: %s registros cargados." % POB_2011_BASE,
         "variables": variables},
        # sin_normalizar: estas etiquetas ya están escritas con el criterio final y
        # traen nombres de columna del INE, que no hay que pasar por sentencia().
        {"tabla": "columnas derivadas", "titulo": titulo_tabla("columnas derivadas"),
         "descripcion": "Agregadas por el proyecto sobre las variables crudas del INE.",
         "sin_normalizar": True,
         "variables": [{"nombre": n, "etiqueta": e, "descripcion": desc, "codigos": []}
                       for n, e, desc in derivadas]},
    ]
    notas = ["Fuente: %s" % d.get("fuente", ""),
             "%s registros en el archivo del INE, %s cargados; %s variables crudas del "
             "INE + 10 derivadas." % (POB_2011_SAV, POB_2011_BASE, d["n_variables"])]
    return notas, tablas


# Ficha de cada censo. Las cifras salen de los encabezados de los esquemas, que
# son los mismos que respetan los motores al contar.
DOC = {
    "1996": {
        "titulo": "Censo 1996",
        "nombre_oficial": "VII Censo General de Población, III de Hogares y V de Viviendas",
        "ponderacion": "Censo completo, sin ponderación: todas las cifras son conteos exactos.",
        "cifras": [("Personas", "3.163.763"), ("Hogares", "975.056"),
                   ("Viviendas", "1.126.502 (182.708 desocupadas)"),
                   ("Viviendas ocupadas", "943.794")],
        "unidades": "Personas: COUNT(*) sobre personas_1996. Hogares: COUNT(DISTINCT hogar_key). "
                    "Viviendas: tabla viviendas_1996.",
        "mapas": "Departamento, sección censal y barrio de Montevideo.",
        "avisos": [
            "No se pueden unir personas_1996 y viviendas_1996: son universos distintos. Las variables "
            "de vivienda ya están copiadas en la tabla de personas.",
            "El código de barrio se guarda sin cero a la izquierda ('1') y el nomenclátor con cero "
            "('01'): el join va con CAST a entero en las dos patas.",
            "edad = 99 es el tope de la variable, no un código de no respuesta.",
        ],
    },
    "2004": {
        "titulo": "Censo 2004 (Fase 1)",
        "nombre_oficial": "Conteo de Población y Viviendas",
        "ponderacion": "Censo de conteo, sin ponderación: todas las cifras son conteos exactos.",
        "cifras": [("Personas", "3.241.003"), ("Hogares", "1.065.677"), ("Viviendas", "1.279.741")],
        "unidades": "Una sola tabla (censo2004) con banderas de registro: personas WHERE per=1, "
                    "hogares WHERE hog=1, viviendas WHERE viv=1.",
        "mapas": "Departamento, sección censal y barrio de Montevideo.",
        "avisos": [
            "Es un CONTEO: releva solo geografía, vivienda, hogar, sexo y edad. No tiene educación, "
            "ocupación, migración ni ascendencia.",
            "El 0 significa NO APLICA, no una categoría.",
            "Sin filtrar la bandera correspondiente la tabla repite los datos de la vivienda en cada "
            "persona y las cifras salen infladas.",
        ],
    },
    "2011": {
        "titulo": "Censo 2011",
        "nombre_oficial": "Censo de Población, Hogares y Viviendas",
        "ponderacion": "Conteos exactos de los microdatos (sin ponderación).",
        "cifras": [("Población contabilizada (INE)", POB_2011_INE),
                   ("Población censada", POB_2011_CENSADA),
                   ("Imputadas por moradores ausentes", POB_2011_IMPUTADAS),
                   ("Registros en la base", POB_2011_BASE),
                   ("Hogares", "1.166.270"), ("Viviendas", "1.136.432"),
                   ("Variables", "145 del INE + 10 derivadas")],
        "unidades": "Personas: COUNT(*). Hogares: COUNT(DISTINCT hogar_key). "
                    "Viviendas: COUNT(DISTINCT vivienda_key).",
        "mapas": "Departamento, sección censal, barrio de Montevideo y CCZ.",
        "avisos": [
            "La cifra oficial del censo es la población CONTABILIZADA: %s personas (los %s "
            "censados más %s imputados en viviendas con moradores ausentes). La base cargada "
            "tiene %s registros, que son TODAS las filas del archivo público de microdatos, "
            "el 99,987%% de esa cifra: el archivo publicado por el INE ya trae 437 registros "
            "menos que el total contabilizado (ver datos/NOTAS_CALIDAD.md). Las respuestas "
            "son conteos sobre los microdatos, no los tabulados oficiales."
            % (POB_2011_INE, POB_2011_CENSADA, POB_2011_IMPUTADAS, POB_2011_BASE),
            "53 personas, en 19 hogares, tienen el cuestionario protegido por SECRETO "
            "ESTADÍSTICO: de ellas solo constan departamento, sexo y el hogar al que "
            "pertenecen. Suman a la población total pero quedan fuera de cualquier otro "
            "corte, igual que cualquier dato faltante.",
            "Las variables de hogar y de vivienda se repiten en cada integrante: para contar hogares "
            "o viviendas es obligatorio COUNT(DISTINCT ...).",
            "Para contar afrodescendientes va asc_afro, que es la MENCIÓN de ascendencia afro. "
            "asc_principal es otra variable: la responden todas las personas que declararon al "
            "menos una ascendencia, y da menos afrodescendientes porque solo cuenta a quienes "
            "eligieron esa como la principal.",
        ],
    },
    "2023": {
        "titulo": "Censo 2023",
        "nombre_oficial": "Censo de Población, Hogares y Viviendas (versión ponderada)",
        "ponderacion": "Censo PONDERADO: las cifras de personas son estimaciones publicadas por el "
                       "INE, calculadas con el ponderador W. Hogares y viviendas son conteos exactos.",
        "cifras": [("Personas", "3.499.451 (estimación ponderada)"), ("Hogares", "1.255.062"),
                   ("Viviendas", "1.659.044"), ("Registros censados", "3.151.118")],
        "unidades": "Personas: SUM(W), nunca COUNT(*). Hogares: COUNT(DISTINCT hogar_key). "
                    "Viviendas: COUNT(*) sobre viviendas_2023.",
        "mapas": "Departamento, sección, localidad, segmento y barrio de Montevideo.",
        "avisos": [
            "No se pueden unir personas_2023 y viviendas_2023: universos distintos (el INE lo "
            "desaconseja).",
        ],
    },
}

# Nombre formal de las variables que el INE publica SIN etiqueta. Sin esto el
# diccionario muestra el nombre técnico de la columna, que no le dice nada a
# quien consulta. Se escriben a mano y solo cuando el significado es seguro.
ETIQUETAS_MANUALES = {
    "1996": {
        "tipohogar": "Tipo de hogar",
        "vivnbi": "NBI de la vivienda",
        "tipoacti": "Tipo de actividad",
        # Texto tomado del formulario original (Formulario_Censo_1996/Pag 4, pregunta 13).
        "finalizo": "13G-Con respecto a ese nivel, ¿actualmente Ud...? "
                    "1=Asiste a un Establecimiento Público; 2=Asiste a un Establecimiento Privado; "
                    "3=Abandonó los estudios; 4=Finalizó el nivel",
        "tipo85": "Tipo de hogar (clasificación comparable con el Censo 1985)",
        # Indicadores de Necesidades Básicas Insatisfechas: son BINARIOS (0/1),
        # marcan la carencia; la cantidad va aparte en cant_nbi.
        "hacinbi": "NBI de hacinamiento",
        "agponbi": "NBI de agua potable",
        "evacnbi": "NBI de evacuación de excretas",
        "enernbi": "NBI de energía eléctrica",
        "totnbi": "Hogar con al menos una NBI",
        "cant_nbi": "Cantidad de NBI del hogar",
        "hogar_key": "Identificador de hogar (clave interna del proyecto)",
        "vivienda_key": "Identificador de vivienda (clave interna del proyecto)",
    },
    "2004": {
        # No es una variable del censo: marca de qué archivo del INE salió la fila.
        "fuente": "Archivo de origen del registro (control de carga: csv o dbf)",
    },
    "2011": {
        "ID_VIVIENDA": "Identificador de vivienda (clave del INE)",
        "CCZ": "Centro Comunal Zonal (solo Montevideo)",
    },
    "2023": {
        "MADRE_PERID": "Madre de la persona con ID",
        "VIVVO03": "Condición de ocupación de la vivienda",
        "HOGCE17_1": "Cantidad de equipos de aire acondicionado en el hogar",
        "HOGMA01_1_1": "Cantidad de perros en el hogar",
        "HOGMA01_2_1": "Cantidad de gatos en el hogar",
        "PERMI05_1": "Años que hace que reside en esta vivienda",
        "PERFM01_2": "Hijos nacidos vivos — total mujeres",
        "PERFM01_3": "Hijos nacidos vivos — total varones",
        "I_PERAL06_TIPO": "Método de clasificación de la variable imputada",
        "AGREGADO_A_HOGAR_CENSADO": "Persona agregada a un hogar ya censado",
    },
}


# --- Presentación de los nombres --------------------------------------------
# El modelo es el diccionario de 2011: nombre corto, en oración ("Condición de
# ocupación de la vivienda"). 2023 trae lo mismo pero EN MAYÚSCULAS y 1996 trae
# la pregunta entera del formulario con su número ("12G-De los siguientes
# niveles..."). Se normalizan las dos, y la pregunta completa se conserva como
# descripción: se muestra al desplegar la variable, no en el título.

SIGLAS = {"NBI", "CCZ", "UTU", "UTE", "OSE", "MSP", "BPS", "ID", "INE", "TV", "PC", "DGI",
          "ANEP", "UDELAR", "ASSE", "MEC", "MTOP", "MIDES", "BROU", "BSE", "AFAM", "W.C.",
          "CIUO", "CIIU", "COTA", "CNUO", "IG", "NC", "NR", "SD", "SE", "II", "III", "IV",
          "XO", "LOC", "MVOTMA", "UTEC", "WC", "W.C."}

# Nombres propios: al pasar de MAYÚSCULAS a oración no pueden quedar en minúscula.
PROPIOS = {"MONTEVIDEO", "ARTIGAS", "CANELONES", "COLONIA", "DURAZNO", "FLORES", "FLORIDA",
           "LAVALLEJA", "MALDONADO", "PAYSANDÚ", "RIVERA", "ROCHA", "SALTO", "SORIANO",
           "TACUAREMBÓ", "URUGUAY", "CERRO", "LARGO", "RÍO", "NEGRO", "SAN", "JOSÉ",
           "TREINTA", "TRES"}


def _cap(s):
    """Mayúscula en la primera LETRA, no en el primer carácter."""
    for i, c in enumerate(s):
        if c.isalpha():
            return s[:i] + c.upper() + s[i + 1:]
    return s


def sentencia(txt):
    """MAYÚSCULAS -> oración, respetando siglas y nombres propios.

    Va PALABRA POR PALABRA, no sobre la etiqueta entera: hay etiquetas mixtas
    como 'MASCOTAS EN EL HOGAR (tiene mascotas)' que con la regla de "toda la
    cadena en mayúsculas" se escapaban. Las de 1 a 3 letras se dejan como están
    (OSE, UTU, TV, NBI): son siglas aunque no estén en la lista.
    """
    if not txt:
        return txt
    palabras = []
    for w in txt.split(" "):
        limpio = w.strip("()¿?¡!,.;:").upper()
        # Lo que trae dígitos o un '=' no es una palabra sino un CÓDIGO: el nombre
        # de una variable o una condición ('PERMI01=3', 'PERAL09'). Pasarlo a
        # minúscula lo vuelve incitable: nadie encuentra 'permi01' en la base.
        if any(c.isdigit() for c in w) or "=" in w:
            palabras.append(w)
            continue
        # Solo se respeta lo que está en la lista de siglas. La regla de "3
        # letras o menos es sigla" dejaba EN, EL, DE, USO, VER en mayúscula.
        if not w.isupper() or limpio in SIGLAS:
            palabras.append(w if limpio not in PROPIOS else _cap(w.lower()))
        elif limpio in PROPIOS:
            palabras.append(_cap(w.lower()))   # ojo: '(montevideo)' empieza con paréntesis
        else:
            palabras.append(w.lower())
    return _cap(" ".join(palabras))


def limpiar(txt):
    """Saca el número de pregunta del formulario y la coletilla del universo."""
    s = re.sub(r"^\d+[A-Za-z]?\s*-\s*", "", (txt or "").strip())
    s = re.split(r"\s*Universo\s*:", s)[0]
    s = re.split(r"\s*Se completa s[oó]lo", s)[0]
    # El código de bloque también aparece EN MEDIO: 'Tipo de vivienda (B6 - Indique...)'.
    s = re.sub(r"\s*\(\s*[A-Z]\s?\d+\s*-[^)]*\)", "", s)
    return sentencia(s.strip().rstrip("."))


# Nombre corto de las variables de 1996, que en el esquema vienen como la
# pregunta textual del formulario. Se escriben con el criterio de 2011.
NOMBRE_CORTO = {"1996": {
    "dpto": "Departamento", "secc": "Sección censal", "segm": "Segmento censal",
    "loc": "Localidad", "vivienda": "Número de vivienda", "tipviv": "Tipo de vivienda",
    "barrio": "Barrio (Montevideo)", "ccz": "Centro Comunal Zonal (Montevideo)",
    "hogarviv": "Número de hogar en la vivienda", "sexo": "Sexo", "edad": "Edad",
    "jubopen": "Jubilado o pensionista", "saludtot": "Cobertura de salud",
    "saludpar": "Cobertura de salud parcial", "habitaqui": "Reside en esta localidad",
    "habdepc": "Departamento o país de residencia", "hablocc": "Localidad de residencia",
    "menor5": "Residencia hace 5 años (menores de 5)",
    "hace5aqui": "Residía en esta localidad hace 5 años",
    "hac5depc": "Departamento o país de residencia hace 5 años",
    "hac5loc": "Localidad de residencia hace 5 años", "nacioaqui": "Nació en esta localidad",
    "nacdepc": "Departamento o país de nacimiento", "naclocc": "Localidad de nacimiento",
    "llegada": "Año de llegada al Uruguay", "leeryesc": "Sabe leer y escribir",
    "nivel": "Nivel educativo más alto al que asistió",
    "finalizo": "Situación en ese nivel educativo", "ultimo": "Años aprobados en ese nivel",
    "carrerc": "Orientación o carrera cursada", "tecnicos": "Cursa o cursó estudios técnicos",
    "niveltec": "Situación en el curso técnico", "fintec": "Años aprobados en estudios técnicos",
    "tecnsc": "Estudio técnico o comercial cursado", "estado": "Estado conyugal",
    "trabajo": "Trabajó la semana pasada", "sinpago": "Trabajo sin pago regular",
    "licencia": "De licencia, con trabajo", "buscotrab": "Buscó trabajo en las últimas 4 semanas",
    "algunavez": "Trabajó alguna vez", "cota70": "Ocupación (COTA-70)",
    "ciuo88": "Ocupación (CIUO-88)", "ciur2": "Rama de actividad (CIIU rev. 2)",
    "ciur3": "Rama de actividad (CIIU rev. 3)", "categoria": "Categoría en la ocupación",
    "hijosvivos": "Hijos nacidos vivos", "vivosactua": "Hijos vivos actualmente",
    "hijos12mes": "Hijos nacidos en los últimos 12 meses", "higienico": "Servicio higiénico",
    "usoservhig": "Uso del servicio higiénico", "evacserhig": "Evacuación del servicio higiénico",
    "lugarcocin": "Lugar para cocinar", "fuentcoci": "Fuente de energía para cocinar",
    "calefaccio": "Calefacción", "fuentcale": "Fuente de energía para calefaccionar",
    "tenencia": "Tenencia de la vivienda", "habdormir": "Habitaciones para dormir",
    "vehiculo": "Vehículo propio del hogar", "calefon": "Calefón o termofón",
    "calentador": "Calentador instantáneo", "refrisimpl": "Refrigerador simple",
    "refriconfr": "Refrigerador con freezer", "freezer": "Freezer", "tvcolor": "TV color",
    "tvbn": "TV blanco y negro", "telefono": "Teléfono", "microonda": "Horno microondas",
    "video": "Video casetero", "lavacomun": "Lavarropa común", "lavaprog": "Lavarropa programable",
    "pc": "Computadora", "totalper": "Personas alojadas anoche",
    "tothombres": "Hombres alojados anoche", "totmujeres": "Mujeres alojadas anoche",
    "aestudio": "Años de estudio", "condocup": "Condición de ocupación de la vivienda",
    "paredes": "Material de las paredes", "techos": "Material de los techos",
    "pisos": "Material de los pisos", "origenagua": "Origen del agua",
    "llegadaagu": "Llegada del agua a la vivienda", "alute": "Alumbrado eléctrico de UTE",
    "alcargador": "Alumbrado por cargador", "algrupo": "Alumbrado por grupo electrógeno",
    "alotro": "Otro tipo de alumbrado", "habresi": "Habitaciones residenciales",
    "habnoresi": "Habitaciones no residenciales", "nhogar": "Hogares en la vivienda",
    "tipoviv": "Categoría de la vivienda",
}}


COMUN = [
    "Solo se publican resultados agregados: nunca registros individuales.",
    "Las celdas con menos de 5 casos se suprimen, en tablas y en mapas. (En el censo "
    "ponderado, la supresión de celdas chicas se evalúa siempre sobre el conteo CRUDO de "
    "registros, nunca sobre la cifra ponderada.)",
    "Los códigos marcados como PERDIDOS (NC, NR, IG, no relevado…) se excluyen siempre de "
    "conteos, totales y denominadores.",
]


def main():
    os.makedirs(SALIDA, exist_ok=True)
    for censo in ("1996", "2004", "2011", "2023"):
        if censo == "2011":
            notas, tablas = desde_diccionario_2011()
        else:
            notas, tablas = desde_esquema(censo)
        manual = ETIQUETAS_MANUALES.get(censo, {})
        cortos = NOMBRE_CORTO.get(censo, {})
        for tb in tablas:
            if tb.pop("sin_normalizar", False):
                continue
            for v in tb["variables"]:
                if not (v.get("etiqueta") or "").strip() and v["nombre"] in manual:
                    v["etiqueta"] = manual[v["nombre"]]
                completo = limpiar(v.get("etiqueta") or "")
                corto = cortos.get(v["nombre"]) or completo
                v["etiqueta"] = corto
                # la pregunta textual del formulario se conserva como descripción
                if completo and completo != corto:
                    v["descripcion"] = completo
        doc = dict(DOC[censo])
        doc["comun"] = COMUN
        doc["notas_esquema"] = notas
        tablas = [tb for tb in tablas if tb["variables"] or tb.get("tablas_ref")]
        n = sum(len(t["variables"]) for t in tablas)
        data = {"censo": censo, "doc": doc, "tablas": tablas, "n_variables": n}
        ruta = os.path.join(SALIDA, "dicc_%s.json" % censo)
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        print("%s: %d variables en %d tabla(s) -> %s (%.0f KB)"
              % (censo, n, len(tablas), os.path.relpath(ruta, RAIZ), os.path.getsize(ruta) / 1024))


if __name__ == "__main__":
    main()
