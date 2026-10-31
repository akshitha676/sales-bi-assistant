"""Automated tests. The Gemini API is never called: a fake interpreter / fake client is used,
so the suite is fast, free and deterministic."""
import csv
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import build_database, connect_readonly
from app.llm import FallbackInterpreter, GeminiInterpreter, Interpretation, RuleInterpreter, sanitize_plan
from app.main import create_app

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "sales.csv"


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    build_database(CSV_PATH, path)
    return path


@pytest.fixture()
def client(db_path):
    """App wired to the rule-based interpreter only (no network)."""
    return TestClient(create_app(db_path=db_path, interpreter=FallbackInterpreter(primary=None)))


class FakeInterpreter:
    """Pretends to be an LLM returning a fixed (possibly malicious) plan."""

    def __init__(self, plan):
        self.plan = plan

    def interpret(self, question, catalog):
        return Interpretation(sanitize_plan(self.plan), "fake")


def expected_revenue(**match):
    """Independent re-computation straight from the CSV (not via our DB code)."""
    total = 0.0
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["Status"] != "Completed" or not r["Unit_price"].strip() or int(r["Quantity"]) <= 0:
                continue
            if any(r[k] != v for k, v in match.items() if k != "month"):
                continue
            if "month" in match and r["Order_date"][3:5] != match["month"]:
                continue
            total += int(r["Quantity"]) * float(r["Unit_price"]) - float(r["Discount_amount"])
    return total


def ask(client, q):
    return client.post("/ask", json={"question": q})


# ------------------------------------------------------------------ data loading
def test_invalid_rows_are_quarantined_not_silently_dropped(db_path):
    conn = sqlite3.connect(db_path)
    loaded = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    rejected = dict(conn.execute("SELECT transaction_id, reason FROM rejected_rows").fetchall())
    assert loaded == 49
    assert set(rejected) == {"TXN0034", "TXN0051"}  # qty = 0, missing unit price
    assert "Quantity" in rejected["TXN0034"] and "Unit_price" in rejected["TXN0051"]
    # dates were converted from DD-MM-YYYY to ISO
    assert conn.execute("SELECT order_date FROM sales WHERE transaction_id='TXN0001'").fetchone()[0] == "2026-01-03"


# ------------------------------------------------------------------ valid questions
def test_total_revenue_matches_independent_calculation(client):
    r = ask(client, "What is the total revenue?")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"][0]["net_revenue"] == pytest.approx(expected_revenue())
    assert body["rows"][0]["orders"] == 48          # 49 loaded minus 1 returned
    assert body["sql"] and body["figures"]          # supporting data is returned


def test_top_product_is_monitor_and_returned_order_excluded(client):
    body = ask(client, "Which product generated the highest revenue?").json()
    assert body["rows"][0]["product"] == "Monitor"
    # Monitor revenue must NOT include the Returned order TXN0033 (2 x 9500)
    assert body["rows"][0]["net_revenue"] == pytest.approx(expected_revenue(Product="Monitor"))


def test_top_region(client):
    body = ask(client, "Which region generated the highest sales?").json()
    expected = {reg: expected_revenue(Region=reg) for reg in ("North", "South", "East", "West")}
    assert body["rows"][0]["region"] == max(expected, key=expected.get)
    assert body["rows"][0]["net_revenue"] == pytest.approx(max(expected.values()))


def test_monthly_trend_has_six_ordered_months(client):
    body = ask(client, "Show me the monthly revenue trend").json()
    months = [r["month"] for r in body["rows"]]
    assert months == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    assert body["rows"][0]["change_vs_prev_month_pct"] is None
    assert body["rows"][1]["net_revenue"] == pytest.approx(expected_revenue(month="02"))


def test_month_comparison(client):
    body = ask(client, "How did revenue change between February and March?").json()
    feb, mar = expected_revenue(month="02"), expected_revenue(month="03")
    assert body["intent"] == "month_comparison"
    assert body["figures"]["Absolute change"] == f"{mar - feb:+,.2f}"
    assert "decreased" in body["answer"] if mar < feb else "increased" in body["answer"]


