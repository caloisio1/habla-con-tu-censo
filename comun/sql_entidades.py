"""comun/sql_entidades.py — Post-paso de entidades sobre el SQL generado.

Es el punto donde el resolver se conecta con los cuatro motores. Trabaja sobre el
SQL YA generado (no sobre la pregunta) porque ahí el nombre que hay que resolver
está aislado y sin ambigüedad sintáctica: es el literal de una comparación contra
una columna de nombre del nomenclátor.

Hace dos cosas:

1. RESOLUCIÓN DE ENTIDADES. Toma cada literal comparado contra una columna de
   nombre, lo pasa por el resolver compartido y:
     - único y distinto de lo escrito -> reescribe el literal al nombre canónico
       y devuelve la interpretación para que la respuesta la declare;
     - ambiguo o fragmentado -> NO ejecuta, devuelve las opciones;
     - no encontrado -> NO ejecuta, devuelve sugerencias.
   Antes, un nombre mal escrito llegaba a la base, no matcheaba nada, devolvía
   una fila con conteo 0 y salía rotulado como "suprimido por confidencialidad".

2. NOMENCLÁTOR CRUZADO DE 1996. `cod_localidades_1963_2023` es una tabla de
   correspondencias con la clave (dpto, cod_1996) REPETIDA: unirla tal cual
   multiplica los registros de personas (el total del país pasa de 3.163.763 a
   16.321.955). Acá se sustituye por su forma deduplicada, que es la única
   correcta, y se deja constancia en el SQL que se le muestra al usuario.
"""
import sqlglot
from sqlglot import exp

from comun import nomenclator as nom
from comun import rechazos
from comun.resolver import (AMBIGUO, FRAGMENTADO, NO_ENCONTRADO, OTRO_CENSO, UNICO,
                            colision_entre_tipos, opciones as opciones_de, resolver,
                            tipo_declarado)

# ── qué columna, de qué tabla, contiene el nombre de qué tipo de entidad ──
# Solo estas combinaciones se tocan. Cualquier otro literal del SQL (países,
# códigos, categorías) se deja intacto: el post-paso nunca adivina.
COLUMNAS_NOMBRE = {
    "2023": {
        ("localidades_2023", "nombre"): nom.LOCALIDAD,
        ("departamentos_2023", "nombre"): nom.DEPARTAMENTO,
        ("barrios_mvd_2023", "nombre"): nom.BARRIO,
        # MUNICIPIO_136 no es un código: guarda el NOMBRE del municipio en la
        # propia tabla de personas, igual que BARRIO85. Sin esta línea el literal
        # no pasaba por el resolver y llegaba crudo a la base: 'B' (la letra con
        # la que se nombra a los municipios de Montevideo) no matcheaba nada y la
        # consulta salía vacía, que es el fallo que este post-paso existe para
        # evitar. La traducción letra -> 'MUNICIPIO B' está en comun/sinonimos.py.
        ("personas_2023", "municipio_136"): nom.MUNICIPIO,
    },
    "2011": {
        ("localidades", "nombre"): nom.LOCALIDAD,
        ("personas", "departamento"): nom.DEPARTAMENTO,
        ("personas", "barrio85"): nom.BARRIO,
    },
    "1996": {
        ("cod_departamentos", "nombre"): nom.DEPARTAMENTO,
        ("cod_barrios_mvd", "nombre"): nom.BARRIO,
        (None, "nom_1996"): nom.LOCALIDAD,
    },
    "2004": {
        ("cod_departamentos", "nombre"): nom.DEPARTAMENTO,
        ("cod_barrios_mvd", "nombre"): nom.BARRIO,
        ("censo2004", "nom_loc"): nom.LOCALIDAD,
        ("censo2004", "nom_dpto"): nom.DEPARTAMENTO,
        ("censo2004", "nom_barrio"): nom.BARRIO,
    },
}

# Columnas que llevan el CÓDIGO de una entidad, no su nombre. El modelo a veces
# filtra por código —"WHERE dpto = '15'" en vez de "WHERE nom_dpto = 'SALTO'"—, y
# entonces no hay ningún literal de nombre que resolver. El código es inequívoco,
# así que no se reescribe nada; pero la entidad sí hay que reconocerla, para poder
# declarar cuál se leyó y ofrecer la otra lectura cuando el nombre colisiona
# (Salto departamento contra Salto ciudad).
COLUMNAS_CODIGO = {
    "2023": {("personas_2023", "departamento"): nom.DEPARTAMENTO,
             ("viviendas_2023", "departamento"): nom.DEPARTAMENTO,
             ("departamentos_2023", "codigo"): nom.DEPARTAMENTO,
             ("localidades_2023", "codloc"): nom.LOCALIDAD},
    "2011": {("localidades", "codloc"): nom.LOCALIDAD,
             ("personas", "codloc"): nom.LOCALIDAD},
    "2004": {("censo2004", "dpto"): nom.DEPARTAMENTO,
             ("cod_departamentos", "dpto"): nom.DEPARTAMENTO},
    "1996": {("personas_1996", "dpto"): nom.DEPARTAMENTO,
             ("viviendas_1996", "dpto"): nom.DEPARTAMENTO,
             ("cod_departamentos", "dpto"): nom.DEPARTAMENTO},
}


