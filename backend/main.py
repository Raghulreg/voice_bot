import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from routes.calls import router as calls_router
from routes.conversation_quality import router as conversation_quality_router
from routes.webhooks import router as webhooks_router

env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Create conversation_state table once at startup (IF NOT EXISTS) — then only UPSERT
    try:
        from services.conversation_state_db import ensure_schema

        ensure_schema()
    except Exception:
        logging.getLogger(__name__).exception("Conversation state DB init failed")
    try:
        from services.observability import setup_observability

        setup_observability()
    except Exception:
        logging.getLogger(__name__).exception("Observability init failed")
    yield


app = FastAPI(
    title="Hospital Payer Claims Follow-Up Voice Agent",
    description=(
        "Automates outbound follow-up calls from hospital RCM to health insurance payers — "
        "IVR navigation, provider ID&V, denial code extraction, and RCM resolution logging "
        "via Vapi + Twilio (phone) and ElevenLabs (voice preview), with Gemini helpers"
    ),
    version="2.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(calls_router)
app.include_router(conversation_quality_router)
app.include_router(webhooks_router)


@app.get("/")
async def root():
    return {
        "service": "Hospital Payer Claims Follow-Up Voice Agent",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
        "health": "/api/health",
    }


@app.get("/swagger", include_in_schema=False)
async def swagger_redirect():
    return RedirectResponse(url="/docs")


if __name__ == "__main__":
    import uvicorn

    from config import get_settings

    settings = get_settings()
    backend_dir = Path(__file__).parent
    use_reload = os.getenv("UVICORN_RELOAD", "true").lower() in ("1", "true", "yes")
    port = settings.backend_port
    logging.getLogger(__name__).info(
        "API docs: http://127.0.0.1:%s/docs  (use localhost, not 0.0.0.0)",
        port,
    )
    uvicorn.run(
        "main:app",
        host=settings.backend_host,
        port=port,
        reload=use_reload,
        reload_dirs=[str(backend_dir)] if use_reload else None,
        reload_excludes=["call_data/*", ".env", ".venv/*", "**/__pycache__/*"],
    )
