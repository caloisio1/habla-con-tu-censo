"""comun/codigos.py — Traducción de códigos a etiquetas ANTES de narrar.

El motor ya sabe qué significa cada código: arma la leyenda de codificaciones y se
la pasa al redactor para que narre "de uso temporal" en vez de "3". Con gpt-5.5
alcanzaba. Con Opus 5 no: teniendo la leyenda delante narra el número pelado y
además dice que no revisó el diccionario, una limitación que no tiene. Tres
intentos de apretar el prompt lo bajaron de siempre a una de cada tres veces, que
para una respuesta que se publica sigue siendo demasiado.

Así que se deja de pedir y se hace: los códigos se reemplazan por su etiqueta en
las filas, antes de que el redactor las vea. Si no hay códigos pelados en los
datos, no hay forma de narrarlos mal — y la tabla que ve el usuario mejora igual.

NO se traducen las columnas geográficas: el front necesita geo_codigo crudo para
pintar el mapa, y traducirlo dejaría la respuesta sin mapa.
"""
import re

# Columnas que NUNCA se traducen: las geográficas las consume el mapa, y los
# conteos son cifras, no códigos.
NO_TRADUCIR = frozenset({
    "geo_codigo", "geo_nombre", "codigo", "codloc", "codbarrio", "codsec", "codloc_2023",
    "n_crudo", "n", "conteo_0", "conteo_1", "personas", "hogares", "viviendas",
    "dpto", "secc", "segm", "loc", "barrio", "ccz", "departamento", "localidad",
})

# Un código y su etiqueta dentro del campo de códigos del esquema. Se corta en ';'
# o en ',' SOLO cuando lo que sigue es otro "número=", porque hay etiquetas con coma
# adentro ("1=Patrón, con obreros o empleados a su cargo").
_SEPARADOR = re.compile(r"[;,](?=\s*(?:PERDIDOS\s*:\s*)?\d+\s*=)")
_PAR = re.compile(r"^\s*(?:PERDIDOS\s*:\s*)?(\d+)\s*=\s*(.+?)\s*$")


def mapa_desde_lineas(lineas):
    """{variable: {código: etiqueta}} a partir de las líneas del esquema generado.

    Formato de cada línea: "- NOMBRE | etiqueta | 1=Uno; 2=Dos; PERDIDOS: 9=Ignorado".
    Es el formato de 1996, 2004 y 2023.
    """
    mapa = {}
    for linea in lineas:
        partes = linea.lstrip("- ").split("|")
        if len(partes) < 3:
            continue
        nombre = partes[0].strip().lower()
        codigos = {}
        for chunk in _SEPARADOR.split(partes[2]):
            m = _PAR.match(chunk)
            if m:
                codigos[m.group(1)] = m.group(2)
        if nombre and codigos:
            mapa[nombre] = codigos
    return mapa


def mapa_desde_variables(variables):
    """Igual, pero desde el diccionario de 2011 (nombre + value_labels)."""
    mapa = {}
    for v in variables:
        nombre = (v.get("nombre") or "").strip().lower()
        etiquetas = v.get("value_labels") or {}
        if isinstance(etiquetas, str):
            continue
        codigos = {str(k).strip(): str(val) for k, val in etiquetas.items() if str(val).strip()}
        if nombre and codigos:
            mapa[nombre] = codigos
    return mapa


def _etiqueta(codigos, valor):
    """Etiqueta del valor, probando las formas en que puede venir el código.

    La misma variable puede llegar como 3, '3' o '03' según el censo y según cómo
    la escribió el modelo en el SELECT. Si no hay etiqueta para ese valor, se
    devuelve None y el valor queda intacto: mejor un código pelado que un dato
    inventado.
    """
    if valor is None or isinstance(valor, bool):
        return None
    formas = [str(valor).strip()]
    if isinstance(valor, float) and valor.is_integer():
        formas.append(str(int(valor)))
    formas.append(formas[0].lstrip("0") or "0")
    for f in formas:
        if f in codigos:
            return codigos[f]
    return None


def etiquetar(filas, mapa, no_traducir=NO_TRADUCIR):
    """Devuelve las filas con los códigos reemplazados por su etiqueta.

    Función pura: no modifica las filas recibidas. Una columna se traduce solo si
    su nombre está en el mapa y no está en la lista de intocables.
    """
    if not filas or not mapa:
        return filas
    salida = []
    for fila in filas:
        nueva = {}
        for col, valor in fila.items():
            clave = col.strip().lower()
            codigos = mapa.get(clave) if clave not in no_traducir else None
            etiqueta = _etiqueta(codigos, valor) if codigos else None
            nueva[col] = etiqueta if etiqueta is not None else valor
        salida.append(nueva)
    return salida
