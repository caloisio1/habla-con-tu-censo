# Habla con tu Censo

**Español** · [Read in English →](README.en.md)

*Hablá con tu Censo — interfaz en lenguaje natural sobre los microdatos censales de Uruguay (2011 y 2023)*

**Preguntale al Censo 2011 y al Censo 2023 de Uruguay en español. Elegís el censo
con un selector; las respuestas se calculan sobre los microdatos oficiales —nunca
desde la memoria de un modelo— y en cada respuesta se muestra el SQL ejecutado.**

**Demo en vivo: https://srv1236510.hstgr.cloud/**

Un prototipo funcional de una capa de consulta en lenguaje natural para oficinas
nacionales de estadística, pensado como una alternativa moderna y auditable a las
herramientas de difusión heredadas.

<p align="center">
  <img src="docs/captura-inicio.png" width="820"
       alt="Página de inicio con una caja de búsqueda grande que pregunta '¿Cuánta gente vive en Salto?' y chips de preguntas de ejemplo debajo">
</p>

Preguntá en español y obtené una respuesta calculada sobre los microdatos —con un
mapa coroplético cuando la consulta es geográfica, el universo válido y las celdas
suprimidas etiquetadas, y el SQL exacto a un clic:

<p align="center">
  <img src="docs/captura-resultado.png" width="600"
       alt="Tarjeta de resultado: '% de afrodescendientes por departamento' respondido con una tabla y un mapa coroplético de Uruguay, sobre un desplegable 'Ver SQL ejecutado'">
</p>

<details>
<summary>Tema oscuro (mismo resultado)</summary>

<p align="center">
  <img src="docs/captura-resultado-oscuro.png" width="600"
       alt="La misma tarjeta de resultado renderizada en tema oscuro">
</p>

</details>

<sub>Temas claro/oscuro, adaptable a móvil, y el SQL ejecutado a la vista en cada
respuesta.</sub>

## Qué es

Una interfaz conversacional sobre los **microdatos a nivel de persona de los Censos
2011 y 2023 de Uruguay** (INE). Un selector en la interfaz elige el censo;
**el predeterminado es 2023**. Cada respuesta queda etiquetada con el censo del que
proviene.

- **2011** — la tabla `personas` tiene una fila por persona, **3.285.824 registros**
  en **155 columnas**: las **145 variables crudas del INE** (con sus códigos
  originales) más **columnas derivadas** legibles (`departamento`, `sexo`, `edad`,
  `asc_afro`, `asc_principal`, `nbi`, `codsec`, `codloc`) y claves estructurales
  (`hogar_key`, `vivienda_key`). Una tabla de referencia `localidades` resuelve los
  nombres de lugar. Los conteos son **exactos**.
- **2023 (ponderado)** — el censo ponderado del INE. Las cifras de personas son
  **estimaciones**: la cifra publicada es `SUM(W)` sobre el ponderador, así que la
  interfaz las redondea y las etiqueta como estimaciones ponderadas. Los hogares
  (`COUNT(DISTINCT hogar_key)`) y las viviendas (una tabla `viviendas_2023` aparte)
  son **conteos exactos**. Los nombres de lugar se resuelven con el nomenclátor
  oficial (departamentos, localidades, secciones y segmentos censales, barrios de
  Montevideo).

Una pregunta en español se traduce a SQL con un LLM, el SQL se valida con un parser
real, se ejecuta sobre SQLite, se controla la divulgación y recién entonces se
convierte en prosa que cita las cifras reales.

## Qué se puede preguntar

- **Conteos** de personas, hogares o viviendas —
  `COUNT(*)`, `COUNT(DISTINCT hogar_key)`, `COUNT(DISTINCT vivienda_key)`
  (las variables de hogar y vivienda se repiten en cada integrante, por eso se
  fuerzan los conteos distintos).
