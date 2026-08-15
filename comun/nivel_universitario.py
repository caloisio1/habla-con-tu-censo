"""comun/nivel_universitario.py — Qué se cuenta cuando alguien dice "universidad".

El posgrado es una categoría APARTE del máximo nivel alcanzado, no un escalón que
lo contenga: quien tiene un doctorado NO figura entre los universitarios salvo que
se lo agregue a propósito. En el Censo 2023 la diferencia es 13,61 % contra
16,48 %, casi tres puntos.

Hasta el 15-ago-2026 el sistema no elegía: preguntaba con un chip. Decisión de
Carlos ese día: **no hace falta preguntar, la pregunta ya lo dice**.

    "universidad" / "universitario"        -> SOLO «Universidad o similar»
    "universidad o más" / "o superior"     -> «Universidad o similar» Y «Posgrado»

Es la misma idea que comun/edad.py: la convención se lee de cómo está escrita la
pregunta, se le pasa al modelo como instrucción obligatoria y se DECLARA en la
respuesta, así el usuario siempre sabe qué se contó. Acá no ejecuta nada: devuelve
el criterio y la frase con que hay que declararlo.

Por censo:
  2023  NIVELEDU25MAS   9 = Universidad o similar   10 = Postgrado
  2011  Niveledu_r      9 = Universidad o similar   10 = Posgrado
  1996  no aplica: `nivel` tiene UNA sola categoría universitaria (6 = Universidad),
        así que no hay nada que decidir y la pregunta se contesta de una.
  2004  no relevó educación.
"""
import re

from comun.texto import normalizar

SOLO = "solo"          # solo «Universidad o similar»
CON_POSGRADO = "con_posgrado"

# Variable y códigos de cada censo. None = el censo no tiene la distinción.
VARIABLE = {
    "2023": ("NIVELEDU25MAS", "9", "10"),
    "2011": ("Niveledu_r", "9", "10"),
    "1996": None,
    "2004": None,
}

# ── que la pregunta hable de universidad ─────────────────────────────────
# UNIVERSITARI cubre universitario/universitaria/universitarios/universitarias.
_RX_UNIVERSIDAD = re.compile(
    r"UNIVERSITARI|UNIVERSIDAD|\bLICENCIATURA\b|\bCARRERA DE GRADO\b")

# ── que ADEMÁS abra el rango hacia arriba ────────────────────────────────
# "o más", "o superior", "o mayor", "en adelante", "al menos", "como mínimo".
# También cuenta nombrar el posgrado a propósito: si la pregunta dice "universidad
# o posgrado" ya eligió, y la respuesta tiene que declararlo igual.
_RX_HACIA_ARRIBA = re.compile(
    r"(?:UNIVERSITARI\w*|UNIVERSIDAD)\s*(?:\w+\s+){0,2}?(?:O|Y)\s+(?:MAS|MAYOR\w*|"
    r"SUPERIOR\w*|ARRIBA|ALTO|ALTA|ALTOS|ALTAS)\b"
    r"|\bEN ADELANTE\b"
    r"|\b(?:AL MENOS|COMO MINIMO|MINIMO|DESDE)\s+(?:\w+\s+){0,3}?(?:UNIVERSITARI\w*|UNIVERSIDAD)\b"
    r"|\b(?:POSGRADO|POSTGRADO|MAESTRIA|DOCTORADO|DIPLOMA)\b")


def detectar(pregunta, censo="2023"):
    """SOLO, CON_POSGRADO, o None si la pregunta no habla de nivel universitario."""
    if not pregunta or not VARIABLE.get(censo):
        return None
    t = normalizar(pregunta)
    if not _RX_UNIVERSIDAD.search(t):
        return None
    return CON_POSGRADO if _RX_HACIA_ARRIBA.search(t) else SOLO


def instruccion(pregunta, censo="2023"):
    """La instrucción obligatoria para el generador de SQL, o '' si no aplica."""
    alcance = detectar(pregunta, censo)
    if alcance is None:
        return ""
    variable, universidad, posgrado = VARIABLE[censo]
    if alcance == SOLO:
        return ("NIVEL UNIVERSITARIO: la pregunta dice «universidad» sin abrirla hacia "
                "arriba, así que contá SOLO %s = '%s' («Universidad o similar»). NO "
                "incluyas '%s' (Posgrado): en esta variable el posgrado es una categoría "
                "aparte del máximo nivel alcanzado, no un escalón que contenga a la "
                "universidad." % (variable, universidad, posgrado))
    return ("NIVEL UNIVERSITARIO: la pregunta abre el nivel hacia arriba, así que contá "
            "%s IN ('%s','%s') — «Universidad o similar» y también «Posgrado»."
            % (variable, universidad, posgrado))


def declaraciones(pregunta, censo="2023"):
    """Las frases que la respuesta DEBE declarar. Lista (vacía si no aplica).

    Se declara en los dos casos, no solo cuando se agrega el posgrado: una cifra de
    universitarios que excluye a los doctorados es exactamente igual de sorprendente
    que una que los incluye, y quien la lea tiene que poder auditarla.
    """
    alcance = detectar(pregunta, censo)
    if alcance is None:
        return []
    if alcance == SOLO:
        return ["se contó «Universidad o similar»; el posgrado es una categoría aparte "
                "del máximo nivel alcanzado y no está incluido"]
    return ["se contó «Universidad o similar» y también «Posgrado»"]
