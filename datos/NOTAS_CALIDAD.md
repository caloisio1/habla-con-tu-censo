# Notas de calidad de datos — Habla con tu Censo

Fuente: microdatos del Censo 2011 (INE Uruguay), archivo
`Base unificada Viv_Hog_Pers.sav` (145 variables).

## Carga de `personas` (v4)

- **Filas leídas del `.sav`:** 3.285.877
- **Filas descartadas:** **0** (desde el 28-jul-2026)
- **Personas cargadas:** **3.285.877** = todas las del archivo
- **Imputadas por moradores ausentes (`MA=1`):** **34.223**, que coincide exacto
  con la cifra publicada por el INE
- **Personas con `edad = NULL`:** 53, por secreto estadístico

Hasta esa fecha se descartaban 53 filas. La nota decía que era "por
`departamento`, `sexo` o `edad` fuera de rango o inválidos", y eso era falso:
describía la regla del cargador, no lo que la regla rechazaba. Ninguna de las 53
tenía departamento ni sexo inválidos. Las 53 salían por una sola causa,
`PERNA01 = 5555`, que es el código de **secreto estadístico** del INE — no una
edad corrupta.

### Por qué ahora se cargan

Las 53 filas **no son personas sueltas: son 19 hogares completos**. En cada uno de
esos 19 hogares TODOS los integrantes están bajo secreto, así que el hogar
desaparecía entero de la base. Recuperarlas suma **+53 personas, +19 hogares y
+19 viviendas**.

Qué traen realmente esas filas: el secreto cubre casi todo el cuestionario, **de
93 a 106 de las 145 variables vienen en 5555** (vivienda, hogar, educación,
ascendencia, migración, actividad, NBI). Lo único utilizable es la **geografía**
(departamento, sección, localidad), el **sexo** y las claves de hogar y vivienda.
Solo 2 de las 53 son imputadas por moradores ausentes (`MA=1`).

**Decisión (28-jul-2026, Carlos): se cargan.** Son personas que el censo contó;
lo protegido es su cuestionario, no su existencia. Descartarlas era además
incoherente con el resto del pipeline, que para cualquier otra variable mapea
5555 a NULL y conserva la fila. Ahora `edad` queda en NULL —perdido, excluido de
todo corte por edad— y las 53 suman a la población, al departamento y al sexo.

**Consecuencia a tener presente:** el 5555 queda en las columnas CRUDAS (las
crudas se guardan tal cual, por diseño), y solo 13 de las 145 variables lo traen
etiquetado como `SECRETO ESTADISTICO` en el diccionario del INE. En las otras ~90
el modelo no tendría cómo saber que es un perdido, así que la regla se declara de
forma global en el prompt (`app/main.py`, bloque PERDIDOS): en este censo el
código 5555 es secreto estadístico en cualquier variable, esté o no listada.

### Referencia oficial del INE
- Población **censada:** 3.252.091
- Imputadas por moradores ausentes: 34.223
- Población **contabilizada** (censada + imputadas): **3.286.314**

### Cuadre de la base contra la cifra publicada

| | INE publicado | En la base | Diferencia |
|---|---|---|---|
| Contabilizada | 3.286.314 | 3.285.877 | −437 |
| Censada (`MA=0`) | 3.252.091 | 3.251.654 | −437 |
| Imputadas (`MA=1`) | 34.223 | 34.223 | **0** |

La diferencia que queda **no es del pipeline**: el archivo público de microdatos
ya trae 437 registros menos que el total contabilizado, y la base carga todas sus
filas. La base es el **99,987%** de la población contabilizada. (Antes del
28-jul-2026 la diferencia era de 490, porque el pipeline descartaba 53 filas más.)

**Cómo se comunica:** la ficha del censo encabeza con la cifra PUBLICADA
(3.286.314), que es la única cotejable contra un documento oficial, y muestra
los registros de la base aparte. Nunca al revés: un total que no existe en
ninguna publicación del INE no se puede presentar como "la población de 2011".

## Consistencia de hogares y `PERID` (diferencia de 7, por diseño)

- `COUNT(DISTINCT hogar_key)` = **1.166.270** hogares
- `COUNT(*) WHERE PERID=1`    = **1.166.263**
- **Diferencia: 7 hogares (0,0006%).**

Causa: **son 7 hogares que en el archivo del INE ya vienen sin su fila `PERID=1`**
— sus listados arrancan en `PERID=2` (uno de ellos tiene 22 personas, de la 2 a
la 23). Verificado fila por fila contra el `.sav`: ninguna de las filas de esos 7
hogares tiene el código 5555, así que **el descarte del pipeline no interviene**.
No hay hogares con `PERID=1` duplicado (0), ni filas con `hogar_key` NULL (0).

Ojo, porque esta nota decía otra cosa y era falsa: no es que "7 de las 53 filas
descartadas eran el jefe de hogar". De las 53 descartadas, 19 son `PERID=1`, pero
esos 19 hogares desaparecen ENTEROS de la base (todos sus integrantes tienen
5555), así que aportan cero a los dos conteos y no pueden generar diferencia
alguna. Las dos cosas son independientes.

