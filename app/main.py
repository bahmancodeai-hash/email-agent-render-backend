from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.database import engine, Base
from app.api.router import api_router
from app.services.background_scheduler import background_scheduler

logger = logging.getLogger(__name__)


async def _ensure_runtime_schema() -> None:
    columns = (
        "remote_id",
        "message_id",
        "thread_id",
        "from_address",
        "from_name",
        "reply_to",
        "in_reply_to",
        "preview",
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL statement_timeout = '8s'"))
            for column in columns:
                data_type = await conn.scalar(text("""
                    SELECT data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'messages'
                      AND column_name = :column
                """), {"column": column})
                if data_type is None:
                    await conn.execute(text(f"ALTER TABLE messages ADD COLUMN {column} TEXT"))
                elif data_type != "text":
                    await conn.execute(text(f"ALTER TABLE messages ALTER COLUMN {column} TYPE TEXT"))

            index_exists = await conn.scalar(text("""
                SELECT 1
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = 'messages'
                  AND indexname = 'ix_messages_remote_id'
            """))
            if not index_exists:
                await conn.execute(text("CREATE INDEX ix_messages_remote_id ON messages (remote_id)"))
    except Exception as exc:
        logger.warning("Runtime schema check skipped: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_create_tables:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    await _ensure_runtime_schema()
    await background_scheduler.start()
    try:
        yield
    finally:
        await background_scheduler.stop()


app = FastAPI(
    title="Email Agent API",
    description="Cross-platform email agent for 100+ accounts",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}