- **Frecuencias** de cualquiera de las 145 variables (p. ej. viviendas por tipo,
  hogares por cantidad de Necesidades Básicas Insatisfechas).
- **Porcentajes** con **exclusión automática de los perdidos**: los códigos marcados
  como no relevado / no corresponde / ignorado / secreto estadístico (y los NULL) se
  descartan siempre de conteos y denominadores — nunca se presenta una tasa sobre la
  población total cuando la variable tiene perdidos.
- **Selección jerárquica** — subconsultas jerárquicas que expresan condiciones
  sobre *otros* integrantes del hogar (p. ej. "personas en hogares donde al menos un
  integrante tiene más de 75 años").
- **Niveles geográficos** — departamento, localidad (por nombre, vía la tabla
  `localidades`), barrio de Montevideo (`BARRIO85`) / CCZ, sección censal (`codsec`)
  y segmentos censales (2023), la geografía más fina publicada — con **mapas
  coropléticos (Leaflet)** para los niveles mapeables.
- **Lugar de nacimiento y migración interna** — personas nacidas en el exterior, por
  país (resuelto con el clasificador oficial de países del INE, `datos/paises.csv`,
  unido como `localidades`), y la matriz de migración interna (nacidos en el
  departamento X que viven en Y). Los códigos de país y de departamento de nacimiento
  vienen del nomenclátor, nunca de la memoria del modelo. Ver `datos/NOTAS_CALIDAD.md`
  para el detalle de la codificación.

## Control de divulgación estadística

Como la tabla subyacente es de microdatos (una fila por persona), la compuerta de
validación (`app/sql_guard.py`) se apoya en un **parser SQL real (`sqlglot`)** que
trabaja sobre el árbol parseado, no sobre coincidencias de texto:

1. **Salida solo agregada** — cada columna de la proyección externa debe ser un
   agregado o una columna del `GROUP BY`; `SELECT *` y la selección de columnas
   sueltas se rechazan. Nunca se devuelven filas individuales.
2. **Supresión estructural de celdas chicas** — el guard identifica *en el árbol* qué
   columnas de salida son conteos y se las pasa al supresor; toda celda de resultado
   con menos de **5 personas** se descarta tras la ejecución (práctica estándar de las
   ONE). Las geografías suprimidas se pintan en gris ("sin dato publicable") en los
   mapas.
3. **Falla cerrada** — si el guard no puede probar que una consulta es segura
   (no parseable, conteos no identificables, tabla/columna/función no permitida), la
   consulta **no se ejecuta** y no se improvisa ninguna respuesta.
4. **Defensa en profundidad** — una sola sentencia `SELECT`; lista blanca de tablas
   (`personas`, `localidades`, `paises`) con JOIN permitido solo por las claves del
   nomenclátor (`codloc`, código de país); lista blanca de columnas desde el
   diccionario; claves reidentificantes (`hogar_key`, `vivienda_key`) permitidas en la
   proyección externa solo dentro de `COUNT(DISTINCT …)`; funciones peligrosas de
   SQLite bloqueadas; `LIMIT` obligatorio y acotado.

El umbral de supresión, la lista blanca y el prompt son **configuración derivada del
diccionario, no código escrito a mano**.

## Arquitectura

```
pregunta (ES) → el LLM escribe SQL → compuerta de validación sqlglot → SQLite sobre microdatos
             → supresión de celdas chicas → el LLM redacta la respuesta con las cifras reales
             → mapa coroplético (Leaflet) cuando la consulta es geográfica
```

**FastAPI + SQLite + OpenAI + Leaflet**, front-end de una sola página ~vanilla (sin
paso de build). El texto del esquema que ve el modelo, la lista blanca de columnas que
aplica el guard y las reglas de perdidos se **generan todos a partir de un diccionario
de datos** —los metadatos públicos del INE de cada censo—, no de código escrito a
mano. El mismo patrón corre **dos motores detrás de un selector**: el motor 2011
(`app/main.py`, `app/sql_guard.py`) y el motor 2023 ponderado (`consultar_2023.py`,
`sql_guard_2023.py`), cada uno con su diccionario y sus reglas de divulgación,
compartiendo el front-end, los mapas Leaflet y la garantía de solo-agregados. El
modelo se configura con la variable de entorno `CENSO_MODELO`.

## Cómo correrlo

```bash
pip install -r requirements.txt

# El archivo de microdatos del INE NO se incluye en este repo (no es nuestro para
# redistribuir). Descargá los microdatos de personas del Censo 2011 desde
# https://www.ine.gub.uy y poné el .sav en datos/.
# El archivo esperado es "Base unificada Viv_Hog_Pers.sav".

# 1. Generar el diccionario de datos — el esquema, la lista blanca y el prompt del LLM
#    derivan de este archivo. Solo metadatos: lee etiquetas, no microdatos.
python datos/generar_diccionario.py "datos/Base unificada Viv_Hog_Pers.sav"

# 2. Verificar el mapeo de columnas contra tu archivo antes de cargar (es también
#    el módulo del que cargar.py importa sus reglas de derivación).
python datos/convertir_ine.py --inspeccionar "datos/Base unificada Viv_Hog_Pers.sav"

# 3. Construir la base (datos/censo.db). Lee el .sav en bloques —sin CSV
#    intermedio— cargando las 145 variables crudas + columnas derivadas + la tabla
#    de referencia localidades.
python datos/cargar.py "datos/Base unificada Viv_Hog_Pers.sav"

# 4. Configurar tu clave de OpenAI
export OPENAI_API_KEY=sk-...

# 5. Lanzar
uvicorn app.main:app --reload
```

Después abrí http://localhost:8000 y preguntá, por ejemplo:
*"¿Cuántas mujeres mayores de 75 años hay en Rivera?"*

> Los pasos de arriba construyen la base de **2011**. El censo **2023 ponderado**
> (`consultar_2023.py`, `sql_guard_2023.py`) corre contra su propia base, preparada
> por separado a partir de los microdatos ponderados y el nomenclátor del INE; esos
> microdatos y su pipeline de carga no forman parte de este repositorio. El
> diccionario de datos 2023 y las capas GeoJSON usadas en tiempo de ejecución **sí**
> se incluyen.

Columnas clave de la tabla `personas`:

| Columna | Descripción |
|---|---|
| `departamento`, `sexo`, `edad` | Departamento (19 valores), sexo, edad en años |
| `asc_afro` | Mención de ascendencia afro/negra (`Si`/`No`/NULL) |
| `asc_principal` | Ascendencia principal declarada (solo para quienes declararon más de una) |
| `nbi` | Necesidades Básicas Insatisfechas del hogar (0–3, topeada en "3 o más") |
| `codsec`, `codloc`, `BARRIO85`, `CCZ` | Sección censal, localidad, barrio de Montevideo / CCZ |
| `hogar_key`, `vivienda_key` | Id de hogar / vivienda (solo dentro de `COUNT(DISTINCT …)`) |
| *145 variables crudas del INE* | Códigos originales (p. ej. `VIVVO03`, `HOGPR01`), con las etiquetas de valor del diccionario |

Los perdidos (no relevado, viviendas colectivas, secreto estadístico) se guardan como
NULL y se excluyen siempre de conteos y denominadores.

## Calidad y alcance de los datos

Los microdatos de personas de **2011** contienen **solo viviendas ocupadas**, así que
las preguntas por **viviendas desocupadas / vacantes** están fuera de alcance ahí y
devuelven un mensaje claro (`NO_RESPONDIBLE_VIVIENDAS`) en vez de una respuesta
incorrecta. El censo **2023** tiene una tabla de viviendas dedicada, así que las
preguntas por viviendas desocupadas *sí* se responden bajo el selector 2023. Los
conteos de carga, la nota de consistencia hogares/`PERID`, la codificación del lugar
de nacimiento y la limitación de alcance de 2011 están documentados en
[`datos/NOTAS_CALIDAD.md`](datos/NOTAS_CALIDAD.md).

**Nomenclátor de localidades.** Los nombres de localidad de **2011** están validados
contra el clasificador oficial del INE (615/615 localidades). Los de **2023**
provienen de la **cartografía oficial del INE 2023** (geopackage de localidades,
`loc_23_pg`) y resuelven el 100% de los códigos de localidad presentes en los
microdatos. El INE mantiene el marco cartográfico 2023 en **revisión técnica** (nota
del 14/05/2026); si publica una versión corregida, el nomenclátor y las capas
geográficas se regeneran desde la fuente oficial.

## Tests

```bash
pytest
```

La batería cubre la compuerta de seguridad (`test_sql_guard.py`: intentos de
inyección, intentos de extracción a nivel de fila, listas blancas de tabla/columna,
exigencia de `COUNT(DISTINCT)` sobre las claves, supresión de celdas chicas), la
detección de nivel de mapa (`test_mapa.py`), la normalización de nombres de
departamento (`test_normalizar.py`) y la reparación de texto del `.sav`
(`test_reparar.py`).

## Por qué existe

La mayoría de las oficinas de estadística de América Latina todavía difunden datos
censales con herramientas diseñadas en los años 90. Este prototipo muestra que una
capa de consulta segura, auditable y potenciada por LLM sobre microdatos oficiales
—con el control de divulgación aplicado por un parser, no por confianza en el
modelo— se puede construir en unos pocos cientos de líneas de Python.

## Próximos pasos

El desarrollo continúa en cuatro direcciones:

**Comparación intercensal (2011–2023).** El sistema calculará el mismo indicador en
ambos censos —cada uno con sus propias reglas de conteo y supresión— y compondrá la
comparación con advertencias metodológicas automáticas. La pieza central es una capa
de armonización explícita y revisable: un archivo público que documenta, variable por
variable, qué es comparable entre censos, con qué mapeo de códigos y con qué nota
obligatoria. Las comparaciones que no son metodológicamente defendibles (por ejemplo,
geografía fina rediseñada entre rondas) no se habilitan. La armonización es un trabajo
de criterio estadístico, no de código: cada entrada lleva un veredicto experto antes
de activarse.

**Exposición como servidor MCP.** La misma capa de validación estructural y supresión
que protege la interfaz web puede exponerse mediante el protocolo MCP (Model Context
Protocol), permitiendo que cualquier asistente de IA consulte los microdatos censales
recibiendo únicamente agregados publicables. El validador actúa como cortafuegos de
revelación estadística, independiente del modelo que escriba la consulta. Esto
integraría los microdatos censales al ecosistema de datos públicos accesibles por IA
que ya se está construyendo en Uruguay, cubriendo la capa que las herramientas sobre
datos abiertos no alcanzan.

**Extensión a nuevas fuentes.** La arquitectura genera su esquema, su lista blanca y su
prompt a partir de la documentación oficial de cada fuente, por lo que incorporar
otros censos de la ronda 2020 de la región —o encuestas de hogares— es un problema de
carga y configuración, no de reescritura. Entre las extensiones previstas: cálculo de
variables derivadas por chat y ampliación de las bases disponibles.

**Disponibilidad multilingüe.** La interfaz estará disponible además en portugués,
francés e inglés, ampliando el alcance regional e internacional del sistema.

## Autor

Carlos Aloisio — sociólogo, estadístico y desarrollador (Montevideo, UY).
Constructor del Observatorio Nacional de Salud Sexual y Reproductiva de Uruguay
(sistema de evidencia asistido por IA, Ministerio de Salud Pública / UNFPA, 2025–2026).

Contacto: caloisio@gmail.com

## Licencia

MIT