def _por_codigo(literal, tipo, censo):
    """Entidad de ese tipo cuyo código es el literal, o None.

    Compara sin ceros a la izquierda: el mismo departamento aparece como '15' o
    como '015' según la tabla.
    """
    clave = str(literal).strip().lstrip("0") or "0"
    for e in nom.catalogo(censo, tipo):
        if str(e.codigo).strip().lstrip("0") == clave:
            return e
    return None


# Tablas de nomenclátor de localidades: si el alias apunta a una de ellas y la
# columna se llama `nombre`, el tipo es localidad.
TABLAS_LOCALIDAD = {"2023": {"localidades_2023"}, "2011": {"localidades"},
                    "2004": {"ref_localidades_2004"}, "1996": {nom.TABLA_CRUZADA}}


class _Res:
    """Envoltorio mínimo para reusar `opciones()` con una lista de candidatos."""

    def __init__(self, candidatos):
        self.candidatos = candidatos


class EntidadNoResuelta(Exception):
    """El SQL no se ejecuta: hay que preguntarle algo al usuario."""

    def __init__(self, rechazo):
        super().__init__(rechazo.mensaje)
        self.rechazo = rechazo


def _alias_a_tabla(arbol):
    """{alias o nombre -> nombre real de tabla} para resolver `l.nombre`."""
    mapa = {}
    for t in arbol.find_all(exp.Table):
        real = (t.name or "").lower()
        mapa[real] = real
        if t.alias:
            mapa[t.alias.lower()] = real
    return mapa


def _tipo_de(columna, tabla_real, censo):
    col = (columna or "").lower()
    tabla = (tabla_real or "").lower()
    mapa = COLUMNAS_NOMBRE.get(censo, {})
    if (tabla, col) in mapa:
        return mapa[(tabla, col)]
    if (None, col) in mapa:
        return mapa[(None, col)]
    if col == "nombre" and tabla in TABLAS_LOCALIDAD.get(censo, set()):
        return nom.LOCALIDAD
    return None


def _literales(arbol, censo, mapa=None, por_defecto=True):
    """[(nodo_literal, tipo_de_entidad)] de las comparaciones que hay que mirar.

    `mapa` permite reusar el recorrido para las columnas de CÓDIGO en vez de las
    de nombre; sin él se usan las de nombre.
    """
    mapa = COLUMNAS_NOMBRE.get(censo, {}) if mapa is None else mapa
    alias = _alias_a_tabla(arbol)
    salida = []
    for cmp_ in list(arbol.find_all(exp.EQ)) + list(arbol.find_all(exp.In)):
        col = cmp_.this if isinstance(cmp_.this, exp.Column) else None
        if col is None:
            continue
        tabla_real = alias.get((col.table or "").lower(), col.table or "")
        if not col.table:
            # sin calificar: se acepta solo si el nombre de columna es inequívoco
            candidatos = {t for (tb, c), t in mapa.items() if c == col.name.lower()}
            tipo = candidatos.pop() if len(candidatos) == 1 else None
        else:
            tipo = (mapa.get(((tabla_real or "").lower(), col.name.lower()))
                    or (_tipo_de(col.name, tabla_real, censo) if por_defecto else None))
        if tipo is None:
            continue
        valores = ([cmp_.expression] if isinstance(cmp_, exp.EQ)
                   else list(cmp_.expressions))
        for v in valores:
            if isinstance(v, exp.Literal) and v.is_string:
                salida.append((v, tipo))
    return salida


