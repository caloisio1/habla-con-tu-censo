# Habla con tu Censo

*"Talk to your Census" — natural-language interface over Uruguay's 2011 census microdata*

**Ask Uruguay's 2011 Census questions in plain Spanish. Get answers computed
from official microdata — never from an LLM's memory.**

**Live demo: https://srv1236510.hstgr.cloud/**

A working prototype of a natural-language query layer for national statistical
offices, built as a modern alternative to legacy dissemination tools (REDATAM).

## How it works

```
question (ES) → LLM writes SQL → validation gate → SQLite over microdata
             → small-cell suppression → LLM writes answer from real numbers
```

The design principle is **attribution or refusal**: the LLM never answers from
its own knowledge. If a query cannot be validated, it is not executed. If a
result cell is too small to publish, it is suppressed. The executed SQL is
always shown to the user.

## Statistical disclosure control

Because the underlying table is microdata (one row per person), the validation
gate (`app/sql_guard.py`) enforces:

1. **Aggregate-only queries** — `COUNT(*)` required, individual rows can never
   be returned (`SELECT *` and bare column selection are rejected).
2. **Small-cell suppression** — any result cell with fewer than 5 persons is
   suppressed after execution, following standard NSO practice.
3. **Defense-in-depth** — SELECT-only, single statement, table/column
   whitelist, no comments, no UNION, mandatory LIMIT.

The suppression threshold and whitelist are configuration, not code — adapting
this to another country's census is a schema change, not a rewrite.

## Run it

```bash
pip install -r requirements.txt
# 1. Download INE's anonymized public-use microdata (https://www.ine.gub.uy)
# 2. Convert the .sav to datos/personas.csv (one row per person)
python datos/convertir_ine.py datos/ARCHIVO_INE.sav
# 3. Build the database (datos/censo.db)
python datos/cargar.py
# 4. Set your LLM key
export OPENAI_API_KEY=sk-...
# 5. Launch
uvicorn app.main:app --reload
```

The `personas` table (one row per person) holds:

| Column | Description |
|---|---|
| `departamento`, `sexo`, `edad` | Geography, sex and age |
| `asc_afro` | Mention of Afro/Black ancestry (`Si`/`No`/NULL) |
| `asc_principal` | Main declared ancestry (only for those declaring more than one) |
| `nbi` | Count of the household's Unsatisfied Basic Needs (0–3, capped at "3 or more") |
| `hogar_key` | Household id — only usable inside `COUNT(DISTINCT hogar_key)` |

Missing values (not surveyed, collective dwellings, statistical secrecy) are
stored as NULL and always excluded from counts and denominators.

Then open http://localhost:8000 and ask: *"¿Cuántas mujeres mayores de 75 años
hay en Rivera?"*

## Tests

```bash
pytest
```

The test suite covers the security gate: injection attempts, row-level
extraction attempts, and disclosure-control suppression.

## Why this exists

Most statistical offices in Latin America still disseminate census data
through tools designed in the 1990s. This prototype demonstrates that a safe,
auditable, LLM-powered query layer over official microdata can be built with
~300 lines of Python — with disclosure control enforced by code, not by trust
in the model.

## Author

Carlos Aloisio — sociologist, statistician and developer (Montevideo, UY).
Builder of Uruguay's National Observatory on Sexual and Reproductive Health
(AI-assisted evidence system, Ministry of Public Health / UNFPA, 2025–2026).

## License

MIT
