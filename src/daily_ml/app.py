"""Serve the installed UI and protected same-origin local APIs."""

import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .bank import (
    ID_PATTERN, QuestionPrompt, SectionNotFound, SectionSummary, import_bank, list_questions,
    list_sections, validate_bank,
)
from .config import ConfigError, Settings, save_user_settings
from .connection import Connection, ConnectionError, ConnectionStatus, SignInStart
from .database import Database, PreferenceConflict, Preferences
from .grading import GradingContext
from .grading_jobs import GradingJobs, RetryConflict
from .practice import (
    Draft, DraftConflict, DraftInvalid, Practice, PracticeAddition, PracticeItem, PracticeItemNotFound, PracticeToday,
    SubmissionConflict, SupportUnavailable,
)
from .provider import Provider, ProviderFailure
from .scheduling import ReviewState

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


class PracticeChoice(BaseModel):
    """Choose varied practice, a focused section or a specific bank question."""

    model_config = ConfigDict(extra="forbid", strict=True)
    section_id: str | None = Field(default=None, pattern=ID_PATTERN, max_length=64)
    question_id: str | None = Field(default=None, pattern=ID_PATTERN, max_length=64)


class PracticeAction(BaseModel):
    """Validate the stable action key for a retryable practice mutation."""

    model_config = ConfigDict(extra="forbid", strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128)


class DraftEdit(BaseModel):
    """Save the editor text against the last observed draft version."""

    model_config = ConfigDict(extra="forbid", strict=True)
    text: str
    version: int = Field(ge=0, le=2**63 - 1)


class FollowOnEdit(DraftEdit):
    """Edit or submit exact text against the displayed grading request."""

    request_id: UUID = Field(strict=False)


class SubmitAction(DraftEdit):
    """One explicit request to save the current editor text as the original."""

    idempotency_key: str = Field(min_length=1, max_length=128)