def test_filters_combine(client):
    body = ask(client, "total revenue for Monitor in North").json()
    assert body["rows"][0]["net_revenue"] == pytest.approx(expected_revenue(Product="Monitor", Region="North"))


# ------------------------------------------------------------------ invalid questions & errors
@pytest.mark.parametrize("payload", [{"question": ""}, {"question": "    "}, {"question": "x" * 301}, {}, {"question": 123}])
def test_invalid_request_bodies_return_422_with_error_shape(client, payload):
    r = client.post("/ask", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_out_of_scope_question_is_refused_not_guessed(client):
    r = ask(client, "What will the weather be like tomorrow?")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "unsupported_question"
    assert err["supported_questions"]               # tells the user what they can ask


# ------------------------------------------------------------------ safety
@pytest.mark.parametrize("q", ["DROP TABLE sales;", "delete all rows from sales", "please update the unit price to 1"])
def test_destructive_requests_are_refused_and_data_is_untouched(client, db_path, q):
    r = ask(client, q)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_question"
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 49


def test_connection_used_for_queries_cannot_write(db_path):
    conn = connect_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM sales")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DROP TABLE sales")


def test_malicious_llm_output_cannot_inject_sql(db_path):
    """Even if the model is prompt-injected and returns SQL, only whitelisted intents run."""
    evil = {"intent": "run_sql", "sql": "DROP TABLE sales"}
    c = TestClient(create_app(db_path=db_path, interpreter=FakeInterpreter(evil)))
    r = ask(c, "anything")
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_question"
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 49


def test_llm_filter_value_is_validated_against_data(db_path):
    plan = {"intent": "total_revenue", "filters": {"region": "North'; DROP TABLE sales;--"}}
    c = TestClient(create_app(db_path=db_path, interpreter=FakeInterpreter(plan)))
    r = ask(c, "revenue in the north")
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_value"


# ------------------------------------------------------------------ edge cases
def test_month_without_data_gives_clear_error(client):
    r = ask(client, "What was the revenue in December?")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "no_data"
    assert "January 2026" in r.json()["error"]["message"]


def test_comparing_same_month_twice_is_rejected(db_path):
    plan = {"intent": "month_comparison", "month_a": "2026-02", "month_b": "2026-02"}
    c = TestClient(create_app(db_path=db_path, interpreter=FakeInterpreter(plan)))
    assert ask(c, "compare feb with feb").status_code == 422


def test_month_naming_is_case_and_year_tolerant(client):
    body = ask(client, "REVENUE FOR march 2026").json()
    assert body["rows"][0]["net_revenue"] == pytest.approx(expected_revenue(month="03"))


# ------------------------------------------------------------------ LLM integration (mocked)
class _Resp:
    def __init__(self, text):
        self.text = text


class _FakeGenaiClient:
    def __init__(self, text=None, error=None):
        self.models = self
        self._text, self._error = text, error

    def generate_content(self, **kwargs):
        if self._error:
            raise self._error
        return _Resp(self._text)


def test_gemini_json_in_code_fence_is_parsed():
    fenced = '```json\n{"intent": "top_region", "filters": {}}\n```'
    interp = GeminiInterpreter("key", "model", client=_FakeGenaiClient(fenced))
    assert interp.interpret("best region?", {"month": ["2026-01"]})["intent"] == "top_region"


@pytest.mark.parametrize("bad", [_FakeGenaiClient("not json at all"), _FakeGenaiClient(error=RuntimeError("429 quota"))])
def test_gemini_failure_falls_back_to_rules_with_warning(db_path, bad):
    interp = FallbackInterpreter(GeminiInterpreter("key", "model", client=bad))
    c = TestClient(create_app(db_path=db_path, interpreter=interp))
    body = ask(c, "What is the total revenue?").json()
    assert body["interpreter"] == "rules"
    assert body["warnings"] and "gemini unavailable" in body["warnings"][0]
    assert body["rows"][0]["net_revenue"] == pytest.approx(expected_revenue())


def test_rule_interpreter_unknown_question_is_unsupported():
    plan = RuleInterpreter().interpret("tell me a joke", {"month": ["2026-01"], "region": [], "product": [], "category": [], "channel": []})
    assert plan["intent"] == "unsupported"
