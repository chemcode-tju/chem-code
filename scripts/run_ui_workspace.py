"""Run this checkout's UI against the existing local application backend."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# Configuration
listen_host = "127.0.0.1"
listen_port = 8772
backend_url = "http://127.0.0.1:8768"
web_directory = Path(__file__).resolve().parents[1] / "src" / "chemistry_agent" / "web"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Share transport connections without starting another computation runtime."""
    timeout = httpx.Timeout(connect=10, read=None, write=None, pool=10)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        application.state.backend = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/assets", StaticFiles(directory=web_directory), name="assets")


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/settings", methods=["GET", "HEAD"])
@app.api_route("/memory", methods=["GET", "HEAD"])
@app.api_route("/structure", methods=["GET", "HEAD"])
@app.api_route("/structures", methods=["GET", "HEAD"])
async def page(request: Request) -> FileResponse:
    filename = {
        "/": "index.html",
        "/settings": "settings.html",
        "/memory": "memory.html",
        "/structure": "structure/index.html",
        "/structures": "structure/index.html",
    }[request.url.path]
    return FileResponse(web_directory / filename, headers={"Cache-Control": "no-cache"})


def forwarded_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Keep cookies and content metadata, excluding connection-specific headers."""
    excluded = {
        b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
        b"te", b"trailer", b"transfer-encoding", b"upgrade",
    }
    for name, value in headers:
        if name.lower() == b"connection":
            excluded.update(token.strip().lower() for token in value.split(b","))
    return [(name, value) for name, value in headers if name.lower() not in excluded]


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def application_request(request: Request, path: str) -> Response:
    """Forward ordinary writes, uploads and unbuffered event streams unchanged."""
    raw_path = request.scope["raw_path"].decode("ascii")
    target = f"{backend_url}{raw_path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    client = request.app.state.backend
    outgoing = client.build_request(
        request.method, target,
        headers=forwarded_headers(request.headers.raw), content=request.stream(),
    )
    try:
        upstream = await client.send(outgoing, stream=True)
    except httpx.HTTPError:
        return JSONResponse({"detail": "本地应用后端暂时无法连接。"}, status_code=502)

    response = StreamingResponse(
        upstream.aiter_raw(), status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose),
    )
    response.raw_headers = forwarded_headers(upstream.headers.raw)
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    uvicorn.run(app, host=listen_host, port=listen_port, timeout_graceful_shutdown=5)
