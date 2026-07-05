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
