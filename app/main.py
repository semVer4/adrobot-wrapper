from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.campaigns import router as campaigns
from app.editor import router as editor

app = FastAPI(title="Редактор кампаний")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


@app.middleware("http")
async def local_ui(request: Request, call_next):
    # A different website must not be able to publish using the local API key.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Недопустимый источник запроса."}, status_code=403)
    return await call_next(request)


app.include_router(campaigns)
app.include_router(editor)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(static_dir / "index.html")
