"""
Configurable FastMCP test server for tool budget and semantic matching testing.

Generates N tools with realistic, semantically varied descriptions across
multiple domains. Supports both full-catalog and search-facade modes.

Usage:
    # Full-catalog mode with 50 tools on port 9002
    python test_large_catalog_server.py --num-tools 50

    # Search-facade mode (BM25) with 30 tools, first 5 pinned
    python test_large_catalog_server.py --num-tools 30 --with-search

    # Custom port
    python test_large_catalog_server.py --num-tools 20 --port 9003
"""

import argparse
import logging
from typing import Optional, List, Dict, Any

from fastmcp import FastMCP
from fastmcp.tools.tool import Tool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response as StarletteResponse

LOGGER = logging.getLogger("test_mcp.large_catalog")

# ---------------------------------------------------------------------------
# CORS setup (mirrors tethysdash_mcp_server.py)
# ---------------------------------------------------------------------------

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
            headers = {
                "access-control-allow-origin": origin or "*",
                "access-control-allow-methods": "GET, POST, OPTIONS",
                "access-control-allow-headers": "content-type, x-csrftoken, authorization",
                "access-control-allow-credentials": "true",
                "access-control-max-age": "86400",
            }
            response = StarletteResponse(status_code=200, headers=headers)
            await response(scope, receive, send)
            return
        await original_handle(self, scope, receive, send)

    SseServerTransport.handle_post_message = patched_handle


_patch_sse_transport_for_cors()

CORS_MIDDLEWARE = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    ),
]

# ---------------------------------------------------------------------------
# Realistic tool catalog (~50 entries across domains)
# ---------------------------------------------------------------------------

