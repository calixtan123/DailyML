"""Serve the installed UI and protected same-origin local APIs."""

import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import ConfigError, Settings, save_user_settings
from .connection import Connection, ConnectionError, ConnectionStatus, SignInStart
from .database import Database, PreferenceConflict, Preferences
from .grading import GradingContext
from .grading_jobs import GradingJobs, RetryConflict
from .provider import Provider, ProviderFailure

logger = logging.getLogger(__name__)
MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}
CONTENT_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class LaunchError(RuntimeError):
    """The local application cannot start with its current setup."""


class TimezoneEdit(BaseModel):
    """Conditionally replace the saved timezone."""

    model_config = ConfigDict(extra="forbid", strict=True)
    timezone: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=0)


class ModelSelection(BaseModel):
    """Validate a selected provider slug for a local Settings action."""

    model_config = ConfigDict(extra="forbid", strict=True)
    model_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._:/-]+$")


class ModelEdit(ModelSelection):
    """Save a model preference against the last observed settings version."""

    version: int = Field(ge=0)


class TestAction(ModelSelection):
    """One explicit reference grading action or recovery."""

    idempotency_key: str = Field(min_length=1, max_length=128)
    retry_of: str | None = Field(default=None, min_length=1, max_length=128)


def resolve_assets(assets_dir: Path | None = None) -> Path:
    """Locate compiled assets in the installed package.

    Parameters
    ----------
    assets_dir : Path or None
        Alternate compiled asset directory for tests.

    Returns
    -------
    Path
        Directory containing the built entrypoint and resources.

    Raises
    ------
    LaunchError
        If the build is incomplete or missing.
    """
    root = assets_dir or Path(__file__).with_name("web")
    if not (root / "index.html").is_file() or not any((root / "assets").glob("*.js")):
        raise LaunchError(
            "Compiled frontend assets are missing. Reinstall the built wheel or run "
            "'npm --prefix frontend ci' and 'npm --prefix frontend run build' before building it."
        )
    return root


def _error(status: int, message: str) -> JSONResponse:
    """Build a redacted API error."""
    return JSONResponse({"detail": message}, status_code=status, headers={"Cache-Control": "no-store"})


