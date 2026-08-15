"""comun/resolver.py — Resolver de entidades nombradas, único y compartido.

Recibe (texto, tipo de entidad esperado, censo) y devuelve un código único, un
conjunto de candidatos para que el usuario elija, o sugerencias. Es DETERMINISTA:
no llama al modelo, no consulta la red, y con la misma entrada da siempre la
misma salida.

La lógica es la misma para los cuatro censos; lo único que cambia es el
nomenclátor contra el que compara (comun/nomenclator.py). Un fix que quedara en
un solo motor reproduciría exactamente la inconsistencia que reportó el INE.

Orden de resolución (de más estricto a más laxo; se corta en el primero que da
un candidato claramente superior):

  1. normalización + match exacto
  2. sinónimos declarados (comun/sinonimos.py)
  3. variantes de número (cifra <-> palabra)
  4. clave fonética del español
  5. distancia de edición
  6. si nada supera el umbral: las 3 a 5 entidades más parecidas como sugerencias

Nunca hay callejón sin salida y NUNCA se devuelve un error de confidencialidad:
la confidencialidad es una propiedad del RESULTADO (celdas con menos de 5 casos),
no de la resolución de un nombre.
"""
from collections import defaultdict, namedtuple

from comun import nomenclator as nom
from comun import sinonimos
from comun.texto import (fonetico, normalizar, normalizar_compat, similitud,
                         variantes_numero)

# ── estados posibles ─────────────────────────────────────────────────────
UNICO = "unico"                  # una sola entidad: se puede ejecutar el SQL
AMBIGUO = "ambiguo"              # varias entidades distintas: hay que preguntar
FRAGMENTADO = "fragmentado"      # un mismo lugar partido entre departamentos
NO_ENCONTRADO = "no_encontrado"  # no existe en este censo (con sugerencias)
OTRO_CENSO = "otro_censo"        # no está en este censo pero sí en otro

# interpretacion: la frase que la respuesta DEBE declarar cuando se resolvió por
# aproximación ("Chamizo, departamento de Florida"). None si fue match exacto.
Resultado = namedtuple(
    "Resultado", "estado entidad candidatos sugerencias interpretacion censos_alternativos")


def _r(estado, entidad=None, candidatos=(), sugerencias=(), interpretacion=None,
       censos_alternativos=()):
    return Resultado(estado, entidad, tuple(candidatos), tuple(sugerencias),
                     interpretacion, tuple(censos_alternativos))


# ── umbrales ─────────────────────────────────────────────────────────────
# Un candidato se acepta solo si es CLARAMENTE superior: supera el piso de
# similitud y le saca al segundo una diferencia mínima. Si dos candidatos están
# parejos, se pregunta; adivinar es lo que produce cifras equivocadas sin aviso.
UMBRAL_ACEPTACION = 0.82
VENTAJA_MINIMA = 0.06
UMBRAL_SUGERENCIA = 0.55
MAX_SUGERENCIAS = 5


def _indices(censo, tipos):
    """Índices del catálogo: por nombre normalizado, por clave de compatibilidad,
    por clave fonética y por variante de número. Se recalculan por llamada sobre
    la lista cacheada, que tiene unos pocos cientos de entradas.

    por_compat es el índice de RESPALDO (NFKD): pliega ancho completo, ligaduras
    y demás compatibilidades Unicode. Guarda listas, igual que los otros, para
    que una equivalencia de compatibilidad pueda ayudar a encontrar una entidad
    sin fusionar dos que el catálogo distingue: si quedan varias, _clasificar()
    devuelve ambigüedad en vez de elegir por su cuenta."""
    entidades = nom.catalogo(censo, tipos)
    por_norm, por_fon, por_num = defaultdict(list), defaultdict(list), defaultdict(list)
    por_compat = defaultdict(list)
    for e in entidades:
        n = normalizar(e.nombre)
        por_norm[n].append(e)
        # SIN condicionar a que difiera de n: lo que llega plegado es el texto
        # del USUARIO, y su clave compat tiene que encontrar la del catálogo
        # aunque para el catálogo ambas claves sean iguales ('MONTEVIDEO').
        por_compat[normalizar_compat(e.nombre)].append(e)
        por_fon[fonetico(e.nombre)].append(e)
        for v in variantes_numero(e.nombre):
            if v != n:
                por_num[v].append(e)
    return entidades, por_norm, por_compat, por_fon, por_num


