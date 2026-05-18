"""
### Unified FastAPI landing + Marimo notebook app.

Single container, single public port. `uvicorn` serves this package's
`asgi_app` on 0.0.0.0:8080; a lifespan hook spawns `marimo edit` as a
local subprocess on 127.0.0.1:8081 and reverse-proxies authenticated
`/nb/*` traffic (HTTP and WebSocket) to it.

The `app_env` AppEnvironment is also defined here — co-located with
the FastAPI application it deploys.

Local development:
    uvicorn app:asgi_app --reload --port 8080

Deploy hosted to Flyte:
    python -m app

spec: [docs/architecture/landing.md](../docs/architecture/landing.md)
"""

import asyncio
import os
import secrets
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import flyte
import flyte.app
import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.background import BackgroundTask

import stargazer
from stargazer.config import (
    PROJECT_ROOT,
    STARGAZER_ENV_VARS,
    logger,
)

from app.init import init
from app.oauth import exchange_code, get_github_user, github_auth_url
from app.session import (
    SESSION_COOKIE,
    SessionData,
    create_session_cookie,
    read_session_cookie,
)
from app.templates import dashboard_html, login_html


MARIMO_HOST = "127.0.0.1"
MARIMO_PORT = 8081
MARIMO_BASE_URL = "/nb"
NOTEBOOK_PATH = (
    Path(stargazer.__file__).parent / "notebooks" / "tutorials" / "scrna_tutorial.py"
)
# Hop-by-hop headers per RFC 7230 §6.1 — must not be forwarded by proxies.
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}


# ---------------------------------------------------------------------------
# Flyte AppEnvironment — the deployment target for this FastAPI app.
# ---------------------------------------------------------------------------


# Devbox secret-injection workaround: Flyte's secret webhook only fires on
# pods labeled `inject-flyte-secrets: "true"`, which the App service doesn't
# add. So `secrets=[...]` doesn't reach the App container env. We bake values
# from the deployer's local shell into `env_vars` instead. Export the same
# names locally before `python -m app`. For production this needs to be
# replaced with a pod_template that sets the label.
_SECRET_NAMES = (
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "SESSION_SECRET",
    "PINATA_JWT",
)
_RUNTIME_SECRETS = {
    name: os.environ[name] for name in _SECRET_NAMES if os.environ.get(name)
}


app_env = flyte.app.AppEnvironment(
    name="stargazer-app",
    description="Unified FastAPI landing + Marimo notebook UI, one container",
    image=(
        flyte.Image.from_debian_base(
            name="stargazer-app",
            registry=os.environ["STARGAZER_REGISTRY"],
            platform=("linux/amd64", "linux/arm64"),
        )
        .with_apt_packages("ca-certificates", "git")
        .with_uv_project(
            PROJECT_ROOT / "pyproject.toml",
            project_install_mode="install_project",
            extra_args="--extra app",
        )
        .with_commands(["flyte create config --local-persistence"])
    ),
    args=[
        "uvicorn",
        "app:asgi_app",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
    ],
    port=8080,
    requires_auth=False,
    resources=flyte.Resources(memory=("2Gi", "6Gi")),
    env_vars={**STARGAZER_ENV_VARS, **_RUNTIME_SECRETS},
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(key: str) -> str:
    """Read a required environment variable."""
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"Missing required env var: {key}")
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the marimo subprocess on app startup; terminate on shutdown."""
    logger.info(f"Starting marimo subprocess for {NOTEBOOK_PATH}")
    proc = subprocess.Popen(
        [
            "marimo",
            "edit",
            str(NOTEBOOK_PATH),
            "--port",
            str(MARIMO_PORT),
            "--host",
            MARIMO_HOST,
            "--headless",
            "--no-token",
            "--base-url",
            MARIMO_BASE_URL,
            "--allow-origins",
            "*",
        ]
    )
    health_url = f"http://{MARIMO_HOST}:{MARIMO_PORT}{MARIMO_BASE_URL}/health"
    async with httpx.AsyncClient(timeout=2.0) as probe:
        for _ in range(60):
            try:
                r = await probe.get(health_url)
                if r.status_code == 200:
                    logger.info(f"Marimo ready at {health_url}")
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        else:
            proc.terminate()
            raise RuntimeError(
                f"Marimo failed to become healthy at {health_url} within 30s"
            )

    app.state.marimo_proc = proc
    app.state.proxy_client = httpx.AsyncClient(
        base_url=f"http://{MARIMO_HOST}:{MARIMO_PORT}",
        timeout=None,
    )
    try:
        yield
    finally:
        await app.state.proxy_client.aclose()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


asgi_app = FastAPI(
    title="Stargazer", docs_url=None, redoc_url=None, lifespan=lifespan
)


def _redirect_uri(request: Request) -> str:
    """Build the OAuth callback URI."""
    base = os.environ.get("LANDING_BASE_URL")
    if base:
        return f"{base.rstrip('/')}/auth/callback"
    return str(request.url_for("auth_callback"))


def _get_session(request: Request) -> SessionData | None:
    """Extract a valid session from the request cookie, if present."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    return read_session_cookie(cookie, _env("SESSION_SECRET"))


