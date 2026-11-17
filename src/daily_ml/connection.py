"""Manage the local ChatGPT connection, browser sign-in and session renewal."""

import base64
import hashlib
import logging
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

import httpx

from .credentials import (
    CLIENT_ID, PLAN_SCOPE, CredentialStore, Session, StorageError,
)
from .openai_oauth import (
    AUTHORIZE, DYNAMIC_CLIENT, RESOURCE, SCOPES, TERMINAL_REFRESH, OAuthError, OpenAIOAuth,
)

APP_NAME = "Daily ML Practice"
CALLBACK_PATH = "/auth/callback"
CALLBACK_TIMEOUT = 180
REFRESH_WINDOW = 60
ConnectionState = Literal[
    "signed_out", "signing_in", "connected", "reconnect", "declined", "cancelled", "timeout", "error",
]
logger = logging.getLogger(__name__)


class ConnectionError(RuntimeError):
    """A redacted connection failure safe for local display."""


@dataclass(frozen=True)
class ConnectionStatus:
    """Public connection information, without OAuth credentials."""

    state: ConnectionState
    email: str | None = None
    plan_authorized: bool = False
    message: str | None = None
    remote_revocation_unconfirmed: bool = False
    requires_consent: bool = False


@dataclass(frozen=True)
class SignInStart:
    """One-time local browser launch path. Authorization parameters stay server-side."""

    launch_path: str


@dataclass
class _Attempt:
    server: "_CallbackServer"
    client_id: str
    generation: UUID
    host_id: str
    consent: bool
    deadline: float
    launch_id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(64), repr=False)
    cancelled: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    launched: bool = False
    query: dict[str, str] | None = field(default=None, repr=False)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{CALLBACK_PATH}"


class _CallbackServer(HTTPServer):
    """Handle one pending callback on an already-bound loopback port."""

    attempt: _Attempt
    timeout = 0.1
    allow_reuse_address = False

    def __init__(self, address, handler):
        self._socket_lock = threading.Lock()
        self._active: socket.socket | None = None
        super().__init__(address, handler)

    def get_request(self):
        """Bound the wait on a socket opened by the browser."""
        connection, address = super().get_request()
        connection.settimeout(1)
        with self._socket_lock:
            self._active = connection
            if self.attempt.cancelled.is_set():
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
        return connection, address

    def shutdown_request(self, request):
        """Stop tracking the browser socket before it is closed."""
        with self._socket_lock:
            self._active = None
        super().shutdown_request(request)

    def interrupt(self) -> None:
        """Wake a callback read or write when the attempt is cancelled."""
        with self._socket_lock:
            if self._active is not None:
                with suppress(OSError):
                    self._active.shutdown(socket.SHUT_RDWR)

    def handle_error(self, request, client_address):
        """Suppress request traces, which may contain OAuth parameters."""
        logger.info("ChatGPT callback connection ended before delivery")


class _CallbackHandler(BaseHTTPRequestHandler):
    """Accept the exact path, Host and one-time OAuth state."""

    server: _CallbackServer

    def log_message(self, format, *args):
        """Keep all callback URLs and query parameters out of operational logs."""

    def send_error(self, code, message=None, explain=None):
        """Return a fixed message for malformed or unsupported requests."""
        self._reply(code, "This sign-in callback request could not be handled.")

    def _reply(self, status: int, message: str) -> None:
        """Send a fixed response that never echoes callback parameters."""
        body = f"<!doctype html><html lang=en><title>{APP_NAME}</title><p>{message}</p></html>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Validate and accept one callback result before closing the listener."""
        attempt = self.server.attempt
        if self.headers.get_all("Host") != [f"127.0.0.1:{self.server.server_port}"]:
            self._reply(400, "This sign-in callback address is invalid.")
            return
        try:
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True, max_num_fields=16, errors="strict")
        except ValueError:
            self._reply(400, "This sign-in callback is invalid.")
            return
        if target.path != CALLBACK_PATH or target.netloc or target.scheme or target.fragment:
            self._reply(404, "Return to Daily ML Practice to check your connection.")
            return
        state = query.get("state", [])
        if (
            not attempt.launched
            or attempt.cancelled.is_set()
            or time.monotonic() >= attempt.deadline
            or len(state) != 1
            or not secrets.compare_digest(state[0].encode(), attempt.state.encode())
            or any(len(values) != 1 for values in query.values())
        ):
            self._reply(400, "This sign-in callback could not be verified.")
            return
        attempt.query = {key: values[0] for key, values in query.items()}
        self._reply(200, "Return to Daily ML Practice Settings to see the sign-in result.")