def _clasificar(candidatos, interpretacion=None):
    """Con la lista de entidades que matchearon, decide el estado."""
    if len(candidatos) == 1:
        return _r(UNICO, entidad=candidatos[0], interpretacion=interpretacion)

    tipos = {e.tipo for e in candidatos}
    deptos = {e.departamento for e in candidatos}
    # FRAGMENTACIÓN: mismo tipo, mismo nombre, repartido entre departamentos. No
    # es homonimia: son partes de un mismo lugar y la cifra correcta puede ser la
    # suma o el desglose, pero nunca una sola parte sin avisar.
    if len(tipos) == 1 and nom.LOCALIDAD in tipos and len(deptos) == len(candidatos) > 1:
        return _r(FRAGMENTADO, candidatos=candidatos, interpretacion=interpretacion)
    return _r(AMBIGUO, candidatos=candidatos, interpretacion=interpretacion)


def _filtrar_por_calificador(candidatos, texto, censo):
    """Si la pregunta trae un calificador ("Santa Lucía, Canelones"), se usa para
    desambiguar en vez de preguntar."""
    t = normalizar(texto)
    deptos = {normalizar(e.nombre): e.nombre
              for e in nom.catalogo(censo, nom.DEPARTAMENTO)}
    mencionados = {nombre for norm, nombre in deptos.items()
                   if norm and norm in t}
    if not mencionados:
        return candidatos
    filtrados = [e for e in candidatos
                 if e.departamento and normalizar(e.departamento) in
                 {normalizar(m) for m in mencionados}]
    return filtrados or candidatos


def _sin_calificador(texto, censo):
    """Quita del texto el nombre de departamento usado como calificador, para que
    'Santa Lucía, Canelones' busque 'SANTA LUCIA' y no la frase entera."""
    t = normalizar(texto)
    for e in nom.catalogo(censo, nom.DEPARTAMENTO):
        d = normalizar(e.nombre)
        if d and d in t and t != d:
            t = t.replace(d, " ").strip()
    return " ".join(t.split())