**Implicación práctica:** para contar hogares usá siempre
`COUNT(DISTINCT hogar_key)`, no `COUNT(*) WHERE PERID=1`.

## Limitación de alcance: solo viviendas OCUPADAS

La tabla `personas` es de MICRODATOS DE PERSONAS, por lo que solo contiene
viviendas **ocupadas**: `VIVVO03` (condición de ocupación) toma únicamente los
valores **1** (ocupada con residentes presentes, 1.121.603 viviendas) y **2**
(ocupada con residentes ausentes, 14.810). Una vivienda **desocupada** o vacante
(VIVVO03 3-7) no tiene residentes y por lo tanto no genera ninguna fila en un
archivo de personas: el stock de desocupadas NO está en estos datos.

Por eso, las preguntas por viviendas desocupadas/vacantes devuelven
`NO_RESPONDIBLE_VIVIENDAS` (ver `app/main.py`), aclarando al usuario que estos son
microdatos de personas y ofreciendo lo que sí es respondible (viviendas ocupadas
por departamento/localidad).

**Extensión futura:** cargar la **base de VIVIENDAS** del Censo 2011 (que sí
incluye el stock de viviendas desocupadas) como tabla aparte, para poder responder
preguntas sobre viviendas vacías, de uso temporal, en construcción, etc.

## Lugar de nacimiento y migración (bloque `PERMI`)

El censo relevó lugar de nacimiento y migración. Las variables (crudas del INE) son:
`PERMI01` (lugar de nacimiento: 1/2=en el país actual, 3=otro departamento, 4=otro
país), `PERMI01_2` (departamento de nacimiento, código '01'..'19'), `PERMI01_4`
(**país** de nacimiento, código), y los análogos de residencia anterior (`PERMI06`) y
de cinco años antes (`PERMI07`).

- **Nomenclátor de países** (`paises`, ver `datos/paises.csv`): los códigos de país
  siguen el **clasificador oficial del INE adaptado al Uruguay** (código numérico
  ONU / ISO 3166-1). Se carga como tabla de referencia y las consultas por país se
  resuelven con un JOIN `personas.PERMI01_4 = paises.codigo`, igual que `localidades`.
  Total nacidos en el exterior 2011: **77.002** (coincide con los tabulados del INE).
- **Códigos de 4 dígitos (solo 2011):** unos pocos orígenes usan un código de 4
  dígitos = país base (ISO 3 díg) × 10 + subdivisión (p. ej. España 724 → 7241/7242;
  Alemania 276 → 276x; Reino Unido 826 → 826x = las cuatro naciones del RU). Se
  agrupan al país base en el nomenclátor (columna `nombre_oficial` marca `(cód. 2011)`).
  Con esto la cobertura de país sube al **99,7%**; el resto (~0,3%: códigos 90xx e
  "ignorado" 9999) queda como país no especificado. En el Censo 2023 los códigos ya
  son de 3 dígitos y no requieren este ajuste.
- **Departamento de nacimiento (matriz de migración interna):** 2011 lo reconstruye de
  `PERMI01`/`PERMI01_2`; 2023 tiene la columna directa `DEPTO_NACIM`. Ojo: en 2023
  `DEPARTAMENTO` (residencia) lleva cero inicial ('01'..'19') pero `DEPTO_NACIM` no
  ('1'..'19').
- **2023 · enumerados por registro (`FUENTE_EXT`=2):** ~12.305 personas no tienen
  lugar de nacimiento relevado (100% faltante en `PERMI01`/`DEPTO_NACIM`). Se excluyen
  naturalmente al filtrar; en porcentajes de nacimiento el denominador son los relevados
  con cuestionario.

## Nomenclátor de localidades

- **2011:** validado contra el clasificador oficial del INE (*Localidades censales
  2011*): 615/615 localidades presentes con nombres completos.
- **2023:** el nomenclátor `localidades_2023` (671 localidades) se pobló desde la
  **cartografía oficial del INE 2023** (geopackage `loc_23_pg.gpkg`), NO de la tabla
  2011 (el nomenclátor 2011 solo cubría ~87% de los códigos 2023, por eso se bajó la
  cartografía oficial). (El archivo de referencia *Localidades censales 2011.xlsx*, con
  población por localidad, sí existe solo para 2011.)
  - Dos métricas de cobertura, distintas y ambas correctas: **96,57% (648/671)** es la
    cobertura del geopackage hacia nombres —las 23 sin nombre son pseudo-localidades
    rurales y códigos de missing—; **100% (621/621)** es la cobertura operativa: todos
    los códigos de localidad que aparecen en los microdatos resuelven a nombre.
  - El INE mantiene el **marco cartográfico 2023 en revisión técnica** (nota del
    14/05/2026); si publica una versión corregida, el nomenclátor y las capas
    geográficas se regeneran desde la fuente oficial (no se editan a mano).
