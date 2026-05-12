# mcp_server.py
from . import tools, prompts  # noqa: F401
from ._mcp import mcp, LOGGER

import logging
import os
from typing import List

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
# from starlette.requests import Request
from starlette.responses import Response

def _configure_runtime_logging() -> None:
    level_name = os.getenv("NRDS_LOG_LEVEL", "INFO").upper()
    level_value = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # Configure this module logger explicitly so it always prints
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level_value)
    stream_handler.setFormatter(formatter)

    LOGGER.handlers.clear()
    LOGGER.addHandler(stream_handler)
    LOGGER.setLevel(level_value)
    LOGGER.propagate = False

    # Optional: keep related loggers at the same level
    logging.getLogger("nextgen_plugins.chatbox.rest").setLevel(level_value)
    logging.getLogger("mcp").setLevel(level_value)
    logging.getLogger("mcp.server").setLevel(level_value)
    logging.getLogger("mcp.server.lowlevel.server").setLevel(level_value)

    LOGGER.info("Runtime logging configured with level=%s", level_name)


def _parse_allowed_origins() -> List[str]:
    """Read ALLOWED_ORIGINS from env (comma-separated). Defaults to wildcard.

    Production deployments behind a known origin (tethysdash, an MCP playground,
    a custom client) should set this explicitly via the deploy YAML's
    --set-env-vars=ALLOWED_ORIGINS=https://example.com[,https://other.com] to
    tighten the surface. The wildcard default matches the test deployment's
    "no auth, public ingress" posture.
    """
    raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


ALLOWED_ORIGINS = _parse_allowed_origins()
# CORS spec forbids `allow_credentials=True` together with `allow_origins=["*"]`.
# We don't use cookies/Authorization in this MCP server (the SSE endpoint is
# public unauthenticated by design), so drop credentials to allow the wildcard.
ALLOW_CREDENTIALS = ALLOWED_ORIGINS != ["*"]


def _patch_sse_transport_for_cors():
    """Monkey-patch SseServerTransport.handle_post_message to handle OPTIONS.

    MCP SDK v1.26+ validates Content-Type on all requests routed to
    handle_post_message, including CORS preflight OPTIONS (which have no
    Content-Type). This patch intercepts OPTIONS and returns 200 with
    CORS headers before the SDK's validation runs.
    """
    from mcp.server.sse import SseServerTransport

    original_handle = SseServerTransport.handle_post_message

    async def patched_handle(self, scope, receive, send):
        if scope.get("method") == "OPTIONS":
            origin = dict(scope.get("headers", [])).get(b"origin", b"").decode()
            if ALLOWED_ORIGINS == ["*"]:
                allow_origin = "*"
            else:
                allow_origin = origin if origin in ALLOWED_ORIGINS else ""
            headers = {
                "access-control-allow-methods": "GET, POST, OPTIONS",
                "access-control-allow-headers": "content-type, x-csrftoken, authorization",
                "access-control-max-age": "86400",
            }
            if allow_origin:
                headers["access-control-allow-origin"] = allow_origin
            if ALLOW_CREDENTIALS and allow_origin and allow_origin != "*":
                headers["access-control-allow-credentials"] = "true"
            response = Response(status_code=200, headers=headers)
            await response(scope, receive, send)
            return
        await original_handle(self, scope, receive, send)

    SseServerTransport.handle_post_message = patched_handle

_patch_sse_transport_for_cors()


CORS_MIDDLEWARE = [
    Middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=ALLOW_CREDENTIALS,
        allow_methods=["*"],
        allow_headers=["*"],
    ),
]


def main() -> None:
    _configure_runtime_logging()
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "9000"))
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    LOGGER.info("Starting NRDS MCP Server on %s:%d with %s transport", host, port, transport)
    mcp.run(
        transport=transport,
        host=host,
        port=port,
        middleware=CORS_MIDDLEWARE,
    )


if __name__ == "__main__":
    main()