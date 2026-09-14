"""comun/pipeline.py — Pasos compartidos por los cuatro motores.

Los motores siguen siendo cuatro porque sus esquemas, sus nomenclátores y su
forma de contar son distintos, pero los pasos que el INE encontró rotos son los
mismos en todos. Ponerlos acá es lo que garantiza que un fix no quede en un censo
y falte en otro.

Orden de una consulta, con el paso compartido entre paréntesis:

  1. ¿el indicador es ambiguo?          -> (antes)  responde opciones, sin SQL ni LLM
  2. ¿hay un corte etario en el texto?  -> (antes)  criterio obligatorio para el prompt
  3. el modelo genera el SQL
  4. ¿el SQL nombra entidades?          -> (sobre_sql) resuelve, reescribe o pregunta
  5. el guard valida y se ejecuta
  6. supresión de celdas chicas         -> (sobre_filas) distingue vacío de suprimido
  7. el redactor narra                  -> (nota_final) declara interpretación y criterio
"""
import re

import usage_log   # telemetría: marcar los aciertos de caché (solo métricas)

from comun import (cache, diagnostico, edad, indicadores, nivel_universitario, rechazos,
                   supresion)
from comun.sql_entidades import (EntidadNoResuelta, canonizar, preparar_1996,
                                 resolver_en_sql)

# Columna de edad de cada censo: es lo único que cambia entre motores para el
# criterio etario.
COLUMNA_EDAD = {"1996": "edad", "2004": "edad", "2011": "edad", "2023": "PERNA01"}


def antes(texto, censo):
    """Chequeos previos a cualquier llamada al modelo.

    Devuelve (respuesta_corta, contexto). Si `respuesta_corta` no es None hay que
    devolverla tal cual: es una desambiguación o un "no relevado", y no cuesta
    ninguna llamada al modelo.
    """
    corta = indicadores.desambiguar(texto, censo)
    if corta is not None:
        return corta, {}
    contexto = {
        "instruccion_edad": edad.instruccion(texto, COLUMNA_EDAD.get(censo, "edad"), censo),
        "declaraciones_edad": edad.declaraciones(texto),
        "instruccion_nivel": nivel_universitario.instruccion(texto, censo),
        "declaraciones_nivel": nivel_universitario.declaraciones(texto, censo),
    }
    return None, contexto


# Las convenciones que se leen de la pregunta, se le imponen al modelo y se declaran
# en la respuesta. Agregar una es agregarla acá y en `antes()`: el resto del pipeline
# no necesita saber cuántas hay.
INSTRUCCIONES = ("instruccion_edad", "instruccion_nivel")
DECLARACIONES = ("declaraciones_edad", "declaraciones_nivel")


def mensaje_usuario(texto, contexto):
    """El mensaje `user` que recibe el generador de SQL.

    La pregunta va SIEMPRE al final: el prefijo del prompt tiene que quedar
    estable para que la API lo sirva desde el caché (99 % del prompt de 2023).
    """
    instrucciones = [(contexto or {}).get(k) or "" for k in INSTRUCCIONES]
    instr = "\n\n".join(i for i in instrucciones if i)
    return (instr + "\n\n" + texto) if instr else texto


_RX_CERCA = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def sql_generado(texto):
    """Saca la cerca de markdown con que el modelo envuelve el SQL.

    El prompt pide "sin markdown", pero hay modelos que igual devuelven
    ```sql ... ```. Con la cerca puesta el guard ve un texto que no empieza en
    SELECT y rechaza la consulta por "Solo se permiten consultas SELECT": la
    pregunta era correcta y el SQL también, se caía por el envoltorio.

    Se limpia acá, en el paso compartido, y no en cada motor: es la misma cerca en
    los cuatro censos. Es puro desenvolver — no toca el SQL de adentro, así que un
    modelo que ya respete el formato pasa sin cambios.
    """
    return _RX_CERCA.sub("", (texto or "").strip()).strip()