class RetryPractice(BaseModel):
    """An explicit later send or clarified answer for the displayed request."""

    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    text: str | None = None
    version: int = Field(default=0, strict=True, ge=0, le=2**63 - 1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class DisputeOpen(BaseModel):
    """Open a challenge against one saved assessed answer."""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: UUID = Field(strict=False)
    objection: str = Field(min_length=1, max_length=1600)


class DisputeResolution(BaseModel):
    """Select an assessed result from one open dispute."""

    model_config = ConfigDict(extra="forbid", strict=True)
    assessment_request_id: UUID = Field(strict=False)


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
        App with protected content, Settings and connection routes.
    """
    if not 1 <= port <= 65535:
        raise LaunchError("The server must have an allocated local port.")
    assets = resolve_assets(assets_dir)
    bank = validate_bank()
    database = Database(settings.data_dir)
    import_bank(database, bank)
    practice = Practice(database, answer_limit=settings.answer_limit, review_intervals=settings.review_intervals)
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
    async def database_failure(request: Request, _error_detail: sqlite3.Error):
        """Keep local database details out of the response and logs."""
        logger.info("Local database operation failed")
        if request.url.path in {"/api/sections", "/api/questions"}:
            return _error(503, "Could not read the question bank. Try again.")
        if request.url.path.startswith("/api/practice/"):
            return _error(503, "Could not access saved practice. Try again.")
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
    async def invalid_request(request: Request, _error_detail: RequestValidationError):
        """Do not echo input in request validation responses."""
        if request.url.path == "/api/questions":
            return _error(422, "Choose a valid section.")
        if request.url.path.startswith("/api/practice/"):
            return _error(422, "The practice request was invalid.")
        return _error(422, "The settings request was invalid.")

    @app.get("/")
    def index() -> FileResponse:
        """Serve the installed application entrypoint."""
        return FileResponse(assets / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/session")
    def browser_session(response: Response) -> dict[str, str]:
        """Bootstrap same-origin request protection for an open browser tab."""
        response.set_cookie(cookie_name, session, httponly=True, samesite="strict")
        return {"csrf_token": csrf}

    @app.exception_handler(SectionNotFound)
    async def section_missing(_request: Request, _error_detail: SectionNotFound):
        """Return a safe error when the selected section is unavailable."""
        return _error(404, "That section is unavailable. Choose another section.")

    @app.get("/api/sections")
    def browse_sections() -> list[SectionSummary]:
        """List saved curriculum sections and active question counts.

        Returns
        -------
        list of SectionSummary
            Ordered section summaries.
        """
        return list_sections(database)

    @app.get("/api/questions")
    def browse_questions(section_id: str = Query(pattern=ID_PATTERN, max_length=64)) -> list[QuestionPrompt]:
        """List active prompts with learner-visible fields only.

        Parameters
        ----------
        section_id : str
            Stable curriculum section ID.

        Returns
        -------
        list of QuestionPrompt
            Prompt, objective and display metadata for each active question.

        Raises
        ------
        SectionNotFound
            If the requested section is unknown.
        """
        return list_questions(database, section_id)

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
            background.add_task(jobs.run_pending, result["id"])
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
        timezone, source = settings.effective_timezone(preferences)
        return {"timezone": timezone, "version": preferences.version, "source": source}

    def practice_timezone() -> str:
        """Read the same effective timezone displayed in Settings."""
        return str(settings_payload(database.get_preferences())["timezone"])

    @app.exception_handler(PracticeItemNotFound)
    async def missing_practice_item(_request: Request, _error_detail: PracticeItemNotFound):
        """Avoid echoing the missing practice identity or question ID."""
        return _error(404, "This question is unavailable. Choose another question.")

    @app.exception_handler(DraftInvalid)
    async def invalid_draft(_request: Request, error: DraftInvalid):
        """Return a safe validation message without answer text."""
        return _error(422, str(error))

    @app.exception_handler(DraftConflict)
    async def draft_conflict(_request: Request, error: DraftConflict):
        """Return the actual saved draft for the local editor's conflict view."""
        return JSONResponse({"detail": str(error), "saved": asdict(error.saved)}, status_code=409)

    @app.exception_handler(SubmissionConflict)
    async def submission_conflict(_request: Request, error: SubmissionConflict):
        """Show a conflict without echoing the competing text."""
        return _error(409, str(error))

    @app.exception_handler(SupportUnavailable)
    async def support_unavailable(_request: Request, error: SupportUnavailable):
        """Explain when the saved question version lacks teaching content."""
        return _error(404, str(error))

    def submission_settings(connection: sqlite3.Connection) -> tuple[str, str | None]:
        """Use the timezone and model preferences in the locked submission snapshot."""
        preferences = database.read_preferences(connection)
        return str(settings_payload(preferences)["timezone"]), model_preferences(preferences)["selected"]

    @app.get("/api/practice/items/{item_id}")
    def saved_practice_item(item_id: UUID) -> PracticeItem:
        """Open the original editor for a saved item, including a retired question."""
        return practice.saved_item(str(item_id))

    @app.get("/api/practice/items/{item_id}/submission")
    def read_submission(item_id: UUID) -> dict | None:
        """Return a practice answer or assessment without starting grading."""
        return practice.submission(str(item_id))

    @app.post("/api/practice/items/{item_id}/help/{kind}")
    def reveal_practice_help(item_id: UUID, kind: Literal["hint", "explanation"]) -> dict[str, str]:
        """Return stored teaching content after its use has been saved."""
        return {"content": practice.view_help(str(item_id), kind)}

    @app.post("/api/practice/items/{item_id}/gap/reveal")
    def reveal_practice_gap(item_id: UUID) -> dict[str, object]:
        """Reveal the gap explanation while keeping the item incomplete."""
        return practice.begin_gap(str(item_id))

    @app.post("/api/practice/items/{item_id}/gap/acknowledge")
    def acknowledge_practice_gap(item_id: UUID, action: PracticeAction) -> dict:
        """Award local completion and update the review date after acknowledgement."""
        result = practice.acknowledge_gap(
            str(item_id), action.idempotency_key,
            resolve_timezone=lambda connection: settings.effective_timezone(database.read_preferences(connection))[0],
        )
        return {"submission": result.submission, "today": result.today}

    @app.post("/api/practice/items/{item_id}/submit", status_code=202)
    def submit_practice(item_id: UUID, action: SubmitAction, request: Request, background: BackgroundTasks) -> dict:
        """Commit the original and its credit before scheduling the grading worker."""
        result = practice.submit(
            str(item_id), action.text, expected_version=action.version, resolve_settings=submission_settings,
        )
        if result.dispatch_id:
            background.add_task(request.app.state.grading.run_pending, result.dispatch_id)
        return {"submission": result.submission, "today": result.today}

    @app.post("/api/practice/items/{item_id}/retry", status_code=202)
    def retry_practice(item_id: UUID, action: RetryPractice, request: Request, background: BackgroundTasks) -> dict:
        """Send or clarify a saved answer only after an explicit recovery action."""
        submission, dispatch_id = practice.retry_submission(
            str(item_id), str(action.request_id), action.text,
            expected_version=action.version, resolve_settings=submission_settings,
        )
        if dispatch_id:
            background.add_task(request.app.state.grading.run_pending, dispatch_id)
        return submission

    @app.put("/api/practice/items/{item_id}/clarification-draft")
    def save_clarification_draft(item_id: UUID, edit: FollowOnEdit) -> Draft:
        """Save exact clarification text locally without requesting grading."""
        return practice.save_clarification_draft(
            str(item_id), str(edit.request_id), edit.text, expected_version=edit.version,
        )

    @app.put("/api/practice/items/{item_id}/revision-draft")
    def save_revision_draft(item_id: UUID, edit: FollowOnEdit) -> Draft:
        """Save the next revision draft without queuing grading."""
        return practice.save_revision_draft(
            str(item_id), str(edit.request_id), edit.text, expected_version=edit.version,
        )

    @app.post("/api/practice/items/{item_id}/revise", status_code=202)
    def revise_practice(item_id: UUID, action: FollowOnEdit, request: Request, background: BackgroundTasks) -> dict:
        """Commit the sole assisted revision and enqueue grading after the commit."""
        submission, dispatch_id = practice.revise(
            str(item_id), str(action.request_id), action.text,
            expected_version=action.version, resolve_settings=submission_settings,
        )
        if dispatch_id:
            background.add_task(request.app.state.grading.run_pending, dispatch_id)
        return submission

    @app.post("/api/practice/items/{item_id}/disputes")
    def open_dispute(item_id: UUID, action: DisputeOpen) -> dict:
        """Store an objection and remove the challenged original evidence from review replay."""
        return practice.open_dispute(str(item_id), str(action.request_id), action.objection)

    @app.post("/api/practice/items/{item_id}/disputes/{dispute_id}/reassess", status_code=202)
    def reassess_dispute(item_id: UUID, dispute_id: UUID, request: Request, background: BackgroundTasks) -> dict:
        """Request an explicit reassessment of the exact saved disputed version."""
        submission, dispatch_id = practice.reassess_dispute(str(item_id), str(dispute_id), resolve_settings=submission_settings)
        if dispatch_id:
            background.add_task(request.app.state.grading.run_pending, dispatch_id)
        return submission

    @app.post("/api/practice/items/{item_id}/disputes/{dispute_id}/resolve")
    def resolve_dispute_route(item_id: UUID, dispute_id: UUID, action: DisputeResolution) -> dict:
        """Accept one available assessment and atomically refresh review eligibility."""
        return practice.resolve_dispute(str(item_id), str(dispute_id), str(action.assessment_request_id))

    @app.post("/api/practice/today")
    def open_today() -> PracticeToday:
        """Prepare and return the current date's saved local practice session."""
        return practice.today(practice_timezone())

    @app.post("/api/practice/reviews/{concept_id}/sooner")
    def review_sooner(
        action: PracticeAction, concept_id: str = PathParam(pattern=ID_PATTERN, max_length=64),
    ) -> ReviewState:
        """Save tomorrow or the existing earlier concept review date."""
        return practice.review_sooner(concept_id, action.idempotency_key, practice_timezone())

    @app.post("/api/practice/items")
    def add_practice(choice: PracticeChoice) -> PracticeAddition:
        """Save or resume a requested extra question in the local session."""
        return practice.add_item(practice_timezone(), section_id=choice.section_id, question_id=choice.question_id)

    @app.put("/api/practice/items/{item_id}/draft")
    def save_draft(item_id: UUID, edit: DraftEdit) -> Draft:
        """Persist a versioned draft and return the committed text and version."""
        return practice.save_draft(str(item_id), edit.text, expected_version=edit.version, timezone=practice_timezone())

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
