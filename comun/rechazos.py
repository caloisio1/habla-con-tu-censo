"""comun/rechazos.py — Etiquetas de rechazo, únicas para los cuatro censos.

Regla de oro: **nada que no sea una supresión real puede reportarse como
confidencialidad.** El sistema venía rotulando "suprimido por confidencialidad"
cualquier consulta que no devolviera filas publicables, incluida la que
simplemente no había encontrado el nombre de una localidad. Ese rótulo hace
desconfiar de un dato que nunca estuvo mal: solo no se reconoció el nombre.

Cada motivo tiene un código estable (para la telemetría y para el frontend) y un
texto en español. Dos formulaciones equivalentes de la misma pregunta tienen que
caer en el mismo código.
"""
from collections import namedtuple

Rechazo = namedtuple("Rechazo", "codigo mensaje opciones sugerencias detalle")

# ── códigos ──────────────────────────────────────────────────────────────
SUPRESION = "supresion"                  # la ÚNICA que puede hablar de confidencialidad
NO_ENCONTRADA = "entidad_no_encontrada"
AMBIGUA = "consulta_ambigua"
FRAGMENTADA = "entidad_fragmentada"
NO_RELEVADA = "variable_no_relevada"
OTRO_CENSO = "entidad_en_otro_censo"
PROCESAMIENTO = "error_de_procesamiento"

UMBRAL = 5   # el umbral de supresión del INE; se declara en el mensaje


def _r(codigo, mensaje, opciones=(), sugerencias=(), detalle=None):
    return Rechazo(codigo, mensaje, tuple(opciones), tuple(sugerencias), detalle)


def supresion(celdas, unidad="personas"):
    """Supresión REAL: hubo casos, pero menos de 5 en cada celda publicable."""
    return _r(SUPRESION,
              "No se puede publicar el resultado: %s con menos de %d %s se "
              "suprimen por confidencialidad estadística (secreto estadístico)."
              % ("la celda quedó" if celdas == 1 else "las %d celdas quedaron" % celdas,
                 UMBRAL, unidad),
              detalle={"celdas": celdas, "umbral": UMBRAL})


def no_encontrada(texto, sugerencias=(), tipo="localidad"):
    """No se reconoció el nombre. NUNCA se rotula como confidencialidad."""
    nombres = [s if isinstance(s, str) else s.nombre for s in sugerencias]
    if nombres:
        cola = " ¿Quisiste decir %s?" % _enumerar(nombres)
    else:
        cola = " Revisá cómo se escribe o probá con otro nombre."
    return _r(NO_ENCONTRADA,
              'No encontré ninguna %s con el nombre "%s" en este censo.%s'
              % (tipo, texto, cola),
              sugerencias=nombres, detalle={"texto": texto, "tipo": tipo})


def ambigua(texto, opciones, tipo="localidad"):
    """Varias entidades distintas con el mismo nombre: elige el usuario, no el sistema."""
    return _r(AMBIGUA,
              '"%s" puede referirse a más de una cosa en este censo. Elegí cuál '
              "querés consultar:" % texto,
              opciones=opciones, detalle={"texto": texto, "tipo": tipo})


def fragmentada(texto, opciones):
    """Un mismo lugar repartido entre departamentos (caso Cerro Chato)."""
    deptos = [o.get("detalle", "") for o in opciones]
    return _r(FRAGMENTADA,
              '"%s" está dividida entre %d departamentos. Puedo darte el total '
              "sumado o el desglose por departamento: elegí cuál." % (texto, len(deptos)),
              opciones=opciones, detalle={"texto": texto, "partes": len(opciones)})


def no_relevada(variable, censo, disponibles=()):
    """La variable no está en el cuestionario de ese censo."""
    cola = ""
    if disponibles:
        cola = " Sí está disponible en %s." % _enumerar(["el Censo %s" % c for c in disponibles])
    return _r(NO_RELEVADA,
              "El Censo %s no relevó %s, así que no puedo responder esa pregunta "
              "con sus datos.%s" % (censo, variable, cola),
              detalle={"variable": variable, "censo": censo,
                       "disponibles": list(disponibles)})


def en_otro_censo(texto, censo, censos, sugerencias=(), tipo="localidad"):
    """Existe, pero no en el censo elegido: se dice explícitamente."""
    nombres = [s if isinstance(s, str) else s.nombre for s in sugerencias]
    return _r(OTRO_CENSO,
              '"%s" no figura con ese nombre en el Censo %s; sí en %s. Los '
              "nomenclátores cambian de un censo a otro: hay localidades que se "
              "renombran y otras que quedan absorbidas por una ciudad vecina."
              % (texto, censo, _enumerar(["el Censo %s" % c for c in censos])),
              sugerencias=nombres,
              detalle={"texto": texto, "censo": censo, "censos": list(censos)})


def procesamiento(motivo=""):
    """Cualquier fallo técnico. No es confidencialidad ni es 'seguridad'."""
    return _r(PROCESAMIENTO,
              "No pude procesar esa consulta. Probá reformulándola de otra manera.",
              detalle={"motivo": motivo})


def _enumerar(items):
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " o " + items[-1]


def a_respuesta(rechazo, sql=None):
    """Convierte un Rechazo en el diccionario que devuelven los cuatro motores."""
    salida = {"ok": False, "respuesta": rechazo.mensaje, "motivo": rechazo.codigo, "sql": sql}
    if rechazo.opciones:
        salida["opciones"] = list(rechazo.opciones)
    if rechazo.sugerencias:
        salida["sugerencias"] = list(rechazo.sugerencias)
    return salida
