"""Deterministic analytics layer.

The LLM never writes SQL. It only picks one of the intents below and fills in
parameters. Each intent owns a fixed, parameterised SQL template, and every
parameter is validated against what actually exists in the database.
That is what makes answers reproducible and un-injectable.

Revenue definition (documented in the README):
    net revenue = quantity * unit_price - discount_amount
    only rows with status = 'Completed' are counted (Returned orders are excluded)
"""
import re
import sqlite3
from datetime import datetime

REVENUE = "(quantity * unit_price - discount_amount)"
COUNTED_STATUS = "Completed"

# user-facing filter name -> real column (whitelist; never built from user text)
FILTER_COLUMNS = {
    "region": "region",
    "product": "product",
    "category": "category",
    "channel": "sales_channel",
    "month": "order_month",
}

SUPPORTED_QUESTIONS = [
    "What is the total revenue?",
    "Which product generated the highest revenue?",
    "What is the monthly revenue trend?",
    "Which region generated the highest sales?",
    "How did revenue change between February and March?",
]


class QueryError(Exception):
    """A problem with the request itself (not a server bug)."""

    def __init__(self, message: str, code: str = "invalid_request", http_status: int = 422):
        super().__init__(message)
        self.message, self.code, self.http_status = message, code, http_status


# ---------------------------------------------------------------- helpers
def load_catalog(conn: sqlite3.Connection) -> dict:
    """Distinct values that actually exist in the data (used to validate input
    and to tell the LLM what is allowed)."""
    def distinct(col):
        return [r[0] for r in conn.execute(f"SELECT DISTINCT {col} FROM sales ORDER BY {col}")]

    return {
        "region": distinct("region"),
        "product": distinct("product"),
        "category": distinct("category"),
        "channel": distinct("sales_channel"),
        "month": distinct("order_month"),
    }


def data_notes(conn: sqlite3.Connection) -> list:
    """Short, always-true caveats about the data behind every answer."""
    notes = []
    rejected = conn.execute("SELECT COUNT(*) FROM rejected_rows").fetchone()[0]
    if rejected:
        notes.append(f"{rejected} invalid CSV row(s) were excluded (see /api/meta for reasons).")
    returned = conn.execute("SELECT COUNT(*) FROM sales WHERE status != ?", (COUNTED_STATUS,)).fetchone()[0]
    if returned:
        notes.append(f"{returned} non-completed order(s) (e.g. Returned) are not counted as revenue.")
    return notes


def month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def money(value: float) -> str:
    value = round(float(value), 2)
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


def normalise_filters(filters, catalog: dict) -> dict:
    """Validate filters against the catalog. Unknown keys/values raise QueryError."""
    if not filters:
        return {}
    if not isinstance(filters, dict):
        raise QueryError("'filters' must be an object")
    clean = {}
    for key, value in filters.items():
        if value in (None, ""):
            continue
        if key not in FILTER_COLUMNS:
            raise QueryError(f"Unsupported filter '{key}'. Allowed: {', '.join(FILTER_COLUMNS)}")
        value = str(value).strip()
        if key == "month":
            clean[key] = _validate_month(value, catalog)
            continue
        match = next((v for v in catalog[key] if v.lower() == value.lower()), None)
        if match is None:
            raise QueryError(
                f"There is no {key} called '{value}' in the data. Available: {', '.join(catalog[key])}.",
                code="unknown_value",
            )
        clean[key] = match
    return clean


def _validate_month(value: str, catalog: dict) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise QueryError(f"Month must look like YYYY-MM, got '{value}'")
    if value not in catalog["month"]:
        available = ", ".join(month_label(m) for m in catalog["month"])
        raise QueryError(
            f"There is no sales data for {month_label(value)}. Data covers: {available}.",
            code="no_data",
        )
    return value


def build_where(filters: dict):
    clauses, params = ["status = ?"], [COUNTED_STATUS]
    for key, value in filters.items():
        clauses.append(f"{FILTER_COLUMNS[key]} = ?")  # column comes from whitelist
        params.append(value)
    return " AND ".join(clauses), params


