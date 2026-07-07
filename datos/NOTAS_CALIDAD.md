# Notas de calidad de datos — Habla con tu Censo

Fuente: microdatos del Censo 2011 (INE Uruguay), archivo
`Base unificada Viv_Hog_Pers.sav` (145 variables).

## Carga de `personas` (v4)

- **Filas leídas del `.sav`:** 3.285.877
- **Filas descartadas:** 53, por tener `departamento`, `sexo` o `edad` fuera de
  rango o inválidos (misma regla de limpieza desde v3).
- **Personas cargadas:** **3.285.824**
- **Imputadas por moradores ausentes (`MA=1`) incluidas:** 34.223

### Referencia oficial del INE
- Población **censada:** 3.252.091
- Población **contabilizada** (incluye 34.223 imputadas): 3.286.314

La cifra cargada (3.285.824) es la contabilizada (3.286.314) menos los 53
descartes por datos inválidos y las diferencias de cobertura del archivo público.

## Consistencia de hogares y `PERID` (diferencia de 7, por diseño)

- `COUNT(DISTINCT hogar_key)` = **1.166.251** hogares
- `COUNT(*) WHERE PERID=1`    = **1.166.244**
- **Diferencia: 7 hogares (0,0006%).**

Causa: 7 de las 53 filas descartadas eran el **jefe de hogar** (`PERID=1`). Al
descartar esa fila por `departamento`/`sexo`/`edad` inválidos, el hogar sigue
existiendo (sobrevive por sus otros miembros con `PERID>=2`), pero se queda sin
la persona `PERID=1`. Por eso el conteo de personas con `PERID=1` es 7 menor que
el de hogares distintos. No hay hogares con `PERID=1` duplicado (0). Es un
artefacto esperado de la limpieza de filas, no un error del pipeline.

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
- **2023:** el nomenclátor de localidades es **provisional** — a la fecha el INE no
  publicó la versión actualizada para 2023, por lo que la resolución de nombres de
  localidad 2023 se apoya en la base 2011 y puede diferir en localidades nuevas o
  redelimitadas.
