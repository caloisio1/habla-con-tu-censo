"""comun/sinonimos.py — Tabla de sinónimos DECLARADA y revisable.

Está separada del resolver a propósito: es la parte que un revisor del INE puede
leer y corregir sin tocar código. Cada entrada mapea una forma de uso corriente a
la forma canónica que el resolver va a buscar en el nomenclátor o en el
diccionario de variables.

Las claves se comparan NORMALIZADAS (mayúsculas, sin tildes, sin puntuación), así
que no hace falta repetir "afro", "AFRO" y "Afro".
"""
from comun.texto import normalizar

# ── conceptos y variables ────────────────────────────────────────────────
# Sinónimo de uso corriente -> TÉRMINO CANÓNICO tal como aparece en el diccionario
# del censo. El valor no es una definición: es lo que el resolver va a buscar
# dentro de las etiquetas de valor, y lo que se le declara al modelo como glosario.
CONCEPTOS = {
    "NBI": "necesidades básicas insatisfechas",
    "NECESIDADES BASICAS INSATISFECHAS": "necesidades básicas insatisfechas",
    "JEFATURA": "jefe",
    "JEFATURA DE HOGAR": "jefe",
    "JEFE DE HOGAR": "jefe",
    "JEFA DE HOGAR": "jefe",
    "JEFE DEL HOGAR": "jefe",
    "AFRO": "afro o negra",
    "NEGRA": "afro o negra",
    "NEGRO": "afro o negra",
    "AFRODESCENDIENTE": "afro o negra",
    "AFRODESCENDIENTES": "afro o negra",
    "ASCENDENCIA AFRO": "afro o negra",
    "CONCUBINATO": "unión libre",
    "UNION CONSENSUAL": "unión libre",
    "PAREJA SIN CASARSE": "unión libre",
    "AMANCEBADO": "unión libre",
    # 2023 dice "Casamiento civil" donde 1996 y 2011 dicen "Casado/a": el sinónimo
    # evita que la misma pregunta funcione en un censo y falle en otro.
    "CASADO": "casamiento",
    "CASADA": "casamiento",
    "MATRIMONIO": "casamiento",
    "SOLTERO": "nunca convivió en pareja",
    "SOLTERA": "nunca convivió en pareja",
    "DIVORCIADO": "divorciado",
    "VIUDO": "viudo",
    "LICEO": "secundaria",
    "UTU": "enseñanza técnica",
    "FACULTAD": "universidad",
    "TERCIARIA": "terciario",
    "ANALFABETISMO": "sabe leer y escribir",
    "ALFABETISMO": "sabe leer y escribir",
    "ANALFABETO": "sabe leer y escribir",
    "ASENTAMIENTO": "asentamiento irregular",
    "CANTEGRIL": "asentamiento irregular",
    "ESCOLARIDAD": "años de estudio",
    "ANOS DE ESTUDIO": "años de estudio",
    "CCZ": "centro comunal zonal",
    "CENTRO COMUNAL": "centro comunal zonal",
    "SECCION": "sección censal",
    "PROPIETARIO": "propietario",
    "INQUILINO": "arrendatario",
    "ALQUILA": "arrendatario",
    "ALQUILER": "arrendatario",
}

# ── geografía: formas corrientes -> nombre del nomenclátor ───────────────
# Ojo: acá NO se resuelven homónimos ni colisiones departamento/ciudad. Eso lo
# decide el resolver pidiendo aclaración; esta tabla solo traduce apodos.
GEOGRAFICOS = {
    "MVD": "MONTEVIDEO",
    "LA CAPITAL": "MONTEVIDEO",
    "CIUDAD DE LA COSTA": "CIUDAD DE LA COSTA",
    "PUNTA DEL ESTE": "PUNTA DEL ESTE",
    "FRAY BENTOS": "FRAY BENTOS",
    "TREINTA Y TRES": "TREINTA Y TRES",
    "33": "TREINTA Y TRES",
}

# ── municipios de Montevideo: la letra -> el nombre que guarda la base ───
# Los ocho municipios de Montevideo no tienen nombre propio: se llaman por una
# letra, y así los nombra todo el mundo ("el municipio B", "vivo en el CH"). La
# columna MUNICIPIO_136, en cambio, guarda 'MUNICIPIO B'. Sin esta traducción el
# filtro sale como MUNICIPIO_136 = 'B', no matchea NADA y la consulta vuelve
# vacía sin explicar por qué, que es exactamente el fallo silencioso que el
# resolver existe para evitar.
#
# Solo se aplica cuando el SQL ya fijó que se está hablando de un municipio (la
# comparación es contra MUNICIPIO_136); una letra suelta no significa nada en
# ningún otro contexto, así que no hay riesgo de que se aplique de más.
LETRAS_MUNICIPIO_MVD = ("A", "B", "C", "CH", "D", "E", "F", "G")