TOOL_CATALOG = [
    # --- Weather (5) ---
    {
        "name": "get_current_weather",
        "description": "Get the current weather conditions for a specified city including temperature, humidity, wind speed, and sky conditions.",
        "params": [("city", str, "Name of the city"), ("units", str, "Temperature units: celsius or fahrenheit")],
        "tags": {"weather", "current"},
    },
    {
        "name": "get_weather_forecast",
        "description": "Retrieve a multi-day weather forecast for a location with daily high/low temperatures and precipitation probability.",
        "params": [("city", str, "Name of the city"), ("days", int, "Number of forecast days (1-14)")],
        "tags": {"weather", "forecast"},
    },
    {
        "name": "get_weather_alerts",
        "description": "Check for active severe weather alerts and warnings in a geographic region including storms, floods, and heat advisories.",
        "params": [("region", str, "Geographic region or state code")],
        "tags": {"weather", "alerts"},
    },
    {
        "name": "get_historical_weather",
        "description": "Retrieve historical weather observations for a location on a specific past date including temperature and precipitation records.",
        "params": [("city", str, "City name"), ("date", str, "Date in YYYY-MM-DD format")],
        "tags": {"weather", "historical"},
    },
    {
        "name": "get_air_quality_index",
        "description": "Get the current Air Quality Index (AQI) for a location with pollutant breakdown and health recommendations.",
        "params": [("city", str, "City name")],
        "tags": {"weather", "air-quality"},
    },

    # --- Search & Web (5) ---
    {
        "name": "web_search",
        "description": "Search the web for information using a text query and return ranked results with titles, URLs, and snippets.",
        "params": [("query", str, "Search query text"), ("max_results", int, "Maximum number of results to return")],
        "tags": {"search", "web"},
    },
    {
        "name": "search_images",
        "description": "Search for images matching a query and return URLs with thumbnails, dimensions, and source attribution.",
        "params": [("query", str, "Image search query"), ("size", str, "Image size filter: small, medium, large")],
        "tags": {"search", "images"},
    },
    {
        "name": "search_news",
        "description": "Search recent news articles by topic or keyword and return headlines, publication dates, and source information.",
        "params": [("topic", str, "News topic or keyword"), ("days_back", int, "How many days back to search")],
        "tags": {"search", "news"},
    },
    {
        "name": "search_academic_papers",
        "description": "Search academic databases for research papers by topic, author, or DOI with citation counts and abstracts.",
        "params": [("query", str, "Academic search query"), ("field", str, "Academic field filter")],
        "tags": {"search", "academic"},
    },
    {
        "name": "search_code_repositories",
        "description": "Search public code repositories for projects matching keywords, language, and license criteria.",
        "params": [("query", str, "Code search query"), ("language", str, "Programming language filter")],
        "tags": {"search", "code"},
    },

    # --- Data & Database (5) ---
    {
        "name": "query_database",
        "description": "Execute a read-only SQL query against a connected database and return the result set as structured data.",
        "params": [("sql", str, "SQL SELECT query to execute"), ("database", str, "Database name or connection identifier")],
        "tags": {"data", "database"},
    },
    {
        "name": "get_dataset_statistics",
        "description": "Calculate summary statistics for a dataset including mean, median, standard deviation, min, max, and percentiles.",
        "params": [("dataset_id", str, "Dataset identifier"), ("columns", str, "Comma-separated column names")],
        "tags": {"data", "statistics"},
    },
    {
        "name": "export_data",
        "description": "Export a dataset or query results to a specified format such as CSV, JSON, Parquet, or Excel.",
        "params": [("dataset_id", str, "Dataset identifier"), ("format", str, "Export format: csv, json, parquet, xlsx")],
        "tags": {"data", "export"},
    },
    {
        "name": "import_csv_data",
        "description": "Import data from a CSV file into a database table with automatic type detection and schema inference.",
        "params": [("file_path", str, "Path to the CSV file"), ("table_name", str, "Target table name")],
        "tags": {"data", "import"},
    },
    {
        "name": "validate_data_quality",
        "description": "Run data quality checks on a dataset including null counts, duplicate detection, type consistency, and range validation.",
        "params": [("dataset_id", str, "Dataset identifier"), ("rules", str, "Validation rules in JSON format")],
        "tags": {"data", "quality"},
    },

    # --- File Operations (5) ---
    {
        "name": "read_file",
        "description": "Read the contents of a file from the filesystem and return it as text with encoding detection.",
        "params": [("path", str, "File path to read")],
        "tags": {"files", "read"},
    },
    {
        "name": "write_file",
        "description": "Write text content to a file, creating it if it does not exist or overwriting if it does.",
        "params": [("path", str, "File path to write"), ("content", str, "Text content to write")],
        "tags": {"files", "write"},
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories in a directory with optional filtering by extension and recursive traversal.",
        "params": [("path", str, "Directory path"), ("pattern", str, "Glob pattern filter")],
        "tags": {"files", "directory"},
    },
    {
        "name": "compress_files",
        "description": "Compress one or more files into a ZIP or TAR archive with optional compression level control.",
        "params": [("paths", str, "Comma-separated file paths"), ("archive_name", str, "Output archive filename")],
        "tags": {"files", "compression"},
    },
    {
        "name": "convert_file_format",
        "description": "Convert a file between formats such as Markdown to HTML, CSV to JSON, or YAML to JSON.",
        "params": [("input_path", str, "Source file path"), ("output_format", str, "Target format: html, json, yaml, csv")],
        "tags": {"files", "conversion"},
    },

    # --- User Management (5) ---
    {
        "name": "get_user_profile",
        "description": "Retrieve a user's profile information including name, email, role, and account creation date.",
        "params": [("user_id", str, "User identifier")],
        "tags": {"users", "profile"},
    },
    {
        "name": "list_users",
        "description": "List all users in the system with optional filtering by role, status, and registration date range.",
        "params": [("role", str, "Filter by role"), ("status", str, "Filter by status: active, inactive, suspended")],
        "tags": {"users", "list"},
    },
    {
        "name": "update_user_permissions",
        "description": "Update the permission set for a user account, granting or revoking access to specific resources.",
        "params": [("user_id", str, "User identifier"), ("permissions", str, "JSON array of permission strings")],
        "tags": {"users", "permissions"},
    },
    {
        "name": "create_user_account",
        "description": "Create a new user account with the specified name, email, role, and initial password configuration.",
        "params": [("name", str, "Full name"), ("email", str, "Email address"), ("role", str, "User role")],
        "tags": {"users", "create"},
    },
    {
        "name": "audit_user_activity",
        "description": "Retrieve an audit log of user actions within a date range including logins, changes, and API calls.",
        "params": [("user_id", str, "User identifier"), ("start_date", str, "Start date YYYY-MM-DD"), ("end_date", str, "End date YYYY-MM-DD")],
        "tags": {"users", "audit"},
    },

    # --- Analytics (5) ---
    {
        "name": "generate_report",
        "description": "Generate an analytics report for a specified metric and time period with aggregation and trend analysis.",
        "params": [("metric", str, "Metric name"), ("period", str, "Time period: daily, weekly, monthly")],
        "tags": {"analytics", "reports"},
    },
    {
        "name": "get_dashboard_metrics",
        "description": "Fetch key performance indicators for a dashboard including active users, conversion rates, and revenue.",
        "params": [("dashboard_id", str, "Dashboard identifier")],
        "tags": {"analytics", "dashboard"},
    },
    {
        "name": "run_ab_test_analysis",
        "description": "Analyze A/B test results computing statistical significance, conversion lift, and confidence intervals.",
        "params": [("experiment_id", str, "Experiment identifier")],
        "tags": {"analytics", "testing"},
    },
    {
        "name": "calculate_retention_cohorts",
        "description": "Calculate user retention cohort tables showing day-1 through day-30 retention by signup week.",
        "params": [("start_date", str, "Cohort start date"), ("cohort_size", str, "Cohort grouping: day, week, month")],
        "tags": {"analytics", "retention"},
    },
    {
        "name": "track_custom_event",
        "description": "Record a custom analytics event with arbitrary properties for funnel analysis and user behavior tracking.",
        "params": [("event_name", str, "Event name"), ("properties", str, "JSON object of event properties")],
        "tags": {"analytics", "events"},
    },

    # --- Messaging & Communication (5) ---
    {
        "name": "send_email",
        "description": "Send an email message to one or more recipients with subject, body, and optional attachments.",
        "params": [("to", str, "Recipient email addresses"), ("subject", str, "Email subject line"), ("body", str, "Email body text")],
        "tags": {"messaging", "email"},
    },
    {
        "name": "send_slack_message",
        "description": "Post a message to a Slack channel or direct message with optional formatting and thread reply.",
        "params": [("channel", str, "Slack channel name or ID"), ("message", str, "Message text")],
        "tags": {"messaging", "slack"},
    },
    {
        "name": "send_sms",
        "description": "Send an SMS text message to a phone number with delivery confirmation tracking.",
        "params": [("phone_number", str, "Recipient phone number"), ("message", str, "SMS message text")],
        "tags": {"messaging", "sms"},
    },
    {
        "name": "create_notification",
        "description": "Create an in-app notification for a user or group with priority level and expiration settings.",
        "params": [("user_id", str, "Target user ID"), ("title", str, "Notification title"), ("priority", str, "Priority: low, normal, high, urgent")],
        "tags": {"messaging", "notifications"},
    },
    {
        "name": "list_message_threads",
        "description": "List conversation threads for a user or channel with message counts and last activity timestamps.",
        "params": [("channel_id", str, "Channel or conversation ID")],
        "tags": {"messaging", "threads"},
    },

    # --- Scheduling & Calendar (5) ---
    {
        "name": "create_calendar_event",
        "description": "Create a new calendar event with title, start/end times, location, and optional attendee invitations.",
        "params": [("title", str, "Event title"), ("start_time", str, "Start time ISO 8601"), ("end_time", str, "End time ISO 8601")],
        "tags": {"scheduling", "calendar"},
    },
    {
        "name": "list_upcoming_events",
        "description": "List upcoming calendar events within a date range with filtering by calendar and attendee.",
        "params": [("days_ahead", int, "Number of days to look ahead")],
        "tags": {"scheduling", "calendar"},
    },
    {
        "name": "schedule_recurring_task",
        "description": "Create a recurring task with a cron-like schedule expression and automatic execution configuration.",
        "params": [("task_name", str, "Task name"), ("schedule", str, "Cron expression"), ("command", str, "Command to execute")],
        "tags": {"scheduling", "tasks"},
    },
    {
        "name": "set_reminder",
        "description": "Set a personal reminder at a specific time with a message and optional repeat frequency.",
        "params": [("message", str, "Reminder message"), ("remind_at", str, "Reminder time ISO 8601")],
        "tags": {"scheduling", "reminders"},
    },
    {
        "name": "check_availability",
        "description": "Check calendar availability for a list of people and suggest common free time slots.",
        "params": [("attendees", str, "Comma-separated email addresses"), ("date", str, "Target date YYYY-MM-DD")],
        "tags": {"scheduling", "availability"},
    },

    # --- DevOps & Infrastructure (5) ---
    {
        "name": "deploy_application",
        "description": "Deploy an application to a target environment with version control, rollback support, and health checks.",
        "params": [("app_name", str, "Application name"), ("environment", str, "Target: staging, production"), ("version", str, "Version tag to deploy")],
        "tags": {"devops", "deployment"},
    },
    {
        "name": "check_service_health",
        "description": "Check the health status of a deployed service including uptime, response time, and error rate metrics.",
        "params": [("service_name", str, "Service name or URL")],
        "tags": {"devops", "monitoring"},
    },
    {
        "name": "view_application_logs",
        "description": "Retrieve recent application logs with filtering by severity level, time range, and keyword search.",
        "params": [("app_name", str, "Application name"), ("severity", str, "Log level: debug, info, warn, error"), ("lines", int, "Number of log lines")],
        "tags": {"devops", "logging"},
    },
    {
        "name": "manage_environment_variables",
        "description": "List, set, or delete environment variables for a deployed application across environments.",
        "params": [("app_name", str, "Application name"), ("action", str, "Action: list, set, delete"), ("key", str, "Variable name")],
        "tags": {"devops", "config"},
    },
    {
        "name": "scale_service",
        "description": "Adjust the number of running instances for a service with auto-scaling thresholds and resource limits.",
        "params": [("service_name", str, "Service to scale"), ("replicas", int, "Target number of instances")],
        "tags": {"devops", "scaling"},
    },

    # --- Geospatial & Maps (5) ---
    {
        "name": "geocode_address",
        "description": "Convert a street address into geographic coordinates (latitude and longitude) with accuracy confidence.",
        "params": [("address", str, "Street address to geocode")],
        "tags": {"geo", "geocoding"},
    },
    {
        "name": "reverse_geocode",
        "description": "Convert latitude/longitude coordinates into a human-readable address with administrative boundaries.",
        "params": [("latitude", float, "Latitude coordinate"), ("longitude", float, "Longitude coordinate")],
        "tags": {"geo", "geocoding"},
    },
    {
        "name": "calculate_route",
        "description": "Calculate the optimal route between two locations with distance, duration, and turn-by-turn directions.",
        "params": [("origin", str, "Starting address or coordinates"), ("destination", str, "Ending address or coordinates")],
        "tags": {"geo", "routing"},
    },
    {
        "name": "find_nearby_places",
        "description": "Search for points of interest near a location by category such as restaurants, gas stations, or hospitals.",
        "params": [("latitude", float, "Center latitude"), ("longitude", float, "Center longitude"), ("category", str, "Place category")],
        "tags": {"geo", "places"},
    },
    {
        "name": "get_elevation_data",
        "description": "Retrieve elevation data for a geographic point or along a path for terrain analysis and profiling.",
        "params": [("latitude", float, "Latitude"), ("longitude", float, "Longitude")],
        "tags": {"geo", "elevation"},
    },

    # --- Math & Computation (5) ---
    {
        "name": "evaluate_expression",
        "description": "Evaluate a mathematical expression with support for variables, functions, and symbolic computation.",
        "params": [("expression", str, "Mathematical expression to evaluate")],
        "tags": {"math", "calculation"},
    },
    {
        "name": "solve_equation",
        "description": "Solve algebraic equations symbolically or numerically with step-by-step solution display.",
        "params": [("equation", str, "Equation to solve"), ("variable", str, "Variable to solve for")],
        "tags": {"math", "algebra"},
    },
    {
        "name": "generate_random_sample",
        "description": "Generate random samples from statistical distributions: normal, uniform, poisson, exponential, etc.",
        "params": [("distribution", str, "Distribution type"), ("size", int, "Sample size"), ("params", str, "Distribution parameters as JSON")],
        "tags": {"math", "statistics"},
    },
    {
        "name": "run_regression_analysis",
        "description": "Perform linear or polynomial regression analysis on a dataset and return coefficients, R-squared, and residuals.",
        "params": [("dataset_id", str, "Dataset identifier"), ("x_column", str, "Independent variable column"), ("y_column", str, "Dependent variable column")],
        "tags": {"math", "regression"},
    },
    {
        "name": "convert_units",
        "description": "Convert a value between measurement units such as meters to feet, Celsius to Fahrenheit, or kilograms to pounds.",
        "params": [("value", float, "Numeric value to convert"), ("from_unit", str, "Source unit"), ("to_unit", str, "Target unit")],
        "tags": {"math", "conversion"},
    },
]


