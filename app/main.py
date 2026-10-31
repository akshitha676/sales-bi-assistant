"""FastAPI application: POST /ask, GET /api/meta, GET /api/health, and the web UI."""
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator

from app import config
from app.db import build_database, connect_readonly
from app.llm import build_default_interpreter, looks_destructive
from app.queries import SUPPORTED_QUESTIONS, QueryError, data_notes, execute_plan, load_catalog

log = logging.getLogger("bi.api")
STATIC_DIR = Path(__file__).parent / "static"


class AskRequest(BaseModel):
    question: str

    @field_validator("question")
    @classmethod
    def check_question(cls, v: str) -> str:
        v = " ".join(v.split())  # collapse whitespace/newlines
        if not v:
            raise ValueError("question must not be empty")
        if len(v) > config.MAX_QUESTION_LENGTH:
            raise ValueError(f"question is too long (max {config.MAX_QUESTION_LENGTH} characters)")
        return v


def error_response(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, **extra}})


def create_app(db_path: Optional[Path] = None, csv_path: Optional[Path] = None, interpreter=None) -> FastAPI:
    """App factory so tests can inject a temp database and a fake interpreter."""
    db_path = Path(db_path or config.DB_PATH)
    csv_path = Path(csv_path or config.CSV_PATH)
    app = FastAPI(title="Mini AI BI Assistant")

    # Build the database on first start (or if it was deleted).
    if not db_path.exists():
        report = build_database(csv_path, db_path)
        log.info("Loaded %s rows, rejected %s", report["loaded"], report["rejected"])

    interp = interpreter or build_default_interpreter(config.GEMINI_API_KEY, config.GEMINI_MODEL, config.GEMINI_TIMEOUT_MS)

    # ---------------------------------------------------------- error handlers
    @app.exception_handler(QueryError)
    async def query_error_handler(_: Request, exc: QueryError):
        extra = {"supported_questions": SUPPORTED_QUESTIONS} if exc.code == "unsupported_question" else {}
        return error_response(exc.http_status, exc.code, exc.message, **extra)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        msg = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
        return error_response(422, "invalid_request", msg)

    @app.exception_handler(Exception)
    async def unexpected_handler(_: Request, exc: Exception):
        log.exception("Unhandled error")
        return error_response(500, "internal_error", "Something went wrong on our side. Please try again.")

    # ---------------------------------------------------------------- routes
    @app.post("/ask")
    def ask(body: AskRequest):
        if looks_destructive(body.question):
            raise QueryError(
                "This assistant is read-only. It can answer questions about the sales data but cannot modify it.",
                code="unsupported_question",
            )
        with connect_readonly(db_path) as conn:
            catalog = load_catalog(conn)
            interpretation = interp.interpret(body.question, catalog)
            plan = interpretation.plan
            if plan.get("intent") == "unsupported":
                raise QueryError(
                    plan.get("reason") or "I can't answer that from the sales data.",
                    code="unsupported_question",
                )
            result = execute_plan(conn, plan)
        result.update(
            question=body.question,
            interpreter=interpretation.interpreter,
            plan=plan,
            warnings=interpretation.warnings,
        )
        return result

    @app.get("/api/meta")
    def meta():
        with connect_readonly(db_path) as conn:
            loaded = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
            rejected = [dict(r) for r in conn.execute(
                "SELECT source_line, transaction_id, reason FROM rejected_rows ORDER BY id")]
            return {
                "catalog": load_catalog(conn),
                "rows_loaded": loaded,
                "rows_rejected": rejected,
                "notes": data_notes(conn),
                "supported_questions": SUPPORTED_QUESTIONS,
            }

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app

