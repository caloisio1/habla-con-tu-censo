"""comun/texto.py — Normalización de texto compartida por los cuatro motores.

Todo lo que hay acá es determinista y no depende del censo: la ortografía del
español es la misma en 1996 que en 2023. Lo que cambia de un censo a otro es el
nomenclátor contra el que se compara, y eso vive en comun/nomenclator.py.

Tres capas, de la más conservadora a la más agresiva:

  normalizar()   mayúsculas, sin tildes, sin puntuación, abreviaturas expandidas.
                 Es la clave de comparación principal.
  fonetico()     además colapsa las confusiones ortográficas del español
                 (s/z/c, b/v, ll/y, h muda, qu/k/c, letras repetidas). Sirve para
                 que "Chamiso" encuentre "Chamizo", pero agrupa más de la cuenta:
                 se usa como segunda pasada, nunca como clave única.
  variantes_numero()  cifra <-> palabra en los dos sentidos ("25 de Agosto" y
                 "veinticinco de agosto" son el mismo lugar, y cada censo lo
                 escribe distinto).
"""
import re
import unicodedata

# Abreviaturas del nomenclátor del INE y las que escribe la gente. Se expanden en
# las dos direcciones: el texto del usuario y el nombre almacenado pasan por acá,
# así que basta con llevar ambos a la forma larga.
ABREVIATURAS = {
    "CNEL": "CORONEL", "CNL": "CORONEL", "GRAL": "GENERAL", "GRL": "GENERAL",
    "DR": "DOCTOR", "DRA": "DOCTORA", "STA": "SANTA", "STO": "SANTO",
    "SN": "SAN", "PTO": "PUERTO", "PQUE": "PARQUE", "PJE": "PARAJE",
    "CAP": "CAPITAN", "CNO": "CAMINO", "ING": "INGENIERO", "ARQ": "ARQUITECTO",
    "PDTE": "PRESIDENTE", "PTE": "PRESIDENTE", "SGTO": "SARGENTO",
    "MTRO": "MAESTRO", "MRO": "MAESTRO", "PROF": "PROFESOR", "GRLA": "GENERALA",
    "RUR": "RURAL", "KM": "KILOMETRO", "KMS": "KILOMETRO", "RTA": "RUTA",
    "FRACC": "FRACCIONAMIENTO", "COL": "COLONIA", "BO": "BARRIO", "BRIO": "BARRIO",
    "VLA": "VILLA", "PZA": "PLAZA", "AV": "AVENIDA", "AVDA": "AVENIDA",
}

_RX_NO_PALABRA = re.compile(r"[^\w\s]", re.UNICODE)
_RX_ESPACIOS = re.compile(r"\s+")


def sin_tildes(texto, forma="NFD"):
    """Quita diacríticos conservando la ñ (que en español no es un acento).

    forma='NFKD' pliega ADEMÁS los caracteres de compatibilidad: ancho completo
    ('ｍｏｎｔｅｖｉｄｅｏ'), ligaduras, superíndices, números encerrados. Eso sirve para
    lo que el usuario tipea o pega, NO para decidir si dos entradas del catálogo
    son el mismo nombre: NFKD equipara cosas que como identidad no queremos
    fusionar. Por eso la clave principal se queda en NFD y la variante solo
    alimenta el índice de respaldo (ver normalizar_compat).
    """
    texto = str(texto).replace("ñ", "\x00").replace("Ñ", "\x01")
    texto = "".join(c for c in unicodedata.normalize(forma, texto)
                    if unicodedata.category(c) != "Mn")
    return texto.replace("\x00", "ñ").replace("\x01", "Ñ")


def _clave(texto, forma):
    t = sin_tildes(texto, forma).upper()
    t = _RX_NO_PALABRA.sub(" ", t)
    t = _RX_ESPACIOS.sub(" ", t).strip()
    return " ".join(ABREVIATURAS.get(p, p) for p in t.split())


