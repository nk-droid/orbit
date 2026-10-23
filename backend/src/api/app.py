from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .actions import router as actions_router
from .attachments import router as attachments_router
from .charts import router as charts_router
from .deck import router as deck_router
from .files import router as files_router
from .integrations import router as integrations_router
from .jobs import router as jobs_router
from .mail import router as mail_router
from .reminders import router as reminders_router
from .session import router as session_router
from .settings import get_settings
from .sheets import router as sheets_router
from .tokens import router as tokens_router

app = FastAPI(title="Orbit API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().allowed_origins),
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

app.include_router(tokens_router, prefix="/api")
app.include_router(integrations_router, prefix="/api")
app.include_router(deck_router, prefix="/api")
app.include_router(actions_router, prefix="/api")
app.include_router(mail_router, prefix="/api")
app.include_router(files_router, prefix="/api")
app.include_router(sheets_router, prefix="/api")
app.include_router(charts_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(attachments_router, prefix="/api")
app.include_router(reminders_router, prefix="/api")
app.include_router(session_router, prefix="/api")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