def resolver(texto, tipo=None, censo="2023", variable=None):
    """Resuelve un nombre escrito por el usuario contra el nomenclátor del censo.

    `tipo` puede ser un tipo (nom.LOCALIDAD, nom.BARRIO, ...), una tupla de tipos
    o None para buscar en toda la geografía del censo. `variable` solo aplica a
    las etiquetas de valor: si la pregunta ya identifica la variable, acota la
    búsqueda a ella y desaparece la ambigüedad de las etiquetas repetidas
    ("Sí", "No", "Otros" aparecen en decenas de variables).
    """
    if texto is None or not str(texto).strip():
        return _r(NO_ENCONTRADO)

    if tipo == nom.ETIQUETA:
        return _resolver_etiqueta(str(texto), censo, variable)

    tipos = tipo if tipo else nom.TIPOS_GEO
    if isinstance(tipos, str):
        tipos = (tipos,)

    original = str(texto)

    # Municipio de Montevideo nombrado por su letra ("B", "CH"), que es como lo
    # nombra todo el mundo y como sale del modelo. Se traduce ANTES de buscar
    # porque 'B' no llega a 'MUNICIPIO B' por ninguna de las vías de abajo: no es
    # una variante ortográfica ni fonética, es otra forma de nombrar. Solo se
    # aplica cuando el tipo ya está fijado en municipio, o sea cuando el SQL
    # comparó contra MUNICIPIO_136.
    if nom.MUNICIPIO in tipos:
        original = sinonimos.municipio(original) or original

    entidades, por_norm, por_compat, por_fon, por_num = _indices(censo, tipos)

    # 1. match exacto sobre lo normalizado
    consulta = normalizar(original)
    if consulta in por_norm:
        cands = _filtrar_por_calificador(por_norm[consulta], original, censo)
        return _clasificar(cands)

    # 1b. con calificador: "Santa Lucía, Canelones" -> buscar "SANTA LUCIA"
    nucleo = _sin_calificador(original, censo)
    if nucleo and nucleo != consulta and nucleo in por_norm:
        cands = _filtrar_por_calificador(por_norm[nucleo], original, censo)
        return _clasificar(cands, interpretacion=_frase(cands[0]) if len(cands) == 1 else None)

    # 2. sinónimos declarados
    sin_geo = sinonimos.geografico(consulta)
    if sin_geo and normalizar(sin_geo) in por_norm:
        cands = por_norm[normalizar(sin_geo)]
        return _clasificar(cands, interpretacion=_frase(cands[0]) if len(cands) == 1 else None)
    cap = _capital_mencionada(consulta, censo)
    if cap and normalizar(cap) in por_norm:
        cands = por_norm[normalizar(cap)]
        return _clasificar(cands, interpretacion=_frase(cands[0]) if len(cands) == 1 else None)

    # 3. variantes de número: "veinticinco de agosto" <-> "25 de agosto"
    for v in variantes_numero(original):
        if v in por_norm:
            cands = _filtrar_por_calificador(por_norm[v], original, censo)
            return _clasificar(cands, interpretacion=_frase(cands[0]) if len(cands) == 1 else None)
        if v in por_num:
            cands = _filtrar_por_calificador(por_num[v], original, censo)
            return _clasificar(cands, interpretacion=_frase(cands[0]) if len(cands) == 1 else None)

    # 3b. compatibilidad Unicode: ancho completo, ligaduras. Va DESPUÉS de las
    # equivalencias declaradas (sinónimos, números), que son curadas y deben
    # ganar, y ANTES de la fonética, que es mucho más laxa. Solo se consulta si
    # la clave tolerante difiere de la conservadora: si son iguales, el paso 1
    # ya falló y repetirlo no aporta nada.
    compat = normalizar_compat(original)
    if compat != consulta and compat in por_compat:
        cands = _filtrar_por_calificador(por_compat[compat], original, censo)
        return _clasificar(cands, interpretacion=_frase(cands[0]) if len(cands) == 1 else None)

    # 4. clave fonética
    clave = fonetico(original)
    if clave in por_fon:
        cands = _filtrar_por_calificador(por_fon[clave], original, censo)
        # Distintas entidades con la misma clave fonética pero nombres distintos
        # son ambigüedad real, no un acierto.
        if len({normalizar(e.nombre) for e in cands}) == 1:
            return _clasificar(cands, interpretacion=_frase(cands[0]) if len(cands) == 1 else None)
        return _clasificar(cands)

    # 5. distancia de edición
    puntuados = sorted(((similitud(consulta, normalizar(e.nombre)), e) for e in entidades),
                       key=lambda p: (-p[0], p[1].nombre))
    if puntuados:
        mejor, entidad = puntuados[0]
        segundo = puntuados[1][0] if len(puntuados) > 1 else 0.0
        empatados = [e for s, e in puntuados if s >= mejor - 1e-9]
        if mejor >= UMBRAL_ACEPTACION:
            if len(empatados) > 1:
                return _clasificar(empatados)
            if mejor - segundo >= VENTAJA_MINIMA:
                return _r(UNICO, entidad=entidad, interpretacion=_frase(entidad))
            # dos candidatos parejos: se pregunta, no se adivina
            return _clasificar([e for s, e in puntuados[:2]])

    # 6. no está en este censo: ¿está en otro?
    otros = [c for c in nom.censos_con(consulta, tipos) if c != censo]
    sugerencias = _dedup([e for s, e in puntuados if s >= UMBRAL_SUGERENCIA][:MAX_SUGERENCIAS]) \
        or _dedup([e for _s, e in puntuados[:6]])[:3]
    if otros:
        return _r(OTRO_CENSO, sugerencias=sugerencias, censos_alternativos=otros)
    return _r(NO_ENCONTRADO, sugerencias=sugerencias)


