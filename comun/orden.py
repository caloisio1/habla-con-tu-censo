"""comun/orden.py — Vuelve determinista el orden de las filas devueltas.

EL PROBLEMA. Un `ORDER BY ... LIMIT` cuyo criterio de orden tiene EMPATES no
define qué filas salen. Si diez segmentos tienen 2.491 personas y el LIMIT corta
en el 300, cuál de esos diez entra al mapa y cuál queda afuera lo decide el plan
de ejecución, no el dato. La misma pregunta puede dar dos mapas distintos sin que
haya cambiado nada en la base.

No es un problema de un motor: SQLite tiene exactamente la misma propiedad que
DuckDB. Lo que pasa es que mientras hubo un solo motor nunca se hizo visible.
Medido sobre el Censo 2004: en el corte del LIMIT 300 por segmento hay una fila
empatada, así que el caso no es teórico.

LA SOLUCIÓN. Se agregan al ORDER BY, como criterios finales, TODAS las columnas
de la proyección, por posición (`ORDER BY ..., 1, 2, 3`). El argumento de por qué
alcanza: si dos filas empatan en todas las columnas de salida, son la misma fila
a los efectos de la respuesta —el redactor y el mapa leen esas columnas y nada
más—, así que da igual en qué orden salgan. Cualquier otro empate queda roto.

Se ordena por POSICIÓN y no por alias porque no todas las proyecciones tienen
alias, y la posición significa lo mismo en los dos motores.

CUANDO NO HABÍA ORDER BY. Ahí es peor: un LIMIT sin orden devuelve un subconjunto
arbitrario. Se agrega igual, y el resultado pasa de arbitrario a reproducible.

QUÉ NO CAMBIA. Si el resultado tiene menos filas que el LIMIT, el conjunto
devuelto es exactamente el mismo de antes: solo cambia el orden dentro de los
empates, que por definición son indistinguibles. Ninguna cifra se mueve.
"""
from sqlglot import exp


def desempatar(arbol):
    """Agrega al ORDER BY las columnas de la proyección que falten, por posición.

    Devuelve el árbol modificado. Si la proyección tiene `*` no se toca: no se
    puede saber cuántas columnas son, y forzar un orden a ciegas sería peor que
    el empate."""
    proyeccion = arbol.expressions if isinstance(arbol, exp.Select) else None
    if not proyeccion:
        return arbol
    if any(isinstance(p, (exp.Star, exp.Column)) and _es_estrella(p) for p in proyeccion):
        return arbol

    orden = arbol.args.get("order")
    ya = _posiciones_ya_ordenadas(orden)
    faltan = [i for i in range(1, len(proyeccion) + 1) if i not in ya]
    if not faltan:
        return arbol

    nuevos = [_ascendente(i) for i in faltan]
    if orden is None:
        return arbol.order_by(*nuevos, copy=False)
    orden.set("expressions", list(orden.expressions) + nuevos)
    return arbol


def _ascendente(i):
    """Criterio ascendente por la posición i, SIN cláusula de nulos.

    `nulls_first=True` no escribe NULLS FIRST en el SQL: le dice a sqlglot que
    eso YA es el comportamiento por defecto y que no hace falta escribir nada.
    Es justo lo que se quiere. Si se construye el nodo sin ese argumento,
    sqlglot escribe `NULLS LAST` explícito y **invierte la semántica de SQLite**
    —donde el NULL es el valor más chico y va primero en ASC—, que es el mismo
    invariante que los canarios de comun/ejecutor.py existen para proteger.
    """
    return exp.Ordered(this=exp.Literal.number(i), nulls_first=True)


def _es_estrella(p):
    return isinstance(p, exp.Star) or (isinstance(p, exp.Column)
                                       and isinstance(p.this, exp.Star))


def _posiciones_ya_ordenadas(orden):
    """Posiciones (1-based) que el ORDER BY ya usa, si ordena por número.

    Solo se detectan las numéricas. Si el ORDER BY ordena por alias o por
    expresión, la posición se agrega igual: un criterio repetido no cambia el
    resultado, y de más vale que de menos."""
    if orden is None:
        return set()
    posiciones = set()
    for o in orden.expressions:
        blanco = o.this
        if isinstance(blanco, exp.Literal) and blanco.is_int:
            posiciones.add(int(blanco.name))
    return posiciones