def normalizar(texto):
    """Clave de comparación principal: MAYÚSCULAS, sin tildes, sin puntuación,
    espacios colapsados y abreviaturas expandidas.

    Es la clave de IDENTIDAD del nomenclátor: define cuándo dos nombres del
    catálogo son el mismo. Conservadora a propósito."""
    if texto is None:
        return ""
    return _clave(texto, "NFD")


def normalizar_compat(texto):
    """Clave TOLERANTE, para lo que escribe el usuario: igual que normalizar()
    pero plegando también los caracteres de compatibilidad Unicode.

    NO reemplaza a normalizar(): se usa como segundo intento cuando el primero
    no encontró nada. Una equivalencia de compatibilidad puede ayudar a ENCONTRAR
    una entidad, pero no debe FUSIONAR dos que el catálogo distingue; por eso el
    índice que alimenta guarda listas y la resolución solo procede si queda un
    único candidato."""
    if texto is None:
        return ""
    return _clave(texto, "NFKD")


# ── normalización fonética del español ────────────────────────────────────
# El orden importa: primero los dígrafos, después las letras sueltas.
_FONETICO = [
    (re.compile(r"[ÑN]"), "N"),      # la ñ colapsa con n (nadie tipea la tilde)
    (re.compile(r"QU"), "K"),
    (re.compile(r"C(?=[EI])"), "S"),  # ce/ci suenan s en el Río de la Plata
    (re.compile(r"[CK]"), "K"),
    (re.compile(r"Z"), "S"),
    (re.compile(r"[BV]"), "B"),
    (re.compile(r"LL"), "Y"),
    (re.compile(r"H"), ""),           # h muda
    (re.compile(r"G(?=[EI])"), "J"),
    (re.compile(r"X"), "S"),
    (re.compile(r"W"), "B"),
    (re.compile(r"(.)\1+"), r"\1"),   # letras repetidas (Anna = Ana)
]


def fonetico(texto):
    """Clave fonética: colapsa las confusiones ortográficas del español.

    "Chamiso" y "Chamizo" dan la misma clave; también "Balle"/"Valle" y
    "Sarandi"/"Zarandi". Agrupa de más a propósito: es una red de seguridad
    después del match exacto, no un reemplazo suyo.
    """
    t = normalizar(texto)
    for rx, rep in _FONETICO:
        t = rx.sub(rep, t)
    return t


# ── números: cifra <-> palabra ────────────────────────────────────────────
_UNIDADES = [
    "CERO", "UNO", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO",
    "NUEVE", "DIEZ", "ONCE", "DOCE", "TRECE", "CATORCE", "QUINCE", "DIECISEIS",
    "DIECISIETE", "DIECIOCHO", "DIECINUEVE", "VEINTE", "VEINTIUNO", "VEINTIDOS",
    "VEINTITRES", "VEINTICUATRO", "VEINTICINCO", "VEINTISEIS", "VEINTISIETE",
    "VEINTIOCHO", "VEINTINUEVE",
]
_DECENAS = {30: "TREINTA", 40: "CUARENTA", 50: "CINCUENTA", 60: "SESENTA",
            70: "SETENTA", 80: "OCHENTA", 90: "NOVENTA"}
_CENTENAS = {1: "CIENTO", 2: "DOSCIENTOS", 3: "TRESCIENTOS", 4: "CUATROCIENTOS",
             5: "QUINIENTOS", 6: "SEISCIENTOS", 7: "SETECIENTOS",
             8: "OCHOCIENTOS", 9: "NOVECIENTOS"}
_ORDINALES = {1: "PRIMERO", 2: "SEGUNDO", 3: "TERCERO", 4: "CUARTO", 5: "QUINTO",
              6: "SEXTO", 7: "SEPTIMO", 8: "OCTAVO", 9: "NOVENO", 10: "DECIMO"}