# ---------------------------------------------------------------------------
# OAuth + landing routes
# ---------------------------------------------------------------------------


@asgi_app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Landing page or dashboard depending on session state."""
    session = _get_session(request)
    if session:
        return HTMLResponse(dashboard_html(session.github_username))
    return HTMLResponse(login_html())


@asgi_app.get("/auth/login")
async def auth_login(request: Request):
    """Redirect to GitHub for OAuth authorization."""
    state = secrets.token_urlsafe(32)
    url = github_auth_url(
        client_id=_env("GITHUB_CLIENT_ID"),
        redirect_uri=_redirect_uri(request),
        state=state,
    )
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        "oauth_state",
        state,
        httponly=True,
        secure=False,
        max_age=600,
        samesite="lax",
    )
    return response


@asgi_app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request, code: str, state: str):
    """Handle the GitHub OAuth callback."""
    expected_state = request.cookies.get("oauth_state")
    if not expected_state or not secrets.compare_digest(state, expected_state):
        return RedirectResponse("/", status_code=302)

    access_token = await exchange_code(
        client_id=_env("GITHUB_CLIENT_ID"),
        client_secret=_env("GITHUB_CLIENT_SECRET"),
        code=code,
        redirect_uri=_redirect_uri(request),
    )
    github_user = await get_github_user(access_token)

    session = SessionData(
        github_username=github_user["login"],
        github_id=github_user["id"],
    )
    cookie = create_session_cookie(session, _env("SESSION_SECRET"))
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        cookie,
        httponly=True,
        secure=False,
        max_age=60 * 60 * 24 * 30,
        samesite="lax",
    )
    response.delete_cookie("oauth_state")
    return response


@asgi_app.get("/auth/logout")
async def auth_logout():
    """Clear session and redirect to landing page."""
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@asgi_app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Marimo reverse proxy (/nb/*)
# ---------------------------------------------------------------------------


@asgi_app.api_route(
    "/nb/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy_http(request: Request, path: str):
    """Auth-gated HTTP reverse proxy to the marimo subprocess."""
    if not _get_session(request):
        return RedirectResponse("/auth/login", status_code=302)

    client: httpx.AsyncClient = request.app.state.proxy_client
    upstream_path = f"{MARIMO_BASE_URL}/{path}"
    if request.url.query:
        upstream_path = f"{upstream_path}?{request.url.query}"

    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS
    }
    upstream_req = client.build_request(
        request.method,
        upstream_path,
        headers=headers,
        content=request.stream(),
    )
    upstream_resp = await client.send(upstream_req, stream=True)
    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_HEADERS
    }
    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        background=BackgroundTask(upstream_resp.aclose),
    )


@asgi_app.websocket("/nb/{path:path}")
async def proxy_ws(websocket: WebSocket, path: str):
    """Auth-gated WebSocket reverse proxy to the marimo subprocess."""
    cookie = websocket.cookies.get(SESSION_COOKIE)
    if not cookie or not read_session_cookie(cookie, _env("SESSION_SECRET")):
        await websocket.close(code=4401)
        return

    await websocket.accept()

    upstream_url = f"ws://{MARIMO_HOST}:{MARIMO_PORT}{MARIMO_BASE_URL}/{path}"
    if websocket.url.query:
        upstream_url = f"{upstream_url}?{websocket.url.query}"

    try:
        async with websockets.connect(upstream_url) as upstream:

            async def client_to_upstream():
                """Forward messages from browser → marimo."""
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            await upstream.close()
                            return
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    await upstream.close()

            async def upstream_to_client():
                """Forward messages from marimo → browser."""
                try:
                    async for msg in upstream:
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except websockets.ConnectionClosed:
                    pass
                finally:
                    try:
                        await websocket.close()
                    except RuntimeError:
                        pass

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception as exc:
        logger.error(f"WebSocket proxy error on /nb/{path}: {exc}")
        try:
            await websocket.close()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Deploy entrypoint
# ---------------------------------------------------------------------------


def main():
    """Deploy the unified app to Flyte."""
    init(root_dir=PROJECT_ROOT)
    deployment = flyte.serve(app_env)
    print(f"App URL: {deployment.url}")


if __name__ == "__main__":
    main()