def describe_filters(filters: dict) -> str:
    if not filters:
        return ""
    parts = []
    for key, value in filters.items():
        parts.append(month_label(value) if key == "month" else f"{key} = {value}")
    return " (" + ", ".join(parts) + ")"


def run(conn, sql: str, params: list) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _result(intent, answer, sql, params, rows, figures=None, filters=None):
    return {
        "intent": intent,
        "answer": answer,
        "figures": figures or {},
        "rows": rows,
        "columns": list(rows[0].keys()) if rows else [],
        "sql": " ".join(sql.split()),
        "params": params,
        "filters": filters or {},
    }


# ---------------------------------------------------------------- intents
def total_revenue(conn, plan, catalog):
    filters = normalise_filters(plan.get("filters"), catalog)
    where, params = build_where(filters)
    sql = f"""
        SELECT COUNT(*)                                  AS orders,
               COALESCE(SUM(quantity), 0)                AS units_sold,
               ROUND(COALESCE(SUM(quantity * unit_price), 0), 2) AS gross_sales,
               ROUND(COALESCE(SUM(discount_amount), 0), 2)       AS discounts,
               ROUND(COALESCE(SUM({REVENUE}), 0), 2)             AS net_revenue
        FROM sales WHERE {where}"""
    rows = run(conn, sql, params)
    r = rows[0]
    if r["orders"] == 0:
        raise QueryError("No completed sales match those filters.", code="no_data")
    answer = f"Total revenue{describe_filters(filters)} is {money(r['net_revenue'])}."
    figures = {
        "Gross sales": money(r["gross_sales"]),
        "Discounts": money(r["discounts"]),
        "Net revenue": money(r["net_revenue"]),
        "Orders": r["orders"],
    }
    return _result("total_revenue", answer, sql, params, rows, figures, filters)


def _ranking(conn, plan, catalog, intent, column, label, noun):
    filters = normalise_filters(plan.get("filters"), catalog)
    where, params = build_where(filters)
    sql = f"""
        SELECT {column} AS {label},
               ROUND(SUM({REVENUE}), 2) AS net_revenue,
               COUNT(*)                 AS orders,
               SUM(quantity)            AS units_sold
        FROM sales WHERE {where}
        GROUP BY {column}
        ORDER BY net_revenue DESC, {column} ASC"""
    rows = run(conn, sql, params)
    if not rows:
        raise QueryError("No completed sales match those filters.", code="no_data")
    top = rows[0]
    total = sum(r["net_revenue"] for r in rows)
    share = top["net_revenue"] / total * 100 if total else 0
    tied = [r[label] for r in rows if r["net_revenue"] == top["net_revenue"]]
    if len(tied) > 1:
        answer = f"{' and '.join(tied)} are tied for highest revenue{describe_filters(filters)} at {money(top['net_revenue'])} each."
    else:
        answer = (f"{top[label]} generated the highest revenue{describe_filters(filters)}: "
                  f"{money(top['net_revenue'])} ({share:.1f}% of {money(total)}).")
    figures = {f"Top {noun}": top[label], "Revenue": money(top["net_revenue"]), "Share of total": f"{share:.1f}%"}
    return _result(intent, answer, sql, params, rows, figures, filters)


def top_product(conn, plan, catalog):
    return _ranking(conn, plan, catalog, "top_product", "product", "product", "product")


def top_region(conn, plan, catalog):
    return _ranking(conn, plan, catalog, "top_region", "region", "region", "region")


