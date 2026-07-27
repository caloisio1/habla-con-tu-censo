"""comun/indicadores.py — Desambiguación de indicadores ambiguos.

Bug 3 del INE: "¿cuál es el departamento más educado?" devolvía un número. Cada
motor elegía una definición distinta —promedio de años de estudio en 1996, el
mismo promedio pero sin universo en 2011, porcentaje con terciaria en 2023— y
ninguno declaraba cuál había usado. De ahí que las cifras "no cuadraran": no
estaban mal calculadas, estaban midiendo cosas distintas.

Regla: ante un indicador ambiguo el sistema NO elige. Ofrece las
interpretaciones posibles para que elija quien pregunta, SIN ejecutar SQL y sin
llamar al redactor (la respuesta de desambiguación es barata: no consume ninguna
de las dos llamadas al modelo).

Las opciones se ajustan al censo seleccionado: se ofrecen solo las que se pueden
calcular con las variables que ese censo relevó. El Censo 2004 Fase 1 no relevó
educación, así que ahí no hay opciones que ofrecer y corresponde decirlo.
"""
import re
from collections import namedtuple

from comun.texto import normalizar

# universo y perdidos se declaran SIEMPRE: son la mitad de la razón por la que
# dos cifras del mismo indicador no coinciden.
Opcion = namedtuple("Opcion", "clave titulo universo perdidos pregunta")
Indicador = namedtuple("Indicador", "clave titulo pregunta_guia opciones_por_censo")


def _o(clave, titulo, universo, perdidos, pregunta):
    return Opcion(clave, titulo, universo, perdidos, pregunta)


# ── educación ────────────────────────────────────────────────────────────
_EDU_1996 = [
    _o("anios_estudio", "Promedio de años de estudio aprobados",
       "población de 3 años y más", "excluye el código 99 (no sabe / no contesta)",
       "¿Cuál es el promedio de años de estudio aprobados por departamento, "
       "en la población de 3 años y más, excluyendo los no declarados?"),
    _o("terciaria_completa", "Porcentaje con nivel terciario o universitario completo",
       "población de 3 años y más con nivel declarado", "excluye el código 9 (ignorado)",
       "¿Qué porcentaje de la población de 3 años y más completó un nivel terciario "
       "o universitario, por departamento?"),
    _o("secundaria_completa", "Porcentaje con secundaria completa",
       "población de 3 años y más con nivel declarado", "excluye el código 9 (ignorado)",
       "¿Qué porcentaje de la población de 3 años y más completó secundaria, por departamento?"),
    _o("alfabetismo", "Tasa de alfabetismo",
       "población de 3 años y más", "excluye el código 9 (ignorado)",
       "¿Qué porcentaje de la población de 3 años y más sabe leer y escribir, por departamento?"),
]

_EDU_2011 = [
    _o("anios_estudio", "Promedio de años de estudio aprobados",
       "población de 25 años y más", "excluye el código 88 (no relevado)",
       "¿Cuál es el promedio de años de estudio aprobados por departamento, en la "
       "población de 25 años y más, excluyendo el código 88?"),
    _o("terciaria_completa", "Porcentaje con nivel terciario o universitario completo",
       "población de 25 años y más con nivel declarado", "excluye nivel no declarado",
       "¿Qué porcentaje de la población de 25 años y más completó un nivel terciario "
       "o universitario, por departamento?"),
    _o("secundaria_completa", "Porcentaje con secundaria completa",
       "población de 25 años y más con nivel declarado", "excluye nivel no declarado",
       "¿Qué porcentaje de la población de 25 años y más completó secundaria, por departamento?"),
    _o("alfabetismo", "Tasa de alfabetismo",
       "población de 10 años y más", "excluye los no declarados",
       "¿Qué porcentaje de la población de 10 años y más sabe leer y escribir, por departamento?"),
]

_EDU_2023 = [
    _o("terciaria_completa", "Porcentaje con nivel terciario o universitario alcanzado",
       "población de 25 años y más", "excluye 0 (menor de 25) y los códigos de no respuesta",
       "¿Qué porcentaje de la población de 25 años y más alcanzó un nivel terciario "
       "o universitario, por departamento?"),
    _o("media_superior", "Porcentaje con educación media superior o más",
       "población de 25 años y más", "excluye 0 (menor de 25) y los códigos de no respuesta",
       "¿Qué porcentaje de la población de 25 años y más alcanzó al menos educación "
       "media superior, por departamento?"),
    _o("nunca_asistio", "Porcentaje que nunca asistió a un centro educativo",
       "población de 25 años y más", "excluye 0 (menor de 25) y los códigos de no respuesta",
       "¿Qué porcentaje de la población de 25 años y más nunca asistió, por departamento?"),
    _o("asistencia", "Porcentaje que asiste actualmente a un centro educativo",
       "población total con respuesta válida", "excluye los códigos de no respuesta",
       "¿Qué porcentaje de la población asiste actualmente a un centro educativo, "
       "por departamento?"),
]