def totales_para_redactor(filas, columnas_conteo):
    """Línea con los totales YA SUMADOS de cada columna de conteo.

    El redactor sumaba las filas de cabeza para dar el total y a veces erraba: en
    un desglose de cuatro categorías que suman 240.191 escribió 234.291. Una cifra
    equivocada por aritmética es el peor error posible acá, y es evitable: la suma
    la hace Python y el modelo solo la transcribe. Vacío si hay una sola fila (no
    hay nada que sumar) o si no hay columnas de conteo.
    """
    if not filas or len(filas) < 2:
        return ""
    conteo = {c.lower() for c in columnas_conteo}
    sumas = {}
    for fila in filas:
        for col, valor in fila.items():
            if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                continue
            clave = col.lower()
            if clave in conteo and not clave.startswith(("pct", "porc", "proporcion")):
                sumas[col] = sumas.get(col, 0) + valor
    if not sumas:
        return ""
    detalle = "; ".join("%s=%d" % (c, v) for c, v in sumas.items())
    return ("\nTOTALES ya calculados por el sistema (%s). Si narrás un total, usá EXACTAMENTE "
            "estos números: NO sumes las filas vos." % detalle)


def sql_cacheado(pregunta, censo, contexto):
    """SQL ya generado para esta misma pregunta, o None.

    Ahorra la llamada CARA (la que razona). Devuelve el SQL CRUDO: el post-paso de
    entidades, el guard y la supresión se siguen ejecutando igual.
    """
    sql = cache.obtener(cache.SQL_DE_PREGUNTA, censo, mensaje_usuario(pregunta, contexto))
    if sql is not None:
        usage_log.marcar_cache("A")
    return sql


def recordar_sql(pregunta, censo, contexto, sql):
    if sql and not sql.strip().startswith("NO_RESPONDIBLE"):
        cache.guardar(cache.SQL_DE_PREGUNTA, censo,
                      mensaje_usuario(pregunta, contexto), sql)


# Lo que NO se cachea con el resultado: las opciones dependen de la PREGUNTA,
# no del SQL. "¿Cuánta gente vive en Salto?" y "¿...en el departamento de Salto?"
# ejecutan el MISMO SQL, pero la primera debe ofrecer el chip de la ciudad y la
# segunda no —el usuario ya aclaró—. Guardarlas bajo la clave del SQL le daría a
# una el chip de la otra, según cuál llegara primero.
_POR_PREGUNTA = ("opciones",)


def resultado_cacheado(sql_seguro, censo, alternativas=()):
    """Filas y texto ya calculados para este SQL, o None.

    La clave es el SQL CANONIZADO: dos consultas equivalentes que llegan con
    distinto formato comparten entrada. Las opciones se vuelven a adjuntar con
    las de ESTA consulta, no con las de la que llenó la caché.
    """
    guardado = cache.obtener(cache.RESULTADO_DE_SQL, censo, canonizar(sql_seguro))
    if guardado is None:
        return None
    usage_log.marcar_cache("B")
    salida = dict(guardado)
    if alternativas:
        salida["opciones"] = list(alternativas)
    return salida


def recordar_resultado(sql_seguro, censo, valor):
    limpio = {k: v for k, v in valor.items() if k not in _POR_PREGUNTA}
    cache.guardar(cache.RESULTADO_DE_SQL, censo, canonizar(sql_seguro), limpio)


def sobre_sql(sql, censo, pregunta=None):
    """Correcciones deterministas sobre el SQL generado.

    Devuelve (sql, interpretaciones, alternativas). `alternativas` son las otras
    lecturas posibles de un nombre ambiguo, que se ofrecen como chips junto a la
    respuesta. Lanza EntidadNoResuelta si hay que preguntar en vez de ejecutar.
    """
    corregido = sql
    if censo == "1996":
        corregido, _ = preparar_1996(corregido)
    corregido, interpretaciones, alternativas = resolver_en_sql(corregido, censo, pregunta)
    return corregido, interpretaciones, alternativas


SIN_CASOS = ("La consulta se ejecutó correctamente pero no encontró ningún caso que "
             "cumpla esas condiciones en este censo. No es un problema de "
             "confidencialidad: sencillamente no hay registros.")


