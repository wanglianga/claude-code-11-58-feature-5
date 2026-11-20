from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.health import router as health_router
from .api.routes import router
from .models import create_all

app = FastAPI(title="轨道交通牵引能耗运行事实与对标平台", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
app.include_router(router)
app.include_router(health_router)


@app.on_event("startup")
def startup():
    create_all()


@app.get("/api/health")
def health():
    return {"status": "ok"}
