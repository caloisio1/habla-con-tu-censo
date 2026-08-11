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

EL SEGUNDO TRABAJO: QUE EL ORDEN SEA EXPRESABLE EN LOS DOS MOTORES. Un
`ORDER BY` que menciona una columna cruda que no está en el GROUP BY es legal en
SQLite —devuelve un valor cualquiera del grupo— y DuckDB lo RECHAZA. Medido: es
el único caso que en 120 pasadas todavía obligaba a repetir la consulta en
SQLite, y aparecía en el cruce sexo × departamento de 1996 y 2004, donde el
modelo agrupaba bien (`GROUP BY t.dpto, d.nombre, 3`) pero ordenaba por la
columna cruda (`ORDER BY ..., t.sexo, ...`).

Se arregla acá y no en el prompt porque un prompt PIDE y un post-paso GARANTIZA:
la regla escrita ya había conseguido que el modelo agrupara por ordinal, y aun
así no la generalizó al ORDER BY. El término suelto se envuelve en `MIN(...)`,
que es válido en los dos motores.

POR QUÉ MIN NO MUEVE NINGUNA FILA. Donde SQLite devolvía "un valor cualquiera
del grupo" —sin definir cuál— ahora devuelve el mínimo. No se pierde un orden
que existiera: se reemplaza algo indefinido por algo definido. Y en el caso que
lo motivó la columna es función del propio grupo (se agrupa por
`CASE t.sexo ... END`, se ordena por `t.sexo`), así que dentro de cada grupo hay
un único valor y MIN es ese valor: el orden resultante es idéntico.
"""
from sqlglot import exp


def desempatar(arbol):
    """Agrega al ORDER BY las columnas de la proyección que falten, por posición.

    Antes de eso vuelve expresable el ORDER BY que ya venía escrito, envolviendo
    en MIN(...) lo que no esté agrupado. Ese paso se aplica a TODOS los SELECT
    del árbol —también a los de un CTE, donde el rechazo de DuckDB es igual de
    fatal—, mientras que el desempate por posición sigue siendo solo del SELECT
    externo, que es el que define las filas que se devuelven.

    Devuelve el árbol modificado. Si la proyección tiene `*` no se toca: no se
    puede saber cuántas columnas son, y forzar un orden a ciegas sería peor que
    el empate."""
    for select in arbol.find_all(exp.Select):
        _agregar_lo_no_agrupado(select)

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


def _agregar_lo_no_agrupado(select):
    """Envuelve en MIN(...) los términos del ORDER BY que el GROUP BY no cubre.

    Modifica el SELECT en su lugar. Sin GROUP BY no hay nada que hacer: ahí
    cualquier columna en el ORDER BY es válida en los dos motores.

    Ante la duda NO envuelve. Un término que se deja como estaba es exactamente
    el comportamiento de hoy —DuckDB lo rechaza y la consulta se repite en
    SQLite—, mientras que envolver de más sí podría cambiar un orden. El error
    barato es no tocar."""
    grupo = select.args.get("group")
    orden = select.args.get("order")
    if grupo is None or orden is None:
        return

    proyeccion = select.expressions
    # Dos formas de estar cubierto, y no son la misma. Un término del ORDER BY
    # vale si es IGUAL a una expresión agrupada, o si todas las columnas que
    # menciona son, ellas mismas, términos del GROUP BY -así `t.dpto` agrupada
    # habilita `CAST(t.dpto AS INTEGER)` en el orden-. Lo que NO habilita es lo
    # inverso: agrupar por `CASE t.sexo ... END` no autoriza `t.sexo` suelta, y
    # ese es justamente el caso que rompe.
    expresiones = set()
    columnas = set()
    for g in grupo.expressions:
        blanco = _sin_alias(_resolver_posicion(g, proyeccion))
        expresiones.add(_canonico(blanco))
        if isinstance(blanco, exp.Column):
            columnas.add(blanco.name.lower())

    for o in orden.expressions:
        blanco = o.this
        if isinstance(blanco, exp.Literal) and blanco.is_int:
            continue                                  # por posición: siempre válido
        if _canonico(blanco) in expresiones:
            continue
        objetivo = _resolver_alias(blanco, proyeccion)
        if _canonico(objetivo) in expresiones:
            continue
        sueltas = _columnas_fuera_de_agregado(objetivo)
        if all(c.name.lower() in columnas for c in sueltas):
            continue                                  # incluye el caso sin columnas: ya es agregado
        o.set("this", exp.Min(this=objetivo.copy()))


def _resolver_posicion(g, proyeccion):
    """`GROUP BY 3` significa la tercera columna de la proyección.

    Sin esto un GROUP BY escrito por ordinal parecería no agrupar nada y todo el
    ORDER BY terminaría envuelto en MIN."""
    if isinstance(g, exp.Literal) and g.is_int and 1 <= int(g.name) <= len(proyeccion):
        return proyeccion[int(g.name) - 1]
    return g


def _resolver_alias(blanco, proyeccion):
    """Cambia un alias de salida por la expresión que nombra.

    `ORDER BY n` sobre `COUNT(*) AS n` tiene que verse como el agregado que es;
    si no, se envolvería en MIN(...) un conteo que ya estaba perfecto."""
    if isinstance(blanco, exp.Column) and not blanco.table:
        nombre = blanco.name.lower()
        for p in proyeccion:
            if isinstance(p, exp.Alias) and p.alias and p.alias.lower() == nombre:
                return p.this
    return blanco


def _columnas_fuera_de_agregado(nodo):
    """Columnas que el motor exigiría agrupadas: las que no están bajo un agregado.

    Lo que hay dentro de un COUNT/SUM/MIN no cuenta -por eso se agrega-, y una
    función de ventana se declara cubierta para no envolverla: MIN(ROW_NUMBER()
    OVER ...) no es válido en ningún motor, y no tocar es el error barato."""
    if isinstance(nodo, (exp.AggFunc, exp.Window)):
        return []
    if isinstance(nodo, exp.Column):
        return [nodo]
    fuera = []
    for hijo in nodo.iter_expressions():
        fuera.extend(_columnas_fuera_de_agregado(hijo))
    return fuera


def _sin_alias(e):
    return e.this if isinstance(e, exp.Alias) else e


def _canonico(e):
    """Texto comparable de una expresión. Se compara el SQL y no el objeto
    porque dos nodos distintos con el mismo texto son la misma expresión para
    lo que acá importa."""
    return _sin_alias(e).sql(dialect="sqlite", comments=False)


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