def resolver_en_sql(sql, censo, pregunta=None):
    """Resuelve las entidades nombradas del SQL.

    Devuelve (sql_corregido, interpretaciones, alternativas). Lanza EntidadNoResuelta cuando hay
    que preguntar en vez de ejecutar. `pregunta` es el texto original: sirve para
    saber si el usuario ya dijo de qué tipo de entidad habla ("el departamento de
    Maldonado") y no volver a preguntárselo.
    """
    declarado = tipo_declarado(pregunta)
    try:
        arboles = [a for a in sqlglot.parse(sql, read="sqlite") if a is not None]
    except Exception:
        # Tres valores, como el return normal: el que espera sobre_sql(). Estas dos
        # salidas tempranas quedaron en dos cuando se agregaron las alternativas, y
        # reventaban con ValueError —un 500— en cuanto un SQL no traía entidades que
        # resolver, que es el caso de cualquier consulta sin nombre propio.
        return sql, [], []      # el guard se encarga del SQL que no parsea
    if not arboles:
        return sql, [], []

    interpretaciones, alternativas, cambiado = [], [], False
    for arbol in arboles:
        for literal, tipo in _literales(arbol, censo):
            escrito = literal.this
            r = resolver(escrito, tipo, censo)

            if r.estado == UNICO:
                # El SQL fija el tipo (comparó contra la columna de departamentos,
                # por ejemplo), así que dentro de ese tipo el nombre es único. Pero
                # si el mismo nombre existe TAMBIÉN como otro tipo y la pregunta no
                # aclaró cuál, la elección la hizo el modelo, no el usuario.
                #
                # No se bloquea la respuesta: se contesta la lectura elegida, se
                # DECLARA cuál es, y se ofrece la otra como alternativa con su cifra.
                # Bloquear sería más fiel a la letra pero obligaría a un clic en las
                # preguntas geográficas más comunes (Salto, Montevideo, Canelones);
                # declarar y ofrecer cumple lo importante —que la elección no sea
                # silenciosa— sin trabar la consulta.
                if declarado is None and r.entidad is not None:
                    for otra in colision_entre_tipos(escrito, censo):
                        if otra.tipo == r.entidad.tipo:
                            continue
                        alternativas.append(otra)
                    if alternativas:
                        interpretaciones.append(_frase_tipo(r.entidad))
                if r.interpretacion:
                    interpretaciones.append(r.interpretacion)
                if r.entidad and r.entidad.nombre != escrito:
                    literal.set("this", r.entidad.nombre)
                    cambiado = True
                continue

            if r.estado in (AMBIGUO, FRAGMENTADO):
                ops = opciones_de(r, censo)
                raise EntidadNoResuelta(
                    rechazos.fragmentada(escrito, ops) if r.estado == FRAGMENTADO
                    else rechazos.ambigua(escrito, ops, tipo))

            if r.estado == OTRO_CENSO:
                raise EntidadNoResuelta(rechazos.en_otro_censo(
                    escrito, censo, r.censos_alternativos, r.sugerencias, tipo))

            raise EntidadNoResuelta(rechazos.no_encontrada(escrito, r.sugerencias, tipo))

        # Entidades referidas por CÓDIGO: no se reescribe nada (el código es
        # inequívoco), pero si el nombre colisiona entre tipos hay que declarar
        # cuál se leyó y ofrecer la otra, igual que con los nombres.
        if declarado is None:
            for literal, tipo in _literales(arbol, censo, COLUMNAS_CODIGO.get(censo, {}),
                                            por_defecto=False):
                entidad = _por_codigo(literal.this, tipo, censo)
                if entidad is None:
                    continue
                choque = [o for o in colision_entre_tipos(entidad.nombre, censo)
                          if o.tipo != entidad.tipo]
                if choque:
                    interpretaciones.append(_frase_tipo(entidad))
                    alternativas.extend(choque)

    salida = sql if not cambiado else " ".join(a.sql(dialect="sqlite") for a in arboles)
    return salida, interpretaciones, _chips(alternativas, censo)


_ETIQUETA_TIPO = {nom.LOCALIDAD: "ciudad o localidad", nom.DEPARTAMENTO: "departamento",
                  nom.BARRIO: "barrio de Montevideo", nom.CCZ: "centro comunal zonal",
                  nom.MUNICIPIO: "municipio"}


def _frase_tipo(entidad):
    """Cómo se declara la lectura elegida: '<Nombre> (departamento)'."""
    from comun.resolver import _titulo
    return "%s (%s)" % (_titulo(entidad.nombre), _ETIQUETA_TIPO.get(entidad.tipo, entidad.tipo))


def _chips(alternativas, censo):
    """Las otras lecturas posibles, con su cifra, listas para el frontend.

    La cifra respeta la supresión: si la entidad tiene menos de 5 registros crudos,
    el chip va sin número (ver nomenclator.poblacion).
    """
    from comun.resolver import _titulo
    salida = []
    for e in _sin_repetir(alternativas):
        etiqueta = _ETIQUETA_TIPO.get(e.tipo, e.tipo)
        personas = nom.poblacion(e)
        texto = "¿Querías %s %s?" % (rechazos.articulo(etiqueta, definido=True),
                                     "%s de %s" % (etiqueta, _titulo(e.nombre)))
        salida.append({"texto": texto,
                       "detalle": ("%s personas" % _miles(personas)) if personas else "",
                       "tipo": e.tipo, "codigo": e.codigo, "censo": censo,
                       "pregunta": "¿Cuántas personas hay en %s, %s?"
                                   % (_titulo(e.nombre), etiqueta)})
    return salida


