"""comun/no_respondible.py — Cuando el censo no relevó lo que se pregunta.

El mensaje era una sola frase fija: "Esa pregunta no puede responderse con las
variables disponibles". Correcta de fondo y casi inútil de forma. Ante

    "¿Cuántos hombres solteros hay de entre 40 y 47 años en Montevideo y son
     heterosexuales?"

el usuario no tiene manera de saber CUÁL de las cuatro condiciones sobra. Puede
irse pensando que el sistema no entiende "solteros", o que Montevideo no está.
Y encima tres de las cuatro condiciones sí se podían responder: había 10.963
hombres solteros de 40 a 47 en Montevideo esperando del otro lado.

Es el mismo defecto que se corrigió hoy en el resultado vacío (comun/diagnostico.py)
y en los chips que reemplazaban la pregunta: el sistema sabe algo y no lo dice.

Dos piezas:

  1. El generador de SQL devuelve `NO_RESPONDIBLE: <el dato que falta>` en vez de
     `NO_RESPONDIBLE` a secas. El modelo ya sabía cuál era; nadie se lo preguntaba.
  2. Con eso se arma un mensaje que NOMBRA lo que falta y se ofrece la misma
     pregunta sin esa condición, como opción, no como decisión tomada.

`MARCA_OMITIR` es lo que hace posible lo segundo sin bucle: la pregunta que vuelve
sigue diciendo "heterosexuales", así que sin una instrucción explícita el modelo
volvería a rechazarla. Es el mismo recurso que MARCA_CRITERIO y MARCA_LECTURA.
"""
import re

TOKEN = "NO_RESPONDIBLE"
MARCA_OMITIR = "Omitir la condición que este censo no relevó:"

# El token tiene que terminar ahí: 2011 usa además NO_RESPONDIBLE_VIVIENDAS para
# otra cosa, y un startswith a secas se lo comería.
_RX = re.compile(r"^\s*%s(?![\w])\s*[:\-–—]?\s*(.*)$" % TOKEN,
                 re.IGNORECASE | re.DOTALL)

# Instrucción para el prompt de los cuatro motores. Vive acá para que los cuatro
# digan exactamente lo mismo: cuando estaba escrita en cada prompt, cada censo
# rechazaba con su propia redacción.
INSTRUCCION_PROMPT = (
    "- Si la pregunta no puede responderse con este esquema, devolvé\n"
    "  %s: <el dato que falta, en pocas palabras, ej. 'la orientación sexual'>\n"
    "  Nombrá SOLO la condición que sobra, no la pregunta entera, y NO uses una\n"
    "  variable parecida como si fuera esa.\n"
    "- Si la pregunta trae «%s X», generá el SQL\n"
    "  IGNORANDO esa condición y respondiendo todo lo demás con normalidad."
    % (TOKEN, MARCA_OMITIR))


def es(salida):
    """¿El generador dijo que no se puede responder?"""
    return bool(salida) and _RX.match(salida.strip()) is not None


def dato_faltante(salida):
    """Lo que el modelo dijo que falta, o '' si no lo dijo."""
    m = _RX.match((salida or "").strip())
    if not m:
        return ""
    # Una línea, sin comillas ni punto final: se incrusta en una oración.
    dato = m.group(1).strip().splitlines()[0] if m.group(1).strip() else ""
    return dato.strip(" .;\"'«»").strip()


def sin_la_condicion(pregunta, dato):
    """La misma pregunta, marcada para que se responda sin la condición imposible."""
    return "%s — %s %s" % ((pregunta or "").strip(), MARCA_OMITIR, dato)


def mensaje(dato, censo):
    """Qué se le dice al usuario. Nombra el dato cuando el modelo lo nombró."""
    if not dato:
        return ("Esa pregunta no puede responderse con las variables que relevó el "
                "Censo %s." % censo)
    return ("El Censo %s no relevó %s, así que no puedo filtrar por esa condición: "
            "no hay ninguna variable con ese dato." % (censo, dato))


def opciones(pregunta, dato, censo):
    """El chip que ofrece la cifra sin la condición imposible. Vacío si no se sabe cuál."""
    if not dato or not pregunta:
        return []
    return [{"texto": "¿Querés la cifra sin esa condición?",
             "detalle": "todo lo demás de la pregunta, sin %s" % dato,
             "pregunta": sin_la_condicion(pregunta, dato),
             "clave": "sin_condicion", "censo": censo}]
