"""ASGI entry point: `uvicorn app.asgi:app`. Kept separate so importing app.main
(e.g. in tests) has no side effects."""
from app.main import create_app

app = create_app()
