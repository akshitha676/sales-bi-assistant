"""Central configuration. Everything can be overridden with environment variables
(or a .env file in the project root)."""
import os
from pathlib import Path

try:  # python-dotenv is optional; env vars work without it
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
CSV_PATH = Path(os.getenv("BI_CSV_PATH", BASE_DIR / "data" / "sales.csv"))
DB_PATH = Path(os.getenv("BI_DB_PATH", BASE_DIR / "data" / "sales.db"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Comma-separated list, tried in order. The "-latest" aliases always point at the current
# model, so the app keeps working when Google retires a specific version. Flash-Lite is
# fast and plenty for classifying a question; Flash is the backup.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest,gemini-flash-latest")
GEMINI_TIMEOUT_MS = int(os.getenv("GEMINI_TIMEOUT_MS", "30000"))

MAX_QUESTION_LENGTH = 300
