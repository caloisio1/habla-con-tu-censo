"""comun/edad.py — Convención etaria demográfica, compartida por los cuatro motores.

El defecto que corrige: "mayores de 65 años" se traducía LITERALMENTE a `edad > 65`
en los cuatro motores, dejando afuera a las personas de exactamente 65 años. En el
Censo 2023 eso son 35.052 personas; en 1996, 31.750.

La convención demográfica —la que usan el INE y la CEPAL— es que "mayores de X",
"X y más" y "de X o más" son la MISMA población y todas incluyen la edad
mencionada: `edad >= X`. No es una interpretación: es cómo se publican los cuadros
de población por grupos de edad.

Este módulo no ejecuta SQL: lee la pregunta, devuelve el criterio en forma
explícita y la frase con que hay que declararlo. El criterio se le pasa al modelo
como instrucción obligatoria y se declara en la respuesta, así el usuario siempre
sabe qué se contó.
"""
import re
from collections import namedtuple

from comun.texto import PALABRA_A_NUMERO, normalizar

# operador: '>=', '<=', '<', '>' o 'entre'. declaracion: la frase para la respuesta.
Criterio = namedtuple("Criterio", "operador limite limite2 declaracion expresion frase_original")

_NUM = r"(\d{1,3}|[A-ZÑ]+(?:\s+Y\s+[A-ZÑ]+)?)"
# normalizar() conserva la ñ (es una letra del español, no un acento): "años" -> "AÑOS".
_ANOS = r"A[NÑ]OS?"


def _valor(texto):
    """'65' o 'SESENTA Y CINCO' -> 65; None si no es un número."""
    t = texto.strip()
    if t.isdigit():
        return int(t)
    return PALABRA_A_NUMERO.get(t)


# El orden importa: los patrones más específicos van primero. "de 65 años o más"
# tiene que ganarle a "de 65 años".
_PATRONES = [
    # ── inclusivos hacia arriba: TODOS son >=
    (r"\bENTRE\s+" + _NUM + r"\s*(?:" + _ANOS + r")?\s*(?:Y|A)\s+" + _NUM + r"\s*" + _ANOS + r"", "entre"),
    (r"\b" + _NUM + r"\s*" + _ANOS + r"\s+(?:Y|O)\s+MAS\b", ">="),
    (r"\bDE\s+" + _NUM + r"\s*(?:" + _ANOS + r")?\s+(?:Y|O)\s+MAS\b", ">="),
    (r"\b" + _NUM + r"\s*(?:" + _ANOS + r")?\s+(?:Y|O)\s+MAS\b", ">="),
    (r"\bMAYORES?\s+DE\s+" + _NUM + r"\s*" + _ANOS + r"", ">="),
    (r"\bDESDE\s+(?:LOS\s+)?" + _NUM + r"\s*" + _ANOS + r"", ">="),
    (r"\bA\s+PARTIR\s+DE\s+(?:LOS\s+)?" + _NUM + r"\s*" + _ANOS + r"", ">="),
    (r"\bMAS\s+DE\s+" + _NUM + r"\s*" + _ANOS + r"", ">="),
    # ── hacia abajo
    (r"\b" + _NUM + r"\s*" + _ANOS + r"\s+(?:O|Y)\s+MENOS\b", "<="),
    (r"\bHASTA\s+(?:LOS\s+)?" + _NUM + r"\s*" + _ANOS + r"", "<="),
    (r"\bMENORES?\s+DE\s+" + _NUM + r"\s*" + _ANOS + r"", "<"),
    (r"\bMENOS\s+DE\s+" + _NUM + r"\s*" + _ANOS + r"", "<"),
]
_COMPILADOS = [(re.compile(rx), op) for rx, op in _PATRONES]


def detectar(pregunta):
    """Devuelve la lista de criterios etarios de una pregunta (normalmente 0 o 1)."""
    t = normalizar(pregunta)
    salida, ocupados = [], []
    for rx, op in _COMPILADOS:
        for m in rx.finditer(t):
            if any(m.start() < fin and ini < m.end() for ini, fin in ocupados):
                continue    # ya lo capturó un patrón más específico
            grupos = [g for g in m.groups() if g]
            x = _valor(grupos[0]) if grupos else None
            if x is None:
                continue
            y = _valor(grupos[1]) if op == "entre" and len(grupos) > 1 else None
            if op == "entre" and y is None:
                continue
            ocupados.append((m.start(), m.end()))
            salida.append(_criterio(op, x, y, m.group(0)))
    return salida