# ---------------------------------------------------------------------------
# Dynamic tool generation
# ---------------------------------------------------------------------------

# Type mapping for building function signatures via exec()
_TYPE_MAP = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
}


def _make_tool_function(name: str, description: str, params: list):
    """Create a function with explicit typed parameters for FastMCP.

    Uses exec() to produce a function with a proper signature since
    FastMCP requires explicitly typed parameters (no **kwargs).
    """
    # Build parameter list for the function signature
    param_strs = []
    for pname, ptype, pdesc in params:
        type_str = _TYPE_MAP.get(ptype, "str")
        param_strs.append(f"{pname}: {type_str} = ''")

    params_sig = ", ".join(param_strs) if param_strs else ""

    # Build the function body — returns a mock response dict
    param_names = [p[0] for p in params]
    body_dict_items = ", ".join(f'"{p}": {p}' for p in param_names)
    body_dict = f"{{{body_dict_items}}}" if body_dict_items else "{}"

    func_code = f"""
def {name}({params_sig}) -> dict:
    \"\"\"{description}\"\"\"
    return {{"tool": "{name}", "status": "mock_success", "params": {body_dict}}}
"""
    local_ns: Dict[str, Any] = {}
    exec(func_code, {}, local_ns)
    return local_ns[name]


def _generate_padded_tool(index: int) -> dict:
    """Generate a synthetic tool entry for padding beyond catalog size."""
    domains = [
        ("data_processing", "Process and transform data", "data"),
        ("api_integration", "Integrate with external API", "integration"),
        ("content_management", "Manage content resources", "content"),
        ("workflow_automation", "Automate workflow steps", "automation"),
        ("monitoring_alerting", "Monitor systems and send alerts", "monitoring"),
    ]
    domain_name, domain_desc, domain_tag = domains[index % len(domains)]
    variant = index // len(domains)

    return {
        "name": f"{domain_name}_v{variant}",
        "description": f"{domain_desc} endpoint variant {variant} with configurable parameters for batch operations and streaming output.",
        "params": [
            ("input_data", str, "Input data or identifier"),
            ("options", str, "Configuration options as JSON"),
        ],
        "tags": {domain_tag, "generated"},
    }


