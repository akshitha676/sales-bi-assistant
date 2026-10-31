# Architecture



## Components

| File | Responsibility |
|---|---|
| `app/db.py` | Reads the CSV, validates every row, loads good rows into SQLite `sales` and bad rows into `rejected_rows` (with reason). Opens the DB read-only for queries. |
| `app/llm.py` | Turns a question into a JSON plan using Gemini; falls back to a rule-based parser if Gemini fails. `sanitize_plan` whitelists the result. |
| `app/queries.py` | Five fixed, parameterised SQL analyses. Validates parameters against values in the DB and builds the answer text from the results. |
| `app/main.py` | FastAPI app: `POST /ask`, `GET /api/meta`, `GET /api/health`, consistent JSON errors. |
| `app/static/index.html` | Simple UI: question box, answer, key figures, supporting table, and the SQL used. |

## Request flow

1. The UI sends the question to `POST /ask`. Empty, over-long or malformed requests get a 422.
2. Questions that ask to drop/delete/update data are refused before any interpretation.
3. Gemini converts the question into a plan such as `{"intent": "month_comparison", "month_a": "2026-02", "month_b": "2026-03"}`. If Gemini errors or times out, the rule-based parser does it instead.
4. `sanitize_plan` keeps only known keys and the five known intents. Anything else becomes "unsupported".
5. `queries.py` checks each parameter exists in the data, runs the intent's fixed SQL on a read-only connection, and generates the answer sentence from the rows.
6. The response contains the answer, key figures, supporting rows, the SQL and parameters used, which interpreter was used, and data-quality notes.

## Why this design

The LLM is used only for understanding language, not for calculating. It never writes SQL and never produces the numbers, so answers are reproducible and cannot be hallucinated or SQL-injected. The trade-off is flexibility: only the five supported analyses can be answered.