MUNICIPIOS_MVD = {letra: "MUNICIPIO " + letra for letra in LETRAS_MUNICIPIO_MVD}
MUNICIPIOS_MVD.update({"MUN " + l: "MUNICIPIO " + l for l in LETRAS_MUNICIPIO_MVD})
MUNICIPIOS_MVD.update({"MPIO " + l: "MUNICIPIO " + l for l in LETRAS_MUNICIPIO_MVD})

# ── nombres que NO se desambiguan ────────────────────────────────────────
# Casi todos los departamentos comparten el nombre con su capital, y esa colisión
# es REAL: quien dice "Canelones" puede estar hablando del departamento (608.960
# personas) o de la ciudad (24.159), y son cosas distintas también en el habla.
# Montevideo no: la ciudad y el departamento son lo mismo para cualquiera que
# hable, y preguntar cuál se quiso decir es ruido. Se lee SIEMPRE como
# departamento, que es la cifra que el INE publica como Montevideo.
#
# La consecuencia, asumida (Carlos, 15-ago-2026): entre las dos lecturas hay
# 21.398 personas de diferencia -las de Rural Montevideo, localidad 01900- y esa
# diferencia deja de mencionarse. Es el 1,6 % del departamento. Quien la necesite
# la puede pedir, porque preguntar por la localidad sigue funcionando.
LECTURA_UNICA = {"MONTEVIDEO": "departamento"}

# Capitales departamentales: permiten resolver "la capital de Flores" -> Trinidad,
# que es el caso que NO es homonimia sino sinónimo (la ciudad y el departamento se
# llaman distinto).
CAPITALES = {
    "ARTIGAS": "ARTIGAS", "CANELONES": "CANELONES", "CERRO LARGO": "MELO",
    "COLONIA": "COLONIA DEL SACRAMENTO", "DURAZNO": "DURAZNO",
    "FLORES": "TRINIDAD", "FLORIDA": "FLORIDA", "LAVALLEJA": "MINAS",
    "MALDONADO": "MALDONADO", "MONTEVIDEO": "MONTEVIDEO", "PAYSANDU": "PAYSANDU",
    "RIO NEGRO": "FRAY BENTOS", "RIVERA": "RIVERA", "ROCHA": "ROCHA",
    "SALTO": "SALTO", "SAN JOSE": "SAN JOSE DE MAYO", "SORIANO": "MERCEDES",
    "TACUAREMBO": "TACUAREMBO", "TREINTA Y TRES": "TREINTA Y TRES",
}

_CONCEPTOS_N = {normalizar(k): v for k, v in CONCEPTOS.items()}
_GEO_N = {normalizar(k): v for k, v in GEOGRAFICOS.items()}
_CAPITALES_N = {normalizar(k): v for k, v in CAPITALES.items()}
_MUNICIPIOS_N = {normalizar(k): v for k, v in MUNICIPIOS_MVD.items()}
_LECTURA_UNICA_N = {normalizar(k): v for k, v in LECTURA_UNICA.items()}


def concepto(texto):
    """Sinónimo de concepto o variable, o None."""
    return _CONCEPTOS_N.get(normalizar(texto))


def geografico(texto):
    """Apodo geográfico -> nombre del nomenclátor, o None."""
    return _GEO_N.get(normalizar(texto))


def capital_de(departamento):
    """Capital de un departamento, o None."""
    return _CAPITALES_N.get(normalizar(departamento))


def municipio(texto):
    """Letra de un municipio de Montevideo -> nombre en la base, o None."""
    return _MUNICIPIOS_N.get(normalizar(texto))


def lectura_unica(texto):
    """Tipo con el que se lee siempre ese nombre, o None si hay que desambiguar."""
    return _LECTURA_UNICA_N.get(normalizar(texto))


def todos():
    """Vista completa de la tabla, para auditarla o mostrarla en un informe."""
    return {"conceptos": dict(CONCEPTOS), "geograficos": dict(GEOGRAFICOS),
            "capitales": dict(CAPITALES), "municipios": dict(MUNICIPIOS_MVD)}


def glosario_para_prompt():
    """Glosario declarado que se inyecta en el prompt de SQL de los cuatro motores.

    Que el modelo lea la MISMA tabla que usa el resolver evita que cada censo
    interprete "NBI" o "jefatura" a su manera.
    """
    pares = sorted({(k, v) for k, v in CONCEPTOS.items() if k.upper() != v.upper()},
                   key=lambda p: p[0])
    return "; ".join("%s = %s" % (k.lower(), v) for k, v in pares)