def numero_a_palabra(n):
    """Entero -> palabras en MAYÚSCULAS ('25' -> 'VEINTICINCO')."""
    n = int(n)
    if n < 0:
        return str(n)
    if n < 30:
        return _UNIDADES[n]
    if n < 100:
        d, u = divmod(n, 10)
        return _DECENAS[d * 10] + (" Y " + _UNIDADES[u] if u else "")
    if n < 1000:
        c, r = divmod(n, 100)
        if c == 1 and r == 0:
            return "CIEN"
        return _CENTENAS[c] + (" " + numero_a_palabra(r) if r else "")
    if n < 1000000:
        m, r = divmod(n, 1000)
        base = "MIL" if m == 1 else numero_a_palabra(m) + " MIL"
        return base + (" " + numero_a_palabra(r) if r else "")
    return str(n)


# Diccionario inverso palabra -> número, con las formas alternativas que la gente
# escribe: "veintiun" por "veintiuno", ordinales, y "primer/tercer" apocopados.
PALABRA_A_NUMERO = {}
for _i in range(0, 2001):
    PALABRA_A_NUMERO.setdefault(numero_a_palabra(_i), _i)
for _i, _p in _ORDINALES.items():
    PALABRA_A_NUMERO.setdefault(_p, _i)
PALABRA_A_NUMERO.update({"PRIMER": 1, "TERCER": 3, "VEINTIUN": 21, "UN": 1, "UNA": 1})

_RX_DIGITOS = re.compile(r"\d+")
# El orden por longitud descendente evita que "TRES" se coma "TREINTA Y TRES".
_RX_PALABRA_NUM = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in
                      sorted(PALABRA_A_NUMERO, key=len, reverse=True)) + r")\b")


def tiene_numero(texto):
    """¿El nombre contiene un número, escrito en cifra o en palabra?"""
    t = normalizar(texto)
    return bool(_RX_DIGITOS.search(t) or _RX_PALABRA_NUM.search(t))


def variantes_numero(texto):
    """Todas las formas de escribir los números de un nombre.

    Devuelve un conjunto que SIEMPRE incluye la forma normalizada de entrada.
    "25 DE AGOSTO" -> {"25 DE AGOSTO", "VEINTICINCO DE AGOSTO"}
    "TREINTA Y TRES" -> {"TREINTA Y TRES", "33"}
    """
    base = normalizar(texto)
    salida = {base}

    # cifra -> palabra
    if _RX_DIGITOS.search(base):
        salida.add(_RX_DIGITOS.sub(lambda m: numero_a_palabra(m.group()), base))

    # palabra -> cifra. Se reemplaza la coincidencia MÁS LARGA de una sola vez:
    # "TREINTA Y TRES" es 33, no "30 Y 3".
    m = _RX_PALABRA_NUM.search(base)
    if m:
        salida.add(base[:m.start()] + str(PALABRA_A_NUMERO[m.group(1)]) + base[m.end():])

    # "KILOMETRO 24" <-> "KM 24" ya lo resuelve la expansión de abreviaturas;
    # acá se agrega la forma pegada ("KM24"), que también se escribe.
    pegado = re.sub(r"\bKILOMETRO\s+(\d+)", r"KILOMETRO\1", base)
    if pegado != base:
        salida.add(pegado)
    return salida


# ── distancia de edición ──────────────────────────────────────────────────
def distancia(a, b):
    """Levenshtein con transposición de adyacentes (Damerau-Levenshtein).

    La transposición cuenta como UN error porque invertir dos letras es el error
    de tipeo más común y no debería costar el doble que omitir una.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    anterior2, anterior = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        actual = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            actual[j] = min(anterior[j] + 1,           # borrado
                            actual[j - 1] + 1,          # inserción
                            anterior[j - 1] + (ca != cb))  # sustitución
            if (i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb):
                actual[j] = min(actual[j], anterior2[j - 2] + 1)  # transposición
        anterior2, anterior = anterior, actual
    return anterior[len(b)]


def similitud(a, b):
    """Distancia normalizada a [0, 1]; 1 es idéntico."""
    if not a and not b:
        return 1.0
    largo = max(len(a), len(b))
    return 1.0 - distancia(a, b) / largo if largo else 1.0
