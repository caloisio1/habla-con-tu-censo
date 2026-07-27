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
from comun import edad, indicadores, rechazos, supresion
from comun.sql_entidades import EntidadNoResuelta, preparar_1996, resolver_en_sql

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
    }
    return None, contexto


def mensaje_usuario(texto, contexto):
    """El mensaje `user` que recibe el generador de SQL.

    La pregunta va SIEMPRE al final: el prefijo del prompt tiene que quedar
    estable para que la API lo sirva desde el caché (99 % del prompt de 2023).
    """
    instr = (contexto or {}).get("instruccion_edad") or ""
    return (instr + "\n\n" + texto) if instr else texto


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


def sobre_filas(filas, columnas_conteo, unidad="personas"):
    """Supresión con la regla corregida.

    Devuelve (filas, suprimidas, vacias, rechazo). `rechazo` no es None cuando no
    queda nada publicable, y distingue los dos casos que antes se confundían:
    no hubo casos (vacío) o los hubo pero son menos de cinco (supresión).
    """
    filas, suprimidas, vacias = supresion.suprimir_celdas_chicas(filas, columnas_conteo)
    if filas:
        return filas, suprimidas, vacias, None
    if suprimidas:
        return filas, suprimidas, vacias, rechazos.supresion(suprimidas, unidad)
    if vacias:
        return filas, suprimidas, vacias, rechazos._r(
            "sin_casos",
            "La consulta se ejecutó correctamente pero no encontró ningún caso que "
            "cumpla esas condiciones en este censo. No es un problema de "
            "confidencialidad: sencillamente no hay registros.")
    return filas, suprimidas, vacias, rechazos._r(
        "sin_casos", "La consulta no devolvió resultados.")


def nota_final(interpretaciones, contexto):
    """Lo que la respuesta DEBE declarar: cómo se interpretó y qué se contó."""
    partes = []
    for i in dict.fromkeys(interpretaciones or []):
        partes.append("Se interpretó como %s." % i)
    for d in dict.fromkeys((contexto or {}).get("declaraciones_edad") or []):
        partes.append("Criterio: %s." % d)
    return " ".join(partes)


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
    claves += list(dict.fromkeys((contexto or {}).get("declaraciones_edad") or []))
    return claves


__all__ = ["antes", "mensaje_usuario", "sobre_sql", "sobre_filas", "nota_final",
           "instruccion_redactor", "asegurar_declaracion", "EntidadNoResuelta",
           "COLUMNA_EDAD"]