# ── condiciones de vivienda ──────────────────────────────────────────────
_VIV_COMUN = [
    _o("tenencia", "Porcentaje de hogares propietarios de su vivienda",
       "hogares particulares con tenencia declarada", "excluye los no declarados",
       "¿Qué porcentaje de los hogares es propietario de su vivienda, por departamento?"),
    _o("hacinamiento", "Porcentaje de hogares en situación de hacinamiento",
       "hogares particulares", "excluye los no declarados",
       "¿Qué porcentaje de los hogares está en situación de hacinamiento, por departamento?"),
]

INDICADORES = [
    Indicador(
        "educacion", "nivel educativo",
        "¿Con qué criterio querés medir el nivel educativo?",
        {"1996": _EDU_1996, "2004": [], "2011": _EDU_2011, "2023": _EDU_2023}),
    Indicador(
        "vivienda", "condiciones de la vivienda",
        "¿Con qué criterio querés medir las condiciones de vivienda?",
        {"1996": _VIV_COMUN, "2004": [], "2011": _VIV_COMUN, "2023": _VIV_COMUN}),
]

# Frases que disparan la desambiguación. Se comparan sobre el texto normalizado.
_DISPARADORES = {
    "educacion": [
        r"MAS EDUCAD", r"MEJOR EDUCAD", r"MAYOR NIVEL EDUCATIVO",
        r"NIVEL EDUCATIVO MAS ALTO", r"MAS INSTRUID", r"MEJOR NIVEL EDUCATIVO",
        r"MAS CULT", r"PEOR EDUCAD", r"MENOS EDUCAD",
    ],
    "vivienda": [
        r"MEJORES CONDICIONES DE VIVIENDA", r"PEORES CONDICIONES DE VIVIENDA",
        r"MEJOR VIVIENDA", r"PEOR VIVIENDA", r"MEJORES VIVIENDAS", r"PEORES VIVIENDAS",
    ],
}
_RX = {k: [re.compile(p) for p in v] for k, v in _DISPARADORES.items()}

# Indicadores ambiguos que NO tienen tabla de opciones todavía: se detectan igual
# para no responder un número inventado, y se pide precisión en palabras.
_SIN_TABLA = {
    "pobreza": ([r"MAS POBRE", r"MENOS POBRE", r"MAS RIC", r"MAS CARENCIAD"],
                "la pobreza puede medirse por cantidad de necesidades básicas "
                "insatisfechas (NBI), por tenencia de la vivienda o por acceso a "
                "servicios; cada una da un orden distinto"),
    "juventud": ([r"MAS JOVEN", r"MAS VIEJ", r"MAS ENVEJECID"],
                 "la estructura de edad puede medirse por edad promedio, por edad "
                 "mediana o por porcentaje de menores de 15 o de 65 y más"),
}
_RX_SIN_TABLA = {k: [re.compile(p) for p in v[0]] for k, v in _SIN_TABLA.items()}


def detectar(pregunta):
    """Clave del indicador ambiguo de la pregunta, o None."""
    t = normalizar(pregunta)
    for clave, rxs in _RX.items():
        if any(rx.search(t) for rx in rxs):
            return clave
    for clave, rxs in _RX_SIN_TABLA.items():
        if any(rx.search(t) for rx in rxs):
            return clave
    return None


def desambiguar(pregunta, censo):
    """Si la pregunta trae un indicador ambiguo, devuelve la respuesta de
    desambiguación lista para el frontend. Si no, None.

    NO ejecuta SQL ni llama al modelo: es la respuesta barata que pide el encargo.
    """
    clave = detectar(pregunta)
    if clave is None:
        return None

    if clave in _SIN_TABLA:
        return {
            "ok": False, "motivo": "consulta_ambigua", "sql": None,
            "respuesta": "Esa pregunta puede responderse de varias maneras y el "
                         "resultado cambia según cuál se elija: %s. Decime con qué "
                         "criterio querés medirlo y te doy la cifra."
                         % _SIN_TABLA[clave][1],
            "opciones": [],
        }

    ind = next(i for i in INDICADORES if i.clave == clave)
    opciones = ind.opciones_por_censo.get(censo, [])
    if not opciones:
        from comun import rechazos
        disponibles = [c for c, o in ind.opciones_por_censo.items() if o]
        return rechazos.a_respuesta(rechazos.no_relevada(ind.titulo, censo, disponibles))

    return {
        "ok": False, "motivo": "consulta_ambigua", "sql": None,
        "respuesta": '"%s" no es una sola cosa: se puede medir de varias maneras y '
                     "cada una da un orden distinto entre departamentos. %s"
                     % (ind.titulo.capitalize(), ind.pregunta_guia),
        "opciones": [{"texto": o.titulo, "detalle": "%s; %s" % (o.universo, o.perdidos),
                      "pregunta": o.pregunta, "clave": o.clave, "censo": censo}
                     for o in opciones],
    }
