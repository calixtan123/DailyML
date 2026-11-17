"""Drive sign-in using an actual loopback callback and mocked OpenAI HTTP."""

import base64
import hashlib
from urllib.parse import urlsplit

import httpx
import pytest

from daily_ml.connection import Connection, ConnectionError
from oauth_support import CLIENT, OTHER_KEY, PLAN, Provider, callback, start, wait_for


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
    status = wait_for(connection, "timeout" if action == "wait" else "cancelled")
    assert not status.plan_authorized
    assert provider.code_calls == []
    with pytest.raises(httpx.TransportError):
        callback(params)


def test_cancelled_exchange_cannot_publish_credentials(flow):
    """Cancellation still wins when a provider reply arrives after it."""
    import threading

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
            alternate.cancel_sign_in()
            release.set()
        finally:
            release.set()
            alternate.close()
    assert not connection.get_status().plan_authorized