def build_server(num_tools: int, with_search: bool, port: int) -> FastMCP:
    """Build a FastMCP server with the requested number of tools.

    Args:
        num_tools: Number of tools to register.
        with_search: If True, wrap with BM25SearchTransform (search-facade mode).
        port: Port number (stored for logging, actual binding happens in mcp.run).

    Returns:
        Configured FastMCP server instance.
    """
    transforms = []
    if with_search:
        from fastmcp.server.transforms.search import BM25SearchTransform

        # Pin the first 5 catalog tools as always_visible
        pinned_names = [entry["name"] for entry in TOOL_CATALOG[:5]]
        transforms.append(
            BM25SearchTransform(
                always_visible=pinned_names,
            )
        )

    mcp = FastMCP(
        "Test Large Catalog Server",
        transforms=transforms if transforms else None,
    )

    # Select tools from catalog, pad if needed
    tool_entries = []
    for i in range(num_tools):
        if i < len(TOOL_CATALOG):
            tool_entries.append(TOOL_CATALOG[i])
        else:
            tool_entries.append(_generate_padded_tool(i - len(TOOL_CATALOG)))

    # Register each tool
    for entry in tool_entries:
        fn = _make_tool_function(entry["name"], entry["description"], entry["params"])
        tool = Tool.from_function(
            fn,
            name=entry["name"],
            description=entry["description"],
            tags=entry.get("tags"),
        )
        mcp.add_tool(tool)

    LOGGER.info(
        f"Registered {num_tools} tools (catalog: {min(num_tools, len(TOOL_CATALOG))}, "
        f"generated: {max(0, num_tools - len(TOOL_CATALOG))})"
    )
    if with_search:
        LOGGER.info(
            f"BM25SearchTransform enabled with pinned tools: "
            f"{[e['name'] for e in TOOL_CATALOG[:5]]}"
        )

    return mcp


def parse_args():
    parser = argparse.ArgumentParser(
        description="Configurable FastMCP test server for tool budget testing"
    )
    parser.add_argument(
        "--num-tools",
        type=int,
        default=50,
        help="Number of tools to register (default: 50)",
    )
    parser.add_argument(
        "--with-search",
        action="store_true",
        help="Enable BM25SearchTransform (search-facade mode with pinned tools)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9002,
        help="SSE transport port (default: 9002)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    args = parse_args()
    LOGGER.info(
        f"Starting Test Large Catalog Server on 0.0.0.0:{args.port} "
        f"with {args.num_tools} tools (search={args.with_search})"
    )

    server = build_server(args.num_tools, args.with_search, args.port)
    server.run(
        transport="sse",
        host="0.0.0.0",
        port=args.port,
        middleware=CORS_MIDDLEWARE,
    )
