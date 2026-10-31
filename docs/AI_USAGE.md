# AI Usage Report

> Edit this to match your own experience before submitting. The sections below describe what actually happened
> while this project was built; if you changed anything or hit different problems, say so. The interviewers will ask.

## 1. Which AI tools were used

- **Claude (Anthropic)**: used as the coding assistant to scaffold the project, write the modules and tests, and draft the docs.
- **Google Gemini API (`gemini-flash-latest`, free tier)**: used *inside* the application at runtime to interpret the user's question.

## 2. How AI was used during development

- I gave Claude the challenge document and the CSV and asked it to build the application with the Gemini API.
- Claude profiled the CSV first and found three problem rows (quantity 0, blank unit price, a Returned order) before writing code. The handling rules were decided from that.
- Architecture choice: instead of letting the LLM generate SQL (text-to-SQL), the LLM only classifies the question into one of five fixed analyses. This was chosen because the challenge asks to "explain how you prevent unsupported or incorrect answers": it is much easier to guarantee correctness with fixed queries than to validate arbitrary SQL.
- Claude wrote the tests; each expected number is recomputed independently from the raw CSV so the tests do not just echo the app's own logic.
- I reviewed and ran the code locally, and I can explain every module (see `docs/INTERVIEW_NOTES.md`).

## 3. How AI is used inside the app

Gemini receives the question plus the lists of valid regions/products/months and returns a small JSON plan.
It never sees the database, never writes SQL, and never writes the final answer text. The code validates its output
(`sanitize_plan`), validates each parameter against the database, and builds the answer from the query results.

## 4. Problems encountered and how they were debugged

| Problem | How it showed up | Fix |
|---|---|---|
| Dirty data: qty 0, blank price, Returned status | Spotted by profiling the CSV with pandas before coding | Quarantine into `rejected_rows` with reasons; exclude Returned from revenue; document the decisions |
| Dates are `DD-MM-YYYY` | Ambiguous with `MM-DD-YYYY` (e.g. `03-01-2026`); strings also sort wrongly | Parse with an explicit format, store ISO dates and a `YYYY-MM` month column |
| Rule-based fallback answered `DROP TABLE sales;` with the total revenue (the word "sales" matched) | Found in a manual smoke test of dangerous inputs | Added a destructive-keyword pre-check before any interpretation, plus a regression test; the DB connection is also read-only |
| Model names change over time, and I could not confirm the exact free-tier model ID | Search results listed several Gemini versions | Default to the `gemini-flash-latest` alias and make the model configurable with `GEMINI_MODEL` |
| LLMs sometimes wrap JSON in markdown fences or return garbage | Anticipated; covered with mocked tests | `parse_json_text` strips fences; any failure falls back to the rule interpreter with a visible warning |
| Free-tier quota / network errors | Anticipated | Same fallback path, and a test that simulates a `429` exception |

Debugging approach: reproduce with a one-line `curl` or a failing pytest first, then fix, then keep the test as a regression guard.

## 5. What I would improve with another day

- Validated text-to-SQL fallback (single `SELECT` only, parsed with `sqlglot`) for questions beyond the five intents.
- Follow-up questions with conversation memory.
- A 50-question evaluation set to measure intent accuracy of Gemini against the rule baseline.
- Charts in the UI, Dockerfile, and CI that runs the tests on each push.

## 6. Honest status

- **Works and tested**: data loading, all five analyses, API error handling, safety checks, UI, the Gemini code path with a mocked client (26 automated tests).
- **Not verified by automated tests**: a live call to Gemini with a real API key. The code path follows the `google-genai` SDK, but run one real question yourself and check the "How this was computed" panel shows `gemini`.