def monthly_trend(conn, plan, catalog):
    filters = normalise_filters(plan.get("filters"), catalog)
    filters.pop("month", None)  # a trend over a single month makes no sense
    where, params = build_where(filters)
    sql = f"""
        SELECT order_month AS month,
               ROUND(SUM({REVENUE}), 2) AS net_revenue,
               COUNT(*)                 AS orders
        FROM sales WHERE {where}
        GROUP BY order_month
        ORDER BY order_month"""
    rows = run(conn, sql, params)
    if not rows:
        raise QueryError("No completed sales match those filters.", code="no_data")
    prev = None
    for r in rows:  # month-over-month % computed here, not by the LLM
        r["change_vs_prev_month_pct"] = None if prev in (None, 0) else round((r["net_revenue"] - prev) / prev * 100, 1)
        prev = r["net_revenue"]
    best = max(rows, key=lambda r: r["net_revenue"])
    worst = min(rows, key=lambda r: r["net_revenue"])
    first, last = rows[0], rows[-1]
    direction = "up" if last["net_revenue"] > first["net_revenue"] else "down" if last["net_revenue"] < first["net_revenue"] else "flat"
    answer = (f"Monthly revenue{describe_filters(filters)} went from {money(first['net_revenue'])} in {month_label(first['month'])} "
              f"to {money(last['net_revenue'])} in {month_label(last['month'])} ({direction}). "
              f"Peak: {month_label(best['month'])} ({money(best['net_revenue'])}); "
              f"lowest: {month_label(worst['month'])} ({money(worst['net_revenue'])}).")
    figures = {"Peak month": month_label(best["month"]), "Lowest month": month_label(worst["month"]), "Months covered": len(rows)}
    return _result("monthly_trend", answer, sql, params, rows, figures, filters)


def month_comparison(conn, plan, catalog):
    month_a, month_b = plan.get("month_a"), plan.get("month_b")
    if not month_a or not month_b:
        raise QueryError("Please name two months to compare, e.g. 'between February and March'.")
    month_a, month_b = _validate_month(str(month_a), catalog), _validate_month(str(month_b), catalog)
    if month_a == month_b:
        raise QueryError("Please give two different months to compare.")
    filters = normalise_filters(plan.get("filters"), catalog)
    filters.pop("month", None)
    where, params = build_where(filters)
    sql = f"""
        SELECT order_month AS month,
               ROUND(SUM({REVENUE}), 2) AS net_revenue,
               COUNT(*)                 AS orders
        FROM sales WHERE {where} AND order_month IN (?, ?)
        GROUP BY order_month
        ORDER BY order_month"""
    params = params + [month_a, month_b]
    rows = run(conn, sql, params)
    by_month = {r["month"]: r["net_revenue"] for r in rows}
    rev_a, rev_b = by_month.get(month_a, 0.0), by_month.get(month_b, 0.0)
    diff = round(rev_b - rev_a, 2)
    pct = None if rev_a == 0 else round(diff / rev_a * 100, 1)
    verb = "increased" if diff > 0 else "decreased" if diff < 0 else "did not change"
    answer = f"Revenue{describe_filters(filters)} {verb} from {money(rev_a)} in {month_label(month_a)} to {money(rev_b)} in {month_label(month_b)}"
    if diff != 0:
        answer += f", a change of {'+' if diff > 0 else '-'}{money(abs(diff))}" + (f" ({pct:+.1f}%)" if pct is not None else " (no % change: starting month has zero revenue)")
    answer += "."
    figures = {
        month_label(month_a): money(rev_a),
        month_label(month_b): money(rev_b),
        "Absolute change": f"{diff:+,.2f}",
        "% change": "n/a" if pct is None else f"{pct:+.1f}%",
    }
    return _result("month_comparison", answer, sql, params, rows, figures, filters)


# The ONLY intents the system can execute. Anything else is rejected.
INTENTS = {
    "total_revenue": total_revenue,
    "top_product": top_product,
    "monthly_trend": monthly_trend,
    "top_region": top_region,
    "month_comparison": month_comparison,
}


def execute_plan(conn: sqlite3.Connection, plan: dict) -> dict:
    intent = plan.get("intent")
    handler = INTENTS.get(intent)
    if handler is None:
        raise QueryError(
            "I can't answer that from the sales data.", code="unsupported_question",
        )
    catalog = load_catalog(conn)
    result = handler(conn, plan, catalog)
    result["notes"] = data_notes(conn)
    return result