def _criterio(op, x, y, frase):
    if op == "entre":
        lo, hi = min(x, y), max(x, y)
        return Criterio("entre", lo, hi,
                        "población de %d a %d años (ambos extremos incluidos)" % (lo, hi),
                        "edad >= %d AND edad <= %d" % (lo, hi), frase)
    if op == ">=":
        return Criterio(">=", x, None, "población de %d años y más" % x,
                        "edad >= %d" % x, frase)
    if op == "<=":
        return Criterio("<=", x, None, "población de %d años o menos" % x,
                        "edad <= %d" % x, frase)
    return Criterio("<", x, None, "población menor de %d años (no incluye a los de %d)" % (x, x),
                    "edad < %d" % x, frase)


# ── dominio de la columna de edad en cada censo ──────────────────────────
# El modelo venía improvisando qué códigos de edad son "perdidos": una corrida
# excluía el 99 y otra no, y la misma pregunta daba 545.299 o 544.857 personas
# (442 de diferencia, las de 99 años exactos). Declarar el dominio acá lo cierra.
#
# Verificado sobre las bases: 1996 llega hasta 99 (que es el TOPE, "99 y más", NO
# un centinela); 2004, 2011 y 2023 llegan a 118, 111 y 112 y no usan 99 como
# marca. Los únicos centinelas reales son los códigos largos de 2023.
DOMINIO = {
    "1996": {"columna": "edad", "excluir": (),
             "nota": "edad=99 es el TOPE ('99 años y más'), NO un código perdido: "
                     "NO lo excluyas"},
    "2004": {"columna": "edad", "excluir": (),
             "nota": "la edad llega hasta 118 y no usa códigos centinela"},
    "2011": {"columna": "edad", "excluir": (),
             "nota": "la edad llega hasta 111 y no usa códigos centinela; las 53 personas "
                     "con la edad bajo secreto estadístico ya vienen con edad NULL"},
    "2023": {"columna": "PERNA01", "excluir": (7777, 8888, 9898, 9999),
             "nota": "99 es una edad VÁLIDA, no un perdido: NO la excluyas"},
}


def regla_perdidos(censo):
    """Qué excluir (y qué NO) al filtrar por edad, en palabras para el prompt."""
    d = DOMINIO.get(censo)
    if not d:
        return ""
    col = d["columna"]
    partes = ["- Perdidos de %s: excluí SIEMPRE los NULL%s." % (
        col, " y los códigos %s" % ", ".join(str(c) for c in d["excluir"])
        if d["excluir"] else "")]
    partes.append("  %s. NO agregues ningún otro código a esa lista." % d["nota"])
    return "\n".join(partes)


def instruccion(pregunta, columna_edad="edad", censo=None):
    """Instrucción OBLIGATORIA para el prompt de SQL, o cadena vacía.

    Se arma con el nombre real de la columna de edad de cada censo, que es lo
    único que cambia entre motores (`edad` en 1996/2011/2004, `PERNA01` en 2023),
    y con el dominio de esa columna, para que el modelo no invente qué códigos
    son perdidos.
    """
    criterios = detectar(pregunta)
    if not criterios:
        return ""
    lineas = ["CRITERIO ETARIO OBLIGATORIO (convención demográfica del INE; NO lo "
              "traduzcas literalmente, usá EXACTAMENTE estos operadores):"]
    for c in criterios:
        expr = c.expresion.replace("edad", columna_edad)
        lineas.append('- "%s" -> %s   (%s)' % (c.frase_original.lower(), expr, c.declaracion))
    regla = regla_perdidos(censo) if censo else ""
    if regla:
        lineas.append(regla)
    lineas.append("El criterio se declara en el TEXTO de la respuesta: NO agregues una "
                  "columna al SELECT para escribirlo.")
    return "\n".join(lineas)


def declaraciones(pregunta):
    """Las frases con que la respuesta tiene que declarar el criterio."""
    return [c.declaracion for c in detectar(pregunta)]
