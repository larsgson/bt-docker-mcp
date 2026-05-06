"""FastAPI app. Mounts REST routes under /api and MCP at /mcp."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from indexer.env import load_env
from server.cors import allowed_origins
from server.routes import ask as ask_route
from server.routes import chunks as chunks_route
from server.routes import health as health_route
from server.routes import search as search_route
from server.routes import trees as trees_route
from server.mcp import server as mcp_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_env()
    yield


app = FastAPI(title="bt-docker-mcp API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=False,
)

# REST surface
app.include_router(health_route.router, prefix="/api")
app.include_router(chunks_route.router, prefix="/api")
app.include_router(search_route.router, prefix="/api")
app.include_router(ask_route.router, prefix="/api")
app.include_router(trees_route.router, prefix="/api")

# MCP surface (mounted at /mcp; not under /api)
app.include_router(mcp_server.router)


@app.get("/")
def root() -> dict:
    return {
        "name": "bt-docker-mcp",
        "version": "2.0.0",
        "endpoints": {
            "rest": "/api/{health,chunk,search,ask,tree}",
            "mcp": "/mcp",
        },
        "docs": {
            "openapi": "/docs",
            "client_integration": "https://github.com/.../docs/client-integration.md",
            "mcp": "https://github.com/.../docs/mcp.md",
        },
    }
