from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from .middleware._input_validation_middleware import InputValidationEnvelopeMiddleware
from .middleware._observability_middleware import ToolCallObservabilityMiddleware
import logging

# Middleware order:
#   - ToolCallObservabilityMiddleware OUTERMOST so it observes the final
#     envelope after validation middleware has converted ValidationError
#     to a structured tool result.
#   - InputValidationEnvelopeMiddleware INNER so it catches pydantic
#     ValidationError before it bubbles out.
mcp = FastMCP(
    "NRDS MCP Server",
    middleware=[
        ToolCallObservabilityMiddleware(),
        InputValidationEnvelopeMiddleware(),
    ],
)
LOGGER = logging.getLogger("nextgen_mcp.mcp_server")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    """Liveness probe used by Docker HEALTHCHECK and container orchestrators.

    Returns 200 with a minimal payload. Does not exercise downstream
    dependencies (S3, etc.) — keep it cheap so polling stays free.
    """
    return JSONResponse({"status": "ok"})