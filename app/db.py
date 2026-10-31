"""CSV -> SQLite loading with validation.

Design: every CSV row is either loaded into `sales` (clean) or written to
`rejected_rows` together with the reason. Nothing is silently dropped, so the
data-quality story can be shown to the user and checked in tests.
"""
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

SCHEMA = """
DROP TABLE IF EXISTS sales;
DROP TABLE IF EXISTS rejected_rows;

CREATE TABLE sales (
    transaction_id  TEXT PRIMARY KEY,
    order_date      TEXT NOT NULL,              -- ISO format YYYY-MM-DD
    order_month     TEXT NOT NULL,              -- YYYY-MM (pre-computed for grouping)
    product         TEXT NOT NULL,
    category        TEXT NOT NULL,
    region          TEXT NOT NULL,
    sales_channel   TEXT NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    unit_price      REAL    NOT NULL CHECK (unit_price > 0),
    discount_amount REAL    NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
    status          TEXT NOT NULL
);
CREATE INDEX idx_sales_month  ON sales(order_month);
CREATE INDEX idx_sales_region ON sales(region);

CREATE TABLE rejected_rows (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_line    INTEGER NOT NULL,            -- line number in the CSV file
    transaction_id TEXT,
    reason         TEXT NOT NULL,
    raw_row        TEXT NOT NULL                -- original row as JSON
);
"""

TEXT_FIELDS = ("Product", "Category", "Region", "Sales_channel", "Status")


def _parse_number(value: str, field: str) -> float:
    value = (value or "").strip()
    if value == "":
        raise ValueError(f"{field} is missing")
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{field} is not a number: {value!r}")


def validate_row(raw: dict, seen_ids: set) -> dict:
    """Return a clean row dict or raise ValueError with a human-readable reason."""
    txn_id = (raw.get("Transaction_Id") or "").strip()
    if not txn_id:
        raise ValueError("Transaction_Id is missing")
    if txn_id in seen_ids:
        raise ValueError(f"duplicate Transaction_Id {txn_id}")

    try:
        order_date = datetime.strptime((raw.get("Order_date") or "").strip(), "%d-%m-%Y")
    except ValueError:
        raise ValueError(f"Order_date is not a valid DD-MM-YYYY date: {raw.get('Order_date')!r}")

    text = {}
    for field in TEXT_FIELDS:
        v = (raw.get(field) or "").strip()
        if not v:
            raise ValueError(f"{field} is missing")
        text[field] = v

    quantity = _parse_number(raw.get("Quantity"), "Quantity")
    if quantity != int(quantity) or quantity <= 0:
        raise ValueError(f"Quantity must be a positive whole number, got {raw.get('Quantity')!r}")

    unit_price = _parse_number(raw.get("Unit_price"), "Unit_price")
    if unit_price <= 0:
        raise ValueError(f"Unit_price must be positive, got {raw.get('Unit_price')!r}")

    # A blank discount is treated as "no discount"; a non-numeric one is an error.
    discount_raw = (raw.get("Discount_amount") or "").strip()
    discount = 0.0 if discount_raw == "" else _parse_number(discount_raw, "Discount_amount")
    if discount < 0:
        raise ValueError("Discount_amount cannot be negative")
    if discount > quantity * unit_price:
        raise ValueError("Discount_amount is larger than the order value")

    return {
        "transaction_id": txn_id,
        "order_date": order_date.strftime("%Y-%m-%d"),
        "order_month": order_date.strftime("%Y-%m"),
        "product": text["Product"],
        "category": text["Category"],
        "region": text["Region"].title(),
        "sales_channel": text["Sales_channel"].title(),
        "quantity": int(quantity),
        "unit_price": unit_price,
        "discount_amount": discount,
        "status": text["Status"].title(),
    }


def build_database(csv_path: Path, db_path: Path) -> dict:
    """(Re)create the SQLite database from the CSV. Returns a load report."""
    csv_path, db_path = Path(csv_path), Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        seen, loaded, rejected = set(), 0, []
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                line = reader.line_num
                try:
                    row = validate_row(raw, seen)
                except ValueError as exc:
                    conn.execute(
                        "INSERT INTO rejected_rows (source_line, transaction_id, reason, raw_row) "
                        "VALUES (?, ?, ?, ?)",
                        (line, (raw.get("Transaction_Id") or "").strip() or None, str(exc), json.dumps(raw)),
                    )
                    rejected.append({"line": line, "transaction_id": raw.get("Transaction_Id"), "reason": str(exc)})
                    continue
                seen.add(row["transaction_id"])
                conn.execute(
                    "INSERT INTO sales VALUES (:transaction_id, :order_date, :order_month, :product, "
                    ":category, :region, :sales_channel, :quantity, :unit_price, :discount_amount, :status)",
                    row,
                )
                loaded += 1
        conn.commit()
    finally:
        conn.close()
    return {"loaded": loaded, "rejected": len(rejected), "rejected_rows": rejected}


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Connection used for answering questions. Two independent write guards:
    the file is opened read-only AND `query_only` is switched on."""
    uri = f"file:{quote(str(Path(db_path).resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


if __name__ == "__main__":  # `python -m app.db` rebuilds the database
    from app import config

    report = build_database(config.CSV_PATH, config.DB_PATH)
    print(f"Loaded {report['loaded']} rows, rejected {report['rejected']}")
    for r in report["rejected_rows"]:
        print(f"  line {r['line']} ({r['transaction_id']}): {r['reason']}")