def _sin_repetir(entidades):
    vistos, salida = set(), []
    for e in entidades:
        if (e.tipo, e.codigo) not in vistos:
            vistos.add((e.tipo, e.codigo))
            salida.append(e)
    return salida


def _miles(n):
    return "{:,}".format(int(n)).replace(",", ".")


# ── nomenclátor cruzado de 1996: deduplicación obligatoria ───────────────
def deduplicar_nomenclator(sql, censo):
    """Sustituye `cod_localidades_1963_2023` por su forma deduplicada.

    Sin esto el JOIN multiplica las filas de personas hasta 46 veces. La
    sustitución es determinista y el SQL resultante es el que se le muestra al
    usuario, así que la corrección queda a la vista.
    """
    if nom.TABLA_CRUZADA not in (sql or "").lower():
        return sql, False
    try:
        arboles = [a for a in sqlglot.parse(sql, read="sqlite") if a is not None]
    except Exception:
        return sql, False

    reemplazado = False
    for arbol in arboles:
        for tabla in list(arbol.find_all(exp.Table)):
            if (tabla.name or "").lower() != nom.TABLA_CRUZADA:
                continue
            alias = tabla.alias or nom.TABLA_CRUZADA
            sub = sqlglot.parse_one(nom.SUBCONSULTA_LOCALIDADES_1996, read="sqlite")
            if isinstance(sub, exp.Subquery):
                sub = sub.this
            nuevo = exp.Subquery(this=sub, alias=exp.TableAlias(this=exp.to_identifier(alias)))
            padre = tabla.parent
            tabla.replace(nuevo)
            reemplazado = True
            # El nomenclátor no cubre el 100% de los códigos que aparecen en los
            # microdatos (hay 1.257 personas en un código de Colonia que no figura
            # en el cruzado del INE). Con INNER JOIN esas personas DESAPARECEN del
            # resultado sin aviso, que es la clase de pérdida silenciosa que este
            # trabajo justamente viene a eliminar. Con LEFT JOIN quedan visibles,
            # agrupadas bajo un nombre nulo.
            if isinstance(padre, exp.Join):
                padre.set("side", "LEFT")
                padre.set("kind", None)
    if not reemplazado:
        return sql, False
    return " ".join(a.sql(dialect="sqlite") for a in arboles), True


# Columnas del cruzado que el SQL puede seguir nombrando después de deduplicar.
# La subconsulta expone dpto, cod y nombre; si el modelo pidió `cod_1996` o
# `nom_1996`, se renombran para que el SQL siga siendo válido.
RENOMBRES_1996 = {"cod_1996": "cod", "nom_1996": "nombre"}


def _normalizar_columnas_1996(sql):
    """Renombra las columnas del cruzado a las que expone la subconsulta.

    Se aplica ANTES de sustituir la tabla: si se hiciera después, renombraría
    también las columnas de adentro de la subconsulta y la rompería.
    """
    try:
        arbol = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return sql
    tocado = False
    for col in arbol.find_all(exp.Column):
        nuevo = RENOMBRES_1996.get(col.name.lower())
        if nuevo:
            col.set("this", exp.to_identifier(nuevo))
            tocado = True
    return arbol.sql(dialect="sqlite") if tocado else sql


def preparar_1996(sql):
    """Punto de entrada único para el SQL de 1996: renombra y después deduplica.

    Devuelve (sql, se_corrigio). El orden no es negociable (ver _normalizar_columnas_1996).
    """
    if nom.TABLA_CRUZADA not in (sql or "").lower():
        return sql, False
    renombrado = _normalizar_columnas_1996(sql)
    nuevo, hubo = deduplicar_nomenclator(renombrado, "1996")
    return nuevo, hubo


def canonizar(sql):
    """Forma canónica de un SQL, para comparar dos consultas equivalentes.

    Hace falta porque el post-paso solo re-renderiza el SQL cuando reescribió algo:
    "Chamiso" (reescrito) y "Chamizo" (intacto) ejecutan exactamente lo mismo pero
    llegan con distinto formato. Sin canonizar, la caché los trataría como consultas
    distintas y la regla del encargo —clave por entidad resuelta, no por texto
    escrito— no se cumpliría.

    Si el SQL no parsea se devuelve tal cual: la caché degrada a fallo, nunca a un
    resultado equivocado.
    """
    try:
        arboles = [a for a in sqlglot.parse(sql, read="sqlite") if a is not None]
    except Exception:
        return sql
    if not arboles:
        return sql
    return " ".join(a.sql(dialect="sqlite") for a in arboles)
