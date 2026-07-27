"""comun/perdidos.py — Códigos centinela que el diccionario del censo NO declara.

El defecto que corrige: en el Censo 2011 la variable `Años_estudio` usa 88 como
"no relevado", pero su entrada del diccionario tiene las etiquetas de valor
VACÍAS. La regla del prompt dice "excluí los códigos anotados NR/NC/IG/SD/SE", y
como no hay nada anotado el modelo no tiene manera de saber que 88 es un perdido:
lo cuenta como si fueran 88 años de estudio.

El efecto medido: el promedio de años de estudio de Montevideo pasa de 8,60 años
(correcto) a 13,75 (contaminado). Un error del 60 % en una cifra que el INE usó
para evaluar el sistema.

No es un caso aislado: son 11 variables del Censo 2011 (habitaciones, autos,
computadoras, códigos de país). Declararlas acá, en un lugar único y auditable,
es lo que impide que el mismo agujero se repita variable por variable.

Verificado contra la base: cada uno de estos códigos aparece como valor máximo de
su columna y no corresponde a una categoría válida.
"""

# censo -> {variable: (codigos_perdidos, motivo)}
CENTINELAS = {
    "2011": {
        "Años_estudio": ((88,), "no relevado"),
        "HOGHD00": ((88,), "no relevado"),
        "HOGHD01": ((88,), "no relevado"),
        "HOGCE06": ((88,), "no relevado"),
        "HOGCE09": ((88,), "no relevado"),
        "HOGCE10": ((88,), "no relevado"),
        "HOGCE12": ((88,), "no relevado"),
        "HOGCE13": ((88,), "no relevado"),
        "PERMI01_4": ((9999,), "país no declarado"),
        "PERMI06_4": ((9999,), "país no declarado"),
        "PERMI07_4": ((9999,), "país no declarado"),
    },
    # 1996 y 2004 declaran sus perdidos en el propio esquema generado
    # (`PERDIDOS: 99=NS/NC`), y 2023 los trae en el bloque `perdidos` del
    # diccionario. Si aparece alguno sin declarar, va acá.
    "1996": {},
    "2004": {},
    "2023": {},
}


def de(censo, variable):
    """Códigos perdidos no declarados de una variable, o tupla vacía."""
    return CENTINELAS.get(censo, {}).get(variable, ((), ""))[0]


def variables(censo):
    return sorted(CENTINELAS.get(censo, {}))


def bloque_para_prompt(censo):
    """Bloque de texto para el prompt de SQL: obligación explícita de excluirlos."""
    tabla = CENTINELAS.get(censo, {})
    if not tabla:
        return ""
    lineas = ["CÓDIGOS CENTINELA NO DECLARADOS EN EL DICCIONARIO (obligatorio excluirlos "
              "de conteos, promedios, totales y denominadores; NO son valores válidos):"]
    for var in sorted(tabla):
        codigos, motivo = tabla[var]
        lineas.append("- %s: %s = %s -> agregá SIEMPRE %s NOT IN (%s)"
                      % (var, ", ".join(str(c) for c in codigos), motivo,
                         _cita(var), ", ".join(str(c) for c in codigos)))
    return "\n".join(lineas)


def _cita(variable):
    """Las variables con nombre no ASCII van entre comillas dobles en SQLite."""
    return '"%s"' % variable if not variable.isascii() else variable


def leyenda_para_redactor(censo, sql):
    """Recuerda al redactor qué centinelas se excluyeron en ESTA consulta."""
    tabla = CENTINELAS.get(censo, {})
    sql_low = (sql or "").lower()
    presentes = [v for v in tabla if v.lower() in sql_low]
    if not presentes:
        return ""
    return ("Se excluyeron los códigos de no respuesta de %s; el universo son los "
            "casos con dato válido, decilo al narrar."
            % ", ".join(sorted(presentes)))
