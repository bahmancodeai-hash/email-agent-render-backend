from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.database import engine, Base
from app.api.router import api_router
from app.services.background_scheduler import background_scheduler


async def _ensure_runtime_schema() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS remote_id VARCHAR(255)"))
        await conn.execute(text("ALTER TABLE messages ALTER COLUMN remote_id TYPE TEXT"))
        await conn.execute(text("ALTER TABLE messages ALTER COLUMN message_id TYPE TEXT"))
        await conn.execute(text("ALTER TABLE messages ALTER COLUMN thread_id TYPE TEXT"))
        await conn.execute(text("ALTER TABLE messages ALTER COLUMN from_address TYPE TEXT"))
        await conn.execute(text("ALTER TABLE messages ALTER COLUMN from_name TYPE TEXT"))
        await conn.execute(text("ALTER TABLE messages ALTER COLUMN reply_to TYPE TEXT"))
        await conn.execute(text("ALTER TABLE messages ALTER COLUMN in_reply_to TYPE TEXT"))
        await conn.execute(text("ALTER TABLE messages ALTER COLUMN preview TYPE TEXT"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_remote_id ON messages (remote_id)"))


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
