"""comun/supresion.py — Control de divulgación, único para los cuatro censos.

Corrige el defecto que producía el falso "confidencialidad" reportado por el INE.

La regla del INE es que no se publica ninguna celda con MENOS DE 5 casos. El
código aplicaba `n < 5`, que también atrapa el **cero**. Y el cero no es una celda
chica: es el conjunto vacío. Aparece cada vez que un nombre no se resuelve —una
consulta agregada sin GROUP BY sobre un filtro que no matchea nada devuelve UNA
fila con COUNT(*) = 0—, y entonces el sistema respondía "suprimido por
confidencialidad" cuando lo que había pasado es que no encontró la localidad.

La regla correcta es `1 <= n < 5`. Un cero no revela a nadie: revela que no hay
nadie, que es justamente lo que hay que poder decir.

La supresión se aplica SIEMPRE sobre el conteo CRUDO (sin ponderar), también en
2023, donde la cifra publicada es SUM(W) pero el resguardo se decide con COUNT(*).
"""
UMBRAL = 5


def clasificar(conteos):
    """'publicable' | 'suprimir' | 'vacio' para una celda, según sus conteos crudos."""
    validos = [c for c in conteos
               if isinstance(c, int) and not isinstance(c, bool)]
    if not validos:
        return "publicable"      # sin columna de conteo no hay nada que resguardar
    minimo = min(validos)
    if minimo == 0:
        return "vacio"           # no hay casos: NO es un secreto estadístico
    return "suprimir" if minimo < UMBRAL else "publicable"


def suprimir_celdas_chicas(filas, columnas_conteo):
    """Descarta las filas con conteo crudo entre 1 y 4.

    Devuelve (filas_publicables, celdas_suprimidas, celdas_vacias). Las dos
    últimas se cuentan por separado a propósito: solo `celdas_suprimidas` puede
    dar lugar a un mensaje de confidencialidad. `celdas_vacias` significa que la
    consulta no encontró casos, y eso se le dice al usuario tal cual.
    """
    claves = {c.lower() for c in columnas_conteo}
    seguras, suprimidas, vacias = [], 0, 0
    for fila in filas:
        conteos = [v for k, v in fila.items() if k.lower() in claves]
        estado = clasificar(conteos)
        if estado == "suprimir":
            suprimidas += 1
        elif estado == "vacio":
            vacias += 1
        else:
            seguras.append(fila)
    return seguras, suprimidas, vacias
