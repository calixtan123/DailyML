"""Drive sign-in using an actual loopback callback and mocked OpenAI HTTP."""

import base64
import hashlib
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

import httpx
import pytest

from daily_ml import openai_oauth
from daily_ml.connection import Connection, ConnectionError
from daily_ml.locking import file_lock
from oauth_support import CLIENT, OTHER_KEY, PLAN, Provider, callback, start, wait_for


def test_isolated_oauth_worker_starts_from_another_directory(tmp_path):
    """The production worker can start from this installation without an HTTP request."""
    subprocess.run(
        openai_oauth._PROVIDER_COMMAND, cwd=tmp_path,
        input=b'["GET", "https://invalid.example", null]', capture_output=True, check=True, timeout=4,
    )


@pytest.fixture
def flow(tmp_path):
    """Supply each test an isolated connection and close every callback listener."""
    provider = Provider()
    with httpx.Client(transport=httpx.MockTransport(provider.transport)) as http:
        connection = Connection(tmp_path, http=http, callback_timeout=0.8)
        try:
            yield connection, provider
        finally:
            connection.close()


def test_dynamic_registration_uses_exact_callback_and_retains_verified_account(flow):
    """Observe PKCE, grant validation, protected credentials and returning client reuse."""
    connection, provider = flow
    params = start(connection, provider)
    assert params["client_id"] == "dynamic_agent_client"
    assert params["agent_name_hint"] == "Daily ML Practice"
    assert params["ext_agent_host_id"].startswith("urn:uuid:")
    assert params["scope"] == f"openid profile email offline_access resource.invoke {PLAN}"
    assert params["response_type"] == "code"
    redirect = urlsplit(params["redirect_uri"])
    assert redirect.scheme == "http" and redirect.hostname == "127.0.0.1"
    assert redirect.path == "/auth/callback"
    assert params["code_challenge_method"] == "S256"
    assert callback(params, scope="irrelevant-callback-scope").status_code == 200
    status = wait_for(connection, "connected")
    assert status.email == "learner@example.test" and status.plan_authorized
    assert connection.get_access_token() == "original-access"
    exchange = provider.code_calls[0]
    verifier = exchange.pop("code_verifier")[0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert params["code_challenge"] == challenge
    assert exchange == {"grant_type": ["authorization_code"], "code": ["mock-code"],
                        "client_id": [CLIENT], "redirect_uri": [params["redirect_uri"]],
                        "resource": ["https://api.openai.com/v1"]}
    returning = start(connection, provider)
    assert returning["client_id"] == CLIENT
    assert returning["ext_agent_host_id"] == params["ext_agent_host_id"]
    assert returning["state"] != params["state"] and returning["nonce"] != params["nonce"]
    assert "agent_name_hint" not in returning
    assert callback(returning, client_id=None).status_code == 200
    wait_for(connection, "connected")


@pytest.mark.parametrize(
    ("claim", "value"),
    [("nonce", "wrong"), ("iss", "https://invalid.test"), ("aud", "different-client"),
     ("exp", 1), ("sub", ""), ("email_verified", False), ("azp", "different-client")],
)
def test_invalid_identity_claims_do_not_connect(flow, claim, value):
    """Reject any unverified identity before saving a credential."""
    connection, provider = flow
    params = start(connection, provider)
    provider.claims[claim] = value
    assert callback(params).status_code == 200
    assert not wait_for(connection, "error").plan_authorized
    with pytest.raises(ConnectionError):
        connection.get_access_token()


def test_invalid_signature_is_rejected(flow):
    """A signed payload from a different key cannot establish a connection."""
    connection, provider = flow
    params = start(connection, provider)
    provider.key = OTHER_KEY
    callback(params)
    assert not wait_for(connection, "error").plan_authorized


@pytest.mark.parametrize("payload", [[], {"keys": [None]}, {"keys": []}])
def test_malformed_signing_keys_are_a_recoverable_failure(flow, payload):
    """A malformed provider key set must not leave the app stuck signing in."""
    connection, provider = flow
    provider.jwks_response = httpx.Response(200, json=payload)
    callback(start(connection, provider))
    assert not wait_for(connection, "error").plan_authorized


def test_mismatched_state_never_reaches_exchange(flow, caplog, capsys):
    """Ignore incorrect state without logging callback data or consuming the attempt."""
    connection, provider = flow
    params = start(connection, provider)
    response = callback(params, state="wrong-state", code="secret-callback-code")
    assert response.status_code == 400
    assert "secret-callback-code" not in response.text + caplog.text + capsys.readouterr().err
    assert provider.code_calls == []
    assert callback(params).status_code == 200
    wait_for(connection, "connected")
    with pytest.raises(httpx.TransportError):
        callback(params)


@pytest.mark.parametrize("mismatch", ["identity", "client", "scope"])
def test_failed_replacement_preserves_working_credentials(flow, mismatch):
    """Returning sign-in validates before replacing the active token set."""
    connection, provider = flow
    callback(start(connection, provider))
    wait_for(connection, "connected")
    params = start(connection, provider)
    if mismatch == "identity":
        provider.claims["sub"] = "other-user"
    if mismatch == "scope":
        provider.scope = "openid email"
    callback(params, client_id="oaiapp_other" if mismatch == "client" else CLIENT)
    assert wait_for(connection, "error").plan_authorized
    assert connection.get_access_token() == "original-access"
    if mismatch == "scope":
        assert start(connection, provider)["prompt"] == "consent"


def test_missing_plan_scope_requires_explicit_consent_retry(flow):
    """Save the verified registration for retry while leaving plan use disabled."""
    connection, provider = flow
    provider.scope = "openid email"
    callback(start(connection, provider), scope=PLAN)
    status = wait_for(connection, "reconnect")
    assert not status.plan_authorized
    assert status.email == "learner@example.test"
    with pytest.raises(ConnectionError):
        connection.get_access_token()
    returning = start(connection, provider)
    assert returning["client_id"] == CLIENT
    assert returning["prompt"] == "consent"
    provider.scope = PLAN
    callback(returning, client_id=None)
    wait_for(connection, "connected")


def test_missing_scope_and_refresh_retains_verified_registration(flow):
    """An identity-only OAuth reply can recover through an explicit consent attempt."""
    connection, provider = flow
    provider.omit_refresh_token = True
    provider.scope = "openid email"
    callback(start(connection, provider))
    assert not wait_for(connection, "reconnect").plan_authorized
    returning = start(connection, provider)
    assert returning["client_id"] == CLIENT and returning["prompt"] == "consent"


@pytest.mark.parametrize("client_id", [None, "dynamic_agent_client", "bad client"])
def test_dynamic_callback_must_supply_an_issued_client(flow, client_id):
    """The placeholder registration client is never exchanged or saved."""
    connection, provider = flow
    callback(start(connection, provider), client_id=client_id)
    wait_for(connection, "error")
    assert provider.code_calls == []


def test_invalid_code_exchange_retries_with_issued_client(flow):
    """A fresh authorization after invalid_grant uses the client issued for that attempt."""
    connection, provider = flow
    provider.code_response = httpx.Response(400, json={"error": "invalid_grant"})
    callback(start(connection, provider))
    wait_for(connection, "error")
    fresh = start(connection, provider)
    assert fresh["client_id"] == CLIENT
    assert "agent_name_hint" not in fresh


@pytest.mark.parametrize("action", ["deny", "cancel", "wait"])
def test_callback_listener_stops_for_denial_cancellation_and_timeout(flow, action):
    """Discard the pending attempt and release its port without exchanging a code."""
    connection, provider = flow
    params = start(connection, provider)
    if action == "deny":
        callback(params, code=None, error="access_denied")
    elif action == "cancel":
        connection.cancel_sign_in()
    expected = {"wait": "timeout", "deny": "declined", "cancel": "cancelled"}
    status = wait_for(connection, expected[action])
    assert not status.plan_authorized
    assert not status.requires_consent
    assert provider.code_calls == []
    with pytest.raises(httpx.TransportError):
        callback(params)
    if action == "deny":
        next_attempt = start(connection, provider)
        assert next_attempt["client_id"] == "dynamic_agent_client"
        assert "prompt" not in next_attempt


@pytest.mark.parametrize("action", ["cancel", "sign_out"])
def test_cancelled_exchange_cannot_publish_credentials(flow, action):
    """A late provider reply cannot publish after cancellation or sign-out."""
    connection, provider = flow
    entered, release = threading.Event(), threading.Event()
    original_transport = provider.transport

    def held_transport(request):
        if request.url.path.endswith("/token"):
            entered.set()
            assert release.wait(3)
        return original_transport(request)

    with httpx.Client(transport=httpx.MockTransport(held_transport)) as http:
        alternate = Connection(connection.data_dir, http=http)
        try:
            callback(start(alternate, provider))
            assert entered.wait(1)
            if action == "cancel":
                alternate.cancel_sign_in()
            else:
                connection.sign_out()
            release.set()
        finally:
            release.set()
            alternate.close()
    assert not connection.get_status().plan_authorized


@pytest.mark.parametrize("failure", ["subject", "audience", "scope", "rejected_client"])
def test_rejected_client_only_replaced_after_verified_new_registration(flow, failure):
    """A confirmed rejection permits a new client for the same verified subject."""
    connection, provider = flow
    initial = start(connection, provider)
    callback(initial)
    wait_for(connection, "connected")

    returning = start(connection, provider)
    assert returning["client_id"] == CLIENT
    provider.code_response = httpx.Response(401, json={"error": {"code": "invalid_client"}})
    callback(returning, client_id=None)
    status = wait_for(connection, "error")
    assert status.plan_authorized
    assert "intended workspace" in status.message
    assert connection.get_access_token() == "original-access"

    provider.code_response = None
    replacement = start(connection, provider)
    assert replacement["client_id"] == "dynamic_agent_client"
    assert replacement["ext_agent_host_id"] == initial["ext_agent_host_id"]
    if failure == "subject":
        provider.claims["sub"] = "different-user"
    elif failure == "audience":
        provider.claims["aud"] = CLIENT
    elif failure == "scope":
        provider.scope = "openid email"
    callback(replacement, client_id=CLIENT if failure == "rejected_client" else "oaiapp_replacement")
    assert wait_for(connection, "error").plan_authorized
    assert connection.get_access_token() == "original-access"

    provider.claims.clear()
    provider.scope = PLAN
    replacement = start(connection, provider)
    assert replacement["client_id"] == "dynamic_agent_client"
    callback(replacement, client_id="oaiapp_replacement")
    wait_for(connection, "connected")
    assert start(connection, provider)["client_id"] == "oaiapp_replacement"


@pytest.mark.parametrize("had_tokens", [True, False])
def test_returning_provider_denial_prepares_explicit_consent_retry(flow, had_tokens):
    """A verified returning decline prepares consent and leaves prior tokens intact."""
    connection, provider = flow
    callback(start(connection, provider))
    wait_for(connection, "connected")
    if not had_tokens:
        connection.sign_out()

    denied = start(connection, provider)
    assert "prompt" not in denied
    assert callback(denied, error="access_denied", code=None, client_id=None).status_code == 200
    result = wait_for(connection, "declined")
    assert result.requires_consent
    assert result.plan_authorized is had_tokens
    if had_tokens:
        assert connection.get_access_token() == "original-access"
    assert len(provider.code_calls) == 1

    explicit_retry = start(connection, provider)
    assert explicit_retry["client_id"] == CLIENT
    assert explicit_retry["prompt"] == "consent"
    assert explicit_retry["scope"] == denied["scope"]


def test_local_cancel_does_not_prepare_consent(flow):
    """Cancel does not change the registration or the next authorization prompt."""
    connection, provider = flow
    callback(start(connection, provider))
    wait_for(connection, "connected")
    start(connection, provider)
    assert connection.cancel_sign_in().state == "cancelled"
    next_attempt = start(connection, provider)
    assert next_attempt["client_id"] == CLIENT
    assert "prompt" not in next_attempt


@pytest.mark.parametrize(("phase", "action"), [
    ("normal", "success"),
    ("token_headers", "close"),
    ("token_body", "close"),
    ("jwks_headers", "close"),
    ("token_headers", "deadline"),
    ("token_body", "deadline"),
    ("jwks_headers", "deadline"),
])
def test_provider_exchange_is_bounded_and_cancellable(tmp_path, monkeypatch, phase, action):
    """A real socket can trickle through token headers, body or JWKS without prolonging sign-in."""
    entered = threading.Event()
    provider = Provider()

    class SlowProvider(BaseHTTPRequestHandler):
        """Serve a valid, deliberately slow provider response on loopback."""

        def log_message(self, format, *args):
            """Keep the simulated provider quiet."""

        def do_POST(self):
            """Return a signed token response."""
            self._respond("token", self.rfile.read(int(self.headers["Content-Length"])))

        def do_GET(self):
            """Return the signing key for the token."""
            self._respond("jwks", b"")

        def _respond(self, endpoint, request_body):
            """Keep progressing slowly enough to avoid an inactivity timeout."""
            uri = openai_oauth.ISSUER + self.path
            reply = provider.transport(httpx.Request(self.command, uri, content=request_body)).content
            try:
                # A slow token request followed by a slow JWKS request also exceeds the shared budget.
                if endpoint == "token" and phase == "jwks_headers":
                    time.sleep(0.35)
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                if phase == endpoint + "_headers":
                    self.wfile.write(b"X-Probe: ")
                    self._trickle()
                    self.wfile.write(b"\r\n")
                self.wfile.write(f"Content-Length: {len(reply)}\r\n\r\n".encode())
                if phase == endpoint + "_body":
                    self._trickle(body=reply)
                else:
                    self.wfile.write(reply)
            except OSError:
                pass  # Closing the peer is the expected cancellation path.

        def _trickle(self, body=None):
            """Deliver bytes for longer than one total-request budget."""
            entered.set()
            for index in range(20):
                self.wfile.write(b"x" if body is None else body[index:index + 1])
                time.sleep(0.075)
            if body is not None:
                self.wfile.write(body[20:])

    with HTTPServer(("127.0.0.1", 0), SlowProvider) as server:
        origin = f"http://127.0.0.1:{server.server_port}"
        token_url = origin + "/api/accounts/oauth/token"
        jwks_url = origin + "/.well-known/jwks.json"
        # The installed process resolves the production endpoints itself. The test runner routes
        # its fixed URLs to the loopback double inside the disposable child.
        runner = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from daily_ml import openai_oauth as oauth; "
            f"oauth.TOKEN_ENDPOINT = {token_url!r}; oauth.JWKS = {jwks_url!r}; "
            "oauth._serve_provider_request()"
        )
        monkeypatch.setattr(openai_oauth, "TOKEN_ENDPOINT", token_url)
        monkeypatch.setattr(openai_oauth, "JWKS", jwks_url)
        monkeypatch.setattr(openai_oauth, "_PROVIDER_COMMAND", (
            sys.executable, "-I", "-c", runner, openai_oauth._PROVIDER_COMMAND[-1],
        ))
        monkeypatch.setattr(openai_oauth, "HTTP_TIMEOUT", 1.1)

        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        connection = Connection(tmp_path)
        try:
            params = start(connection, provider)
            started = time.monotonic()
            assert callback(params).status_code == 200
            if action == "success":
                assert wait_for(connection, "connected").plan_authorized
                return
            assert entered.wait(2)
            if action == "close":
                shutdown = time.monotonic()
                connection.close()
                assert time.monotonic() - shutdown < 0.75
            else:
                result = wait_for(connection, "error")
                assert "timed out" in result.message
                assert time.monotonic() - started < 1.5
            assert not connection.get_status().plan_authorized
            assert not (tmp_path / "credentials/session.json").exists()
        finally:
            connection.close()
            server.shutdown()
            worker.join()


