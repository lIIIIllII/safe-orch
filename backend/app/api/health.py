import sqlite3

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.store import db

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    try:
        with db.read() as conn:
            sqlite_version = conn.execute("SELECT sqlite_version()").fetchone()[0]
            schema_version = db.get_schema_version(conn)
    except sqlite3.Error as e:
        return JSONResponse(status_code=503, content={"db": "error", "detail": str(e)})
    return JSONResponse(
        content={
            "db": "connected",
            "sqlite_version": sqlite_version,
            "schema_version": schema_version,
        }
    )