def create_app(settings: Settings, *, port: int, assets_dir: Path | None = None) -> FastAPI:
    """Create the application for a socket the launcher already owns.

    Parameters
    ----------
    settings : Settings
        Validated settings for the resolved data directory.
    port : int
        The socket's actual loopback port.
    assets_dir : Path or None
        Optional alternate asset directory for tests.

    Returns
    -------
    FastAPI
        App with protected Settings and connection routes.
    """
    if not 1 <= port <= 65535:
        raise LaunchError("The server must have an allocated local port.")
    assets = resolve_assets(assets_dir)
    database = Database(settings.data_dir)
    host = f"127.0.0.1:{port}"
    origin = f"http://{host}"
    cookie_name = f"daily_ml_session_{port}"
    session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Keep OAuth resources alive exactly as long as the server."""
        app.state.connection = Connection(settings.data_dir)
        provider = Provider(app.state.connection, timeout=settings.request_timeout)
        app.state.grading = GradingJobs(database, provider, settings)
        try:
            app.state.grading.recover_interrupted()
            yield
        finally:
            app.state.connection.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def has_session(request: Request) -> bool:
        """Check the instance-specific HTTP-only browser cookie."""
        return secrets.compare_digest(request.cookies.get(cookie_name, "").encode(), session.encode())

    @app.middleware("http")
    async def protect(request: Request, call_next):
        """Reject untrusted origins and require a protected session for mutations."""
        if request.headers.getlist("host") != [host]:
            return _error(400, "Use the 127.0.0.1 address shown by the Daily ML launcher.")
        origins = request.headers.getlist("origin")
        if origins and origins != [origin]:
            return _error(403, "The request must come from the Daily ML Practice tab.")
        path = request.url.path
        if path.startswith("/api/") and path != "/api/session" and not has_session(request):
            return _error(403, "Reload Daily ML Practice to establish a local session.")
        if request.method in MUTATIONS:
            supplied = request.headers.getlist("x-csrf-token")
            if (
                not has_session(request) or origins != [origin] or len(supplied) != 1
                or not secrets.compare_digest(supplied[0].encode(), csrf.encode())
            ):
                return _error(403, "Reload Daily ML Practice and try again from this tab.")
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ConnectionError)
    async def connection_failure(_request: Request, error: ConnectionError):
        """Return only the connection service's redacted message."""
        return _error(409, str(error))

    @app.exception_handler(sqlite3.Error)
    async def database_failure(_request: Request, _error_detail: sqlite3.Error):
        """Keep local database details out of the response and logs."""
        logger.info("Local preferences database operation failed")
        return _error(503, "Could not access local settings. Try again.")

    @app.exception_handler(PreferenceConflict)
    async def settings_conflict(_request: Request, error: PreferenceConflict):
        """Tell the browser to fetch a fresh preference version."""
        return _error(409, str(error))

    @app.exception_handler(ConfigError)
    async def invalid_setting(_request: Request, error: ConfigError):
        """Return the invalid key without the input value."""
        return _error(422, str(error))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error_detail: RequestValidationError):
        """Do not echo input in request validation responses."""
        return _error(422, "The settings request was invalid.")

    @app.get("/")
    def index() -> FileResponse:
        """Serve the installed Settings entrypoint."""
        return FileResponse(assets / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/session")
    def browser_session(response: Response) -> dict[str, str]:
        """Bootstrap same-origin request protection for an open browser tab."""
        response.set_cookie(cookie_name, session, httponly=True, samesite="strict")
        return {"csrf_token": csrf}

    @app.get("/api/connection")
    def connection_status(request: Request) -> ConnectionStatus:
        """Return only display-safe account and connection status."""
        return request.app.state.connection.get_status()

    @app.post("/api/connection/sign-in")
    def sign_in(request: Request) -> SignInStart:
        """Start a fresh callback listener and return its local browser launch path."""
        return request.app.state.connection.start_sign_in()

    @app.get("/api/connection/authorize/{launch_id}", include_in_schema=False)
    def authorize(request: Request, launch_id: str) -> RedirectResponse:
        """Send the browser to OpenAI once without giving OAuth data to React."""
        return RedirectResponse(request.app.state.connection.authorization_url(launch_id), status_code=303)

    @app.post("/api/connection/cancel")
    def cancel(request: Request) -> ConnectionStatus:
        """Cancel the pending OAuth attempt."""
        return request.app.state.connection.cancel_sign_in()

    @app.post("/api/connection/sign-out")
    def sign_out(request: Request) -> ConnectionStatus:
        """End the ChatGPT session without affecting SQLite."""
        return request.app.state.connection.sign_out()

    @app.exception_handler(ProviderFailure)
    @app.exception_handler(RetryConflict)
    async def grading_failure(_request: Request, error: Exception):
        """Return an allowlisted grading recovery message."""
        return _error(409, str(error))

    model_override = settings.sources["model_id"] in ("installation", "environment")

    def model_preferences(preferences: Preferences) -> dict:
        """Expose effective selection without treating missing catalog entries as available."""
        selected = settings.model_id if model_override else preferences.values.get("model_id")
        return {"selected": selected, "version": preferences.version,
                "source": settings.sources["model_id"] if model_override else "preferences" if selected else "default",
                "overridden": model_override}

    @app.get("/api/connection/models")
    async def account_models(request: Request) -> dict:
        """Fetch the actual signed-in account's ordered model catalog."""
        models = await request.app.state.grading.provider.list_models()
        return {"models": models, **model_preferences(database.get_preferences())}

    @app.put("/api/connection/model")
    async def choose_model(edit: ModelEdit, request: Request) -> dict:
        """Persist a visible account model with the existing conditional preference write."""
        if model_override:
            raise PreferenceConflict("Model selection is overridden by installation configuration.")
        models = await request.app.state.grading.provider.list_models()
        if edit.model_id not in {model.slug for model in models}:
            raise ProviderFailure("model_unavailable")
        values = {**database.get_preferences().values, "model_id": edit.model_id}
        return model_preferences(save_user_settings(settings, values, expected_version=edit.version))

    @app.get("/api/connection/test")
    def reference_test(request: Request) -> dict:
        """Restore the latest result and show the exact fixed input before consent."""
        return {"reference": GradingContext.connection_test(), "latest": request.app.state.grading.latest_connection_test()}

    @app.post("/api/connection/test", status_code=202)
    def start_test(action: TestAction, request: Request, background: BackgroundTasks) -> dict:
        """Persist one explicit connection test before allowing a worker to claim it."""
        if model_override and action.model_id != settings.model_id:
            raise PreferenceConflict("Use the model selected by installation configuration.")
        jobs = request.app.state.grading
        result = jobs.enqueue_connection_test(action.idempotency_key, action.model_id, retry_of=action.retry_of)
        if result["state"] == "queued":
            background.add_task(jobs.run_pending)
        return result

    @app.get("/api/requests/{request_id}", response_model=None)
    def read_request(request_id: str, request: Request):
        """Read stored feedback or recovery state without another model call."""
        return request.app.state.grading.get_request(request_id) or _error(404, "Grading request not found.")

    @app.get("/api/usage")
    def observed_usage(request: Request) -> dict[str, int]:
        """Show the local rolling 24-hour outbound attempt count."""
        return {"attempts_24h": request.app.state.grading.usage_count()}

    def settings_payload(preferences: Preferences) -> dict[str, str | int]:
        """Show the effective timezone, version and override visible to the user."""
        source = settings.sources["timezone"]
        if source in ("installation", "environment"):
            return {"timezone": settings.timezone, "version": preferences.version, "source": source}
        timezone = preferences.values.get("timezone")
        return {
            "timezone": timezone or settings.default_timezone,
            "version": preferences.version,
            "source": "preferences" if timezone else "default",
        }

    @app.get("/api/settings")
    def read_settings() -> dict[str, str | int]:
        """Show effective timezone and provenance."""
        return settings_payload(database.get_preferences())

    @app.put("/api/settings")
    def update_settings(edit: TimezoneEdit) -> dict[str, str | int]:
        """Save the timezone only against the displayed preference version."""
        if settings.sources["timezone"] in ("installation", "environment"):
            raise PreferenceConflict("Timezone is overridden by installation configuration.")
        saved = database.get_preferences()
        values = {**saved.values, "timezone": edit.timezone}
        result = save_user_settings(settings, values, expected_version=edit.version)
        return settings_payload(result)

    app.mount("/assets", StaticFiles(directory=assets / "assets"), name="assets")
    return app