# ── etiquetas de valor ───────────────────────────────────────────────────
# Las etiquetas no son nombres cortos sino frases ("Unión libre con pareja de otro
# sexo"), así que la comparación de cadena completa no sirve: lo que la gente
# escribe es un FRAGMENTO de la etiqueta. Por eso acá se resuelve por contención
# de palabras, y la distancia de edición queda como último recurso.
def _resolver_etiqueta(texto, censo, variable=None):
    entidades = [e for e in nom.catalogo(censo, nom.ETIQUETA)
                 if variable is None or e.variable == variable]
    if not entidades:
        return _r(NO_ENCONTRADO)

    consulta = normalizar(texto)
    buscados = [consulta]
    # sinónimo declarado; si el texto viene con errata, se busca el sinónimo por
    # clave fonética antes de darse por vencido ("afrodesendiente" -> "afro").
    sin = sinonimos.concepto(texto)
    if sin is None:
        clave = fonetico(texto)
        for k, v in sinonimos.CONCEPTOS.items():
            if fonetico(k) == clave:
                sin = v
                break
    if sin:
        buscados.append(normalizar(sin))

    # 1. etiqueta idéntica
    for b in buscados:
        exactas = [e for e in entidades if normalizar(e.nombre) == b]
        if exactas:
            return _clasificar_etiqueta(exactas, texto)

    # 2. contención de palabras: la consulta es una subsecuencia de la etiqueta
    for b in buscados:
        contenidas = [e for e in entidades if _contiene(normalizar(e.nombre), b)]
        if contenidas:
            return _clasificar_etiqueta(contenidas, texto,
                                        interpretacion=None if b == consulta else sin)

    # 3. contención fonética (absorbe la errata)
    clave = fonetico(texto)
    foneticas = [e for e in entidades if _contiene(fonetico(e.nombre), clave)]
    if foneticas:
        return _clasificar_etiqueta(foneticas, texto, interpretacion=sin)

    # 4. distancia de edición sobre la etiqueta completa
    puntuados = sorted(((similitud(consulta, normalizar(e.nombre)), e) for e in entidades),
                       key=lambda p: (-p[0], p[1].nombre))
    if puntuados and puntuados[0][0] >= UMBRAL_ACEPTACION:
        empatados = [e for s, e in puntuados if s >= puntuados[0][0] - 1e-9]
        return _clasificar_etiqueta(empatados, texto)

    sugerencias = _dedup([e for s, e in puntuados if s >= UMBRAL_SUGERENCIA][:MAX_SUGERENCIAS]) \
        or _dedup([e for _s, e in puntuados[:6]])[:3]
    # ¿La categoría existe en OTRO censo? Se compara con los mismos términos
    # buscados (incluido el sinónimo) y por contención: "afro" no figura como
    # etiqueta literal en ningún censo, pero "Afro o Negra" sí en 2011 y 2023.
    otros = []
    for c in nom.CENSOS:
        if c == censo:
            continue
        etiquetas = [normalizar(e.nombre) for e in nom.catalogo(c, nom.ETIQUETA)]
        if any(_contiene(et, b) for et in etiquetas for b in buscados):
            otros.append(c)
    if otros:
        return _r(OTRO_CENSO, sugerencias=sugerencias, censos_alternativos=otros)
    return _r(NO_ENCONTRADO, sugerencias=sugerencias)


def _dedup(entidades):
    """Sugerencias sin repetir el mismo texto (varias variables comparten etiqueta)."""
    vistos, salida = set(), []
    for e in entidades:
        k = normalizar(e.nombre)
        if k not in vistos:
            vistos.add(k)
            salida.append(e)
    return tuple(salida)


def _contiene(etiqueta, consulta):
    """¿La consulta aparece en la etiqueta como secuencia de palabras completas?"""
    if not consulta:
        return False
    palabras_e, palabras_c = etiqueta.split(), consulta.split()
    n = len(palabras_c)
    return any(palabras_e[i:i + n] == palabras_c for i in range(len(palabras_e) - n + 1))


def _clasificar_etiqueta(candidatos, texto, interpretacion=None):
    """Varias etiquetas de la MISMA variable que contienen lo buscado no son un
    error: son las categorías que abarca el término (unión libre = con pareja de
    otro sexo y del mismo sexo). Se devuelven todas para que el SQL use el
    conjunto de códigos, declarándolo."""
    variables = {e.variable for e in candidatos}
    if len(candidatos) == 1:
        return _r(UNICO, entidad=candidatos[0], interpretacion=interpretacion)
    if len(variables) == 1:
        return _r(UNICO, entidad=candidatos[0], candidatos=candidatos,
                  interpretacion=interpretacion or _frase_etiquetas(candidatos))
    return _r(AMBIGUO, candidatos=candidatos, interpretacion=interpretacion)


def _frase_etiquetas(candidatos):
    nombres = [e.nombre for e in candidatos]
    return "%s (códigos %s de %s)" % (" y ".join(nombres),
                                      ", ".join(e.codigo for e in candidatos),
                                      candidatos[0].variable)


def _capital_mencionada(consulta, censo):
    """'la capital de Flores' -> 'TRINIDAD'."""
    if "CAPITAL" not in consulta:
        return None
    for e in nom.catalogo(censo, nom.DEPARTAMENTO):
        if normalizar(e.nombre) in consulta:
            return sinonimos.capital_de(e.nombre)
    return None