def _por_que_vacio(mensaje, sql, base, censo):
    """Cambia el mensaje de vacío cuando la culpa es de un filtro, no del censo.

    "Sencillamente no hay registros" es una afirmación sobre el país, y era falsa
    cada vez que un filtro no coincidía con ningún valor de su variable: la
    consulta del municipio 'B' contestaba que no hay solteros universitarios de 40
    a 47 en el Municipio B, y había 628. Cuando se puede señalar el filtro, se
    señala; cuando no, el mensaje de siempre, que ahí sí es cierto.
    """
    if not (sql and base):
        return mensaje
    try:
        diag = diagnostico.filtro_sin_coincidencias(base, sql, censo)
        explicacion = diagnostico.explicar(diag)
    except Exception:                                    # noqa: BLE001
        return mensaje      # el diagnóstico mejora el mensaje, nunca impide responder
    if not explicacion:
        return mensaje
    return ("La consulta volvió vacía, pero no porque el censo no tenga esos casos. "
            + explicacion)


def sobre_filas(filas, columnas_conteo, unidad="personas", sql=None, base=None, censo=None):
    """Supresión con la regla corregida.

    Devuelve (filas, suprimidas, vacias, rechazo). `rechazo` no es None cuando no
    queda nada publicable, y distingue los dos casos que antes se confundían:
    no hubo casos (vacío) o los hubo pero son menos de cinco (supresión).

    `sql`, `base` y `censo` son opcionales y sirven solo para diagnosticar el vacío
    (ver `_por_que_vacio`); sin ellos el comportamiento es el de antes.
    """
    filas, suprimidas, vacias = supresion.suprimir_celdas_chicas(filas, columnas_conteo)
    if filas:
        return filas, suprimidas, vacias, None
    if suprimidas:
        return filas, suprimidas, vacias, rechazos.supresion(suprimidas, unidad)
    base_msg = SIN_CASOS if vacias else "La consulta no devolvió resultados."
    return filas, suprimidas, vacias, rechazos._r(
        "sin_casos", _por_que_vacio(base_msg, sql, base, censo))


def nota_final(interpretaciones, contexto):
    """Lo que la respuesta DEBE declarar: cómo se interpretó y qué se contó."""
    partes = []
    for i in dict.fromkeys(interpretaciones or []):
        partes.append("Se interpretó como %s." % i)
    for d in dict.fromkeys(_declaraciones(contexto)):
        partes.append("Criterio: %s." % d)
    return " ".join(partes)


def _declaraciones(contexto):
    """Todas las declaraciones del contexto, en orden y sin repetir."""
    salida = []
    for clave in DECLARACIONES:
        salida += list((contexto or {}).get(clave) or [])
    return list(dict.fromkeys(salida))


def instruccion_redactor(interpretaciones, contexto):
    """Instrucción para el redactor: la interpretación y el criterio no son
    opcionales, se declaran siempre en el texto de la respuesta."""
    nota = nota_final(interpretaciones, contexto)
    if not nota:
        return ""
    return ("\nDECLARÁ SIEMPRE, en la primera oración, cómo se interpretó la "
            "pregunta y qué población se contó, con estas palabras: " + nota)


def asegurar_declaracion(respuesta, interpretaciones, contexto):
    """Garantiza que la respuesta declare la interpretación y el criterio usados.

    La instrucción al redactor no alcanza: obedece casi siempre, pero "casi" no
    sirve para una declaración metodológica. Si el texto ya la trae —que es lo
    normal y se lee mejor porque va integrada en la oración— se deja como está;
    si falta, se antepone. Así la declaración deja de depender del modelo.
    """
    nota = nota_final(interpretaciones, contexto)
    if not nota:
        return respuesta
    texto = respuesta or ""
    faltan = [frase for frase in _claves(interpretaciones, contexto)
              if frase.lower() not in texto.lower()]
    if not faltan:
        return respuesta
    return nota + " " + texto.lstrip()


def _claves(interpretaciones, contexto):
    """Las frases cuya presencia se verifica en el texto de la respuesta."""
    claves = list(dict.fromkeys(interpretaciones or []))
    claves += _declaraciones(contexto)
    return claves


__all__ = ["antes", "mensaje_usuario", "sobre_sql", "sobre_filas", "nota_final",
           "instruccion_redactor", "asegurar_declaracion", "EntidadNoResuelta",
           "COLUMNA_EDAD"]
