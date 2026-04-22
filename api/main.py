"""
UCD Research Platform — FastAPI backend.

Deployed as a Cloud Run service. Serves the chat agent, entity pages,
and page-image citations. The widget.js floating chat script is also
served from here.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="UCD Research Platform", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten to Duda domain(s) before launch
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


# Routers added here as each stage is built:
# from api.routes import chat, projects, firms, widget
# app.include_router(chat.router, prefix="/chat")
# app.include_router(projects.router, prefix="/projects")
# app.include_router(firms.router, prefix="/firms")
# app.include_router(widget.router)