def _frase(entidad):
    """Cómo se declara una interpretación en la respuesta al usuario."""
    if entidad.tipo == nom.LOCALIDAD and entidad.departamento:
        return "%s, departamento de %s" % (_titulo(entidad.nombre), _titulo(entidad.departamento))
    if entidad.tipo == nom.BARRIO:
        return "%s, barrio de Montevideo" % _titulo(entidad.nombre)
    if entidad.tipo == nom.CCZ:
        return "%s de Montevideo" % entidad.nombre
    if entidad.tipo == nom.DEPARTAMENTO:
        return "departamento de %s" % _titulo(entidad.nombre)
    if entidad.tipo == nom.MUNICIPIO:
        # Los ocho de Montevideo ya se llaman "Municipio B": anteponerle otra vez
        # la palabra daría "municipio de Municipio B".
        nombre = _titulo(entidad.nombre)
        return nombre if nombre.upper().startswith("MUNICIPIO") else "municipio de %s" % nombre
    return _titulo(entidad.nombre)


_MINUSCULAS = {"DE", "DEL", "LA", "LAS", "EL", "LOS", "Y", "A"}


def _titulo(nombre):
    """'PASO DE LOS TOROS' -> 'Paso de los Toros'. Los nombres del nomenclátor
    están en mayúsculas sostenidas y así no se muestran a nadie."""
    palabras = str(nombre).split()
    salida = []
    for i, p in enumerate(palabras):
        limpia = p.strip(",.")
        if i > 0 and limpia.upper() in _MINUSCULAS:
            salida.append(p.lower())
        elif p.isupper() or p.islower():
            salida.append(p.capitalize())
        else:
            salida.append(p)   # ya venía en formato mixto (barrios de 2011)
    return " ".join(salida)


# ── presentación de las opciones (las "chips" del frontend) ───────────────
def opciones(resultado, censo):
    """Convierte candidatos en opciones listas para mostrar como chips."""
    fuera = []
    for e in resultado.candidatos:
        etiqueta = _titulo(e.nombre)
        detalle = []
        if e.tipo == nom.LOCALIDAD and e.departamento:
            detalle.append("localidad de %s" % _titulo(e.departamento))
        elif e.tipo == nom.DEPARTAMENTO:
            detalle.append("departamento")
        elif e.tipo == nom.BARRIO:
            detalle.append("barrio de Montevideo")
        elif e.tipo == nom.CCZ:
            detalle.append("centro comunal zonal")
        fuera.append({"texto": etiqueta, "detalle": ", ".join(detalle),
                      "tipo": e.tipo, "codigo": e.codigo, "censo": censo})
    return fuera


# ── colisión entre tipos ─────────────────────────────────────────────────
# Caso "Maldonado": el modelo ya eligió el tipo al escribir el SQL (comparó contra
# departamentos_2023.nombre), así que el resolver, acotado a ese tipo, lo encuentra
# único y no hay nada que preguntar. Pero el usuario escribió un nombre que es a la
# vez departamento y ciudad, y la diferencia es de 212.954 contra 100.985 personas.
# Esta función detecta esa colisión mirando TODOS los tipos, no solo el elegido.
def colision_entre_tipos(texto, censo):
    """Entidades de tipos DISTINTOS que comparten ese nombre. Vacío si no hay."""
    clave = normalizar(texto)
    coincidencias = [e for e in nom.catalogo(censo, nom.TIPOS_GEO)
                     if normalizar(e.nombre) == clave]
    if len({e.tipo for e in coincidencias}) > 1:
        return coincidencias
    return []


# Palabras con las que la pregunta indica ella misma de qué tipo habla. Si están,
# el usuario ya desambiguó y no hay que volver a preguntarle.
_PALABRAS_TIPO = {
    nom.DEPARTAMENTO: ("DEPARTAMENTO", "DEPARTAMENTOS", "DEPTO"),
    nom.LOCALIDAD: ("LOCALIDAD", "CIUDAD", "PUEBLO", "LOCALIDADES"),
    nom.BARRIO: ("BARRIO", "BARRIOS"),
    nom.CCZ: ("CCZ", "CENTRO COMUNAL"),
}


def tipo_declarado(pregunta):
    """Tipo de entidad que la propia pregunta menciona, o None."""
    if not pregunta:
        return None
    t = normalizar(pregunta)
    encontrados = {tipo for tipo, palabras in _PALABRAS_TIPO.items()
                   if any(p in t.split() or p in t for p in palabras)}
    return encontrados.pop() if len(encontrados) == 1 else None