class Connection:
    """Coordinate one selected account and transient, cancellable browser sign-ins."""

    def __init__(
        self, data_dir: Path, *, http: httpx.Client | None = None, callback_timeout: float = CALLBACK_TIMEOUT
    ):
        self.data_dir = data_dir
        self._store = CredentialStore(data_dir)
        self._http = http
        self._callback_timeout = callback_timeout
        self._mutex = threading.Lock()
        self._attempt: _Attempt | None = None
        self._workers: set[threading.Thread] = set()
        self._stopped = threading.Event()
        self._notice: tuple[UUID, ConnectionState, str] | None = None
        self._retry_client: tuple[UUID, str] | None = None

    def _read(self) -> Session:
        """Load the latest atomic session snapshot with a public error."""
        try:
            return self._store.read()
        except StorageError as error:
            raise ConnectionError(str(error)) from None

    def start_sign_in(self) -> SignInStart:
        """Start a temporary loopback listener and prepare a fresh authorization.

        Returns
        -------
        SignInStart
            One-time path to open in the browser on the application's own origin.

        Raises
        ------
        ConnectionError
            If sign-in is already active or the callback cannot be prepared.
        """
        if self._stopped.is_set():
            raise ConnectionError("Daily ML Practice is stopping.")
        try:
            host_id = self._store.host_id()
        except StorageError as error:
            raise ConnectionError(str(error)) from None
        with self._mutex:
            if self._stopped.is_set() or self._attempt is not None or self._workers:
                raise ConnectionError("A sign-in is still finishing. Try again in a moment.")
            session = self._read()
            client = DYNAMIC_CLIENT
            if session.account and not session.client_rejected:
                client = session.account.client_id
            if not session.account and self._retry_client and self._retry_client[0] == session.generation:
                client = self._retry_client[1]
            try:
                server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
            except OSError:
                raise ConnectionError("Could not open a local ChatGPT callback port. Try again.") from None
            attempt = _Attempt(
                server, client, session.generation, host_id,
                session.requires_consent and client != DYNAMIC_CLIENT,
                time.monotonic() + self._callback_timeout,
            )
            server.attempt = attempt
            self._attempt = attempt
            self._notice = None
            worker = threading.Thread(target=self._listen, args=(attempt,), name="chatgpt-callback")
            self._workers.add(worker)
            worker.start()
            return SignInStart(f"/api/connection/authorize/{attempt.launch_id}")

    def authorization_url(self, launch_id: str) -> str:
        """Consume a launch handle and build the browser's trusted OAuth redirect.

        Parameters
        ----------
        launch_id : str
            Opaque handle returned by start_sign_in.

        Returns
        -------
        str
            OpenAI authorization URL for a top-level browser redirect.

        Raises
        ------
        ConnectionError
            If the handle is invalid, expired or already consumed.
        """
        with self._mutex:
            attempt = self._attempt
            if (
                attempt is None or attempt.launched or time.monotonic() >= attempt.deadline
                or not secrets.compare_digest(launch_id.encode(), attempt.launch_id.encode())
            ):
                raise ConnectionError("This sign-in link has expired. Return to Settings to retry.")
            attempt.launched = True
            params = {
                "client_id": attempt.client_id, "ext_agent_host_id": attempt.host_id,
                "response_type": "code", "redirect_uri": attempt.redirect_uri, "scope": SCOPES,
                "resource": RESOURCE, "state": attempt.state, "nonce": attempt.nonce,
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(
                    hashlib.sha256(attempt.verifier.encode()).digest()
                ).decode().rstrip("="),
            }
            if attempt.client_id == DYNAMIC_CLIENT:
                params["agent_name_hint"] = APP_NAME
            if attempt.consent:
                params["prompt"] = "consent"
            return f"{AUTHORIZE}?{urlencode(params)}"

    def _listen(self, attempt: _Attempt) -> None:
        """Close the listener before exchange and publish only this attempt's result."""
        try:
            with attempt.server:
                while (
                    not attempt.cancelled.is_set() and attempt.query is None
                    and time.monotonic() < attempt.deadline
                ):
                    attempt.server.handle_request()
            if attempt.cancelled.is_set():
                return
            if attempt.query is None:
                self._finish(attempt, "timeout", "Sign-in timed out. Start a new sign-in to continue.")
            elif attempt.query.get("error") == "access_denied":
                if attempt.client_id != DYNAMIC_CLIENT:
                    client = attempt.query.get("client_id", attempt.client_id)
                    if client != attempt.client_id:
                        raise ConnectionError("ChatGPT returned a denial for a different registration.")
                    self._mark_recovery(attempt, client)
                self._finish(attempt, "declined", "Authorization was declined in ChatGPT.")
            else:
                self._exchange(attempt, attempt.query)
        except (OAuthError, StorageError, ConnectionError) as error:
            if not attempt.cancelled.is_set():
                logger.info("ChatGPT sign-in could not be completed")
                self._finish(attempt, "error", str(error))
        finally:
            with self._mutex:
                self._workers.discard(threading.current_thread())
                attempt.finished.set()

    def _finish(self, attempt: _Attempt, state: ConnectionState, message: str) -> None:
        """Clear a pending attempt and record its redacted outcome."""
        with self._mutex:
            if self._attempt is attempt:
                self._attempt = None
                self._notice = (attempt.generation, state, message)

    @contextmanager
    def _pending_session(self, attempt: _Attempt) -> Iterator[Session | None]:
        """Hold both locks only while the same pending attempt can change the record."""
        if attempt.cancelled.is_set():
            yield None
            return
        with self._store.locked(cancelled=attempt.cancelled), self._mutex:
            if self._attempt is not attempt:
                yield None
                return
            current = self._store.read()
            if current.generation != attempt.generation:
                self._attempt = None
                yield None
                return
            yield current

    def _mark_recovery(self, attempt: _Attempt, client: str, *, rejected: bool = False) -> None:
        """Remember a provider-confirmed recovery path for the same saved client."""
        with self._pending_session(attempt) as current:
            if current is None:
                return
            if current.account and current.account.client_id == client:
                needs_replacement = rejected or current.client_rejected
                self._store.replace(current.model_copy(update={
                    "reconnect": True,
                    "client_rejected": needs_replacement,
                    "requires_consent": not needs_replacement,
                }))
            if self._retry_client == (attempt.generation, client):
                self._retry_client = None

    def _exchange(self, attempt: _Attempt, query: dict[str, str]) -> None:
        """Exchange the accepted code and compare identity before committing."""
        code = query.get("code", "")
        client = query.get("client_id", attempt.client_id)
        if (
            "error" in query or not 0 < len(code) <= 4096
            or not CLIENT_ID.fullmatch(client) or client == DYNAMIC_CLIENT
            or (attempt.client_id != DYNAMIC_CLIENT and client != attempt.client_id)
        ):
            raise ConnectionError("ChatGPT returned an invalid authorization. Try signing in again.")
        try:
            account, tokens = OpenAIOAuth(self._http, cancelled=attempt.cancelled).exchange(
                client_id=client, code=code, verifier=attempt.verifier,
                redirect_uri=attempt.redirect_uri, nonce=attempt.nonce,
            )
        except OAuthError as error:
            if error.code == "invalid_client":
                self._mark_recovery(attempt, client, rejected=True)
            elif error.code == "invalid_grant":
                with self._mutex:
                    if self._attempt is attempt:
                        self._retry_client = (attempt.generation, client)
            raise
        granted = tokens is not None
        with self._pending_session(attempt) as current:
            if current is None:
                return
            if current.account:
                same_client = current.account.client_id == account.client_id
                replacing = current.client_rejected
                if current.account.subject != account.subject:
                    raise ConnectionError("Select the original ChatGPT account and workspace to reconnect.")
                if replacing and (attempt.client_id != DYNAMIC_CLIENT or same_client):
                    raise ConnectionError("ChatGPT did not issue a new registration. Try connecting again.")
                if not replacing and not same_client:
                    raise ConnectionError("ChatGPT returned a different registration.")
                if not granted and (current.tokens or replacing):
                    if not replacing:
                        self._store.replace(current.model_copy(update={"requires_consent": True}))
                    raise ConnectionError("ChatGPT plan permission was missing. Your previous connection was kept.")
            self._store.replace(Session(
                generation=uuid4(), account=account, tokens=tokens,
                reconnect=not granted, requires_consent=not granted,
            ))
            self._attempt = None
            self._retry_client = None
        logger.info("ChatGPT sign-in completed with plan permission %s", granted)

    def get_status(self) -> ConnectionStatus:
        """Read public connection state without contacting OpenAI.

        Returns
        -------
        ConnectionStatus
            Local sign-in state and verified account display name, when applicable.
        """
        with self._mutex:
            session = self._read()
            authorized = bool(session.tokens and session.tokens.expires_at > time.time())
            message = None
            if self._attempt is not None and self._attempt.generation == session.generation:
                state = "signing_in"
            elif self._notice and self._notice[0] == session.generation:
                _, state, message = self._notice
            elif authorized and not (session.client_rejected or session.requires_consent):
                state = "connected"
            elif session.tokens or session.reconnect:
                state = "reconnect"
                if session.client_rejected:
                    message = "ChatGPT rejected this registration. Connect the original account again."
                elif session.requires_consent:
                    message = "Enable ChatGPT plan use to grant permission in ChatGPT."
                else:
                    message = "The ChatGPT session needs renewal."
            else:
                state = "signed_out"
            email = session.account.email if session.account and state != "signed_out" else None
            return ConnectionStatus(
                state, email, authorized, message, session.remote_revocation_unconfirmed,
                session.requires_consent,
            )

    def cancel_sign_in(self) -> ConnectionStatus:
        """Cancel sign-in while preserving any previously validated session.

        Returns
        -------
        ConnectionStatus
            Public connection state after cancellation.
        """
        with self._mutex:
            attempt = self._attempt
            if attempt is not None:
                attempt.cancelled.set()
                self._attempt = None
                self._notice = (attempt.generation, "cancelled", "Sign-in was cancelled.")
        if attempt is not None:
            attempt.server.interrupt()
            attempt.finished.wait(timeout=0.25)
        return self.get_status()

    def _replace_while_running(self, session: Session) -> None:
        """Prevent an already-draining request from writing after shutdown."""
        with self._mutex:
            if self._stopped.is_set():
                raise ConnectionError("Daily ML Practice is stopping.")
            self._store.replace(session)

    def get_access_token(self) -> str:
        """Get a plan-authorized access token for Python callers, renewing when needed.

        Returns
        -------
        str
            Current usable bearer credential.

        Raises
        ------
        ConnectionError
            If signed out, or stored credentials cannot be used or refreshed.
        """
        if self._stopped.is_set():
            raise ConnectionError("Daily ML Practice is stopping.")
        try:
            with self._store.locked(cancelled=self._stopped):
                current = self._store.read()
                if not current.tokens or not current.account:
                    raise ConnectionError("Connect ChatGPT in Settings first.")
                now = time.time()
                previous = current.tokens
                if current.client_rejected:
                    if now < previous.expires_at:
                        return previous.access_token
                    raise ConnectionError("ChatGPT rejected this registration. Reconnect in Settings.")
                if now < previous.expires_at - REFRESH_WINDOW:
                    return previous.access_token
                try:
                    rotated = OpenAIOAuth(self._http, cancelled=self._stopped).refresh(current.account, previous)
                except OAuthError as error:
                    if error.code == "invalid_client":
                        self._replace_while_running(current.model_copy(update={
                            "client_rejected": True, "reconnect": True, "requires_consent": False,
                        }))
                    elif error.code in TERMINAL_REFRESH:
                        self._replace_while_running(current.model_copy(update={"tokens": None, "reconnect": True}))
                    raise ConnectionError(str(error)) from None
                if PLAN_SCOPE not in rotated.scope.split():
                    self._replace_while_running(current.model_copy(update={
                        "tokens": None, "reconnect": True, "requires_consent": True,
                    }))
                    raise ConnectionError("ChatGPT plan permission was lost. Reconnect in Settings.")
                self._replace_while_running(current.model_copy(update={"tokens": rotated}))
                return rotated.access_token
        except StorageError as error:
            raise ConnectionError(str(error)) from None

    def sign_out(self) -> ConnectionStatus:
        """Revoke the latest renewable session and clear tokens under its lock.

        Returns
        -------
        ConnectionStatus
            Signed-out status, including whether remote revocation was unconfirmed.

        Raises
        ------
        ConnectionError
            If local credentials could not be removed.
        """
        if self._stopped.is_set():
            raise ConnectionError("Daily ML Practice is stopping.")
        self.cancel_sign_in()
        try:
            with self._store.locked(cancelled=self._stopped):
                current = self._store.read()
                unconfirmed = current.remote_revocation_unconfirmed
                if current.account and current.tokens:
                    oauth = OpenAIOAuth(self._http, cancelled=self._stopped)
                    unconfirmed = not oauth.revoke(current.account, current.tokens)
                self._replace_while_running(Session(
                    generation=uuid4(), account=current.account,
                    client_rejected=current.client_rejected, requires_consent=current.requires_consent,
                    remote_revocation_unconfirmed=unconfirmed,
                ))
        except StorageError as error:
            raise ConnectionError(str(error)) from None
        logger.info("ChatGPT signed out locally")
        return self.get_status()

    def close(self) -> None:
        """Cancel pending callback and provider I/O before releasing the app owner."""
        with self._mutex:
            self._stopped.set()
            attempt = self._attempt
            if attempt is not None:
                attempt.cancelled.set()
            self._attempt = None
            workers = tuple(self._workers)
        if attempt is not None:
            attempt.server.interrupt()
        for worker in workers:
            worker.join()