@pytest.mark.parametrize("action", ["complete", "close"])
def test_pending_commit_respects_credential_lock_and_shutdown(flow, action):
    """The credential lock allows ordinary contention and wakes promptly on shutdown."""
    connection, provider = flow
    entered = threading.Event()
    original_transport = provider.transport

    def signal_jwks(request):
        if request.url.path.endswith("/jwks.json"):
            entered.set()
        return original_transport(request)

    with httpx.Client(transport=httpx.MockTransport(signal_jwks)) as http:
        alternate = Connection(connection.data_dir, http=http)
        try:
            params = start(alternate, provider)
            with file_lock(connection.data_dir / "credentials/.lock", wait=0):
                callback(params)
                assert entered.wait(1)
                if action == "complete":
                    time.sleep(1.15)
                else:
                    started = time.monotonic()
                    alternate.close()
                    assert time.monotonic() - started < 0.65
            if action == "complete":
                assert wait_for(alternate, "connected").plan_authorized
            else:
                assert not connection.get_status().plan_authorized
        finally:
            alternate.close()


def test_decline_of_unverified_retry_returns_to_initial_registration(flow):
    """A provider decline keeps the opt-in on the initial flow until identity is verified."""
    connection, provider = flow
    provider.code_response = httpx.Response(400, json={"error": "invalid_grant"})
    callback(start(connection, provider))
    wait_for(connection, "error")
    retry = start(connection, provider)
    assert retry["client_id"] == CLIENT

    callback(retry, client_id=None, error="access_denied", code=None)
    assert not wait_for(connection, "declined").requires_consent
    next_attempt = start(connection, provider)
    assert next_attempt["client_id"] == "dynamic_agent_client"
    assert "prompt" not in next_attempt
