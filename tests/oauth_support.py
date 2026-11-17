"""Mock OpenAI at the HTTP boundary with signed OpenID tokens."""

import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://auth.openai.com"
CLIENT = "oaiapp_test"
PLAN = "chatgpt.tokens.use.direct"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Provider:
    """Supply configurable OAuth replies without live requests."""

    def __init__(self):
        self.nonce = ""
        self.claims = {}
        self.key = KEY
        self.code_calls = []
        self.refresh_calls = []
        self.revoke_calls = []
        self.scope = PLAN
        self.access_lifetime = 3600
        self.omit_refresh_token = False
        self.refresh_response = None
        self.code_response = None
        self.jwks_response = None
        self.revoke_response = httpx.Response(200)

    def transport(self, request):
        """Return a signed exchange or a predetermined renewal/revocation response."""
        assert request.url.host == "auth.openai.com"
        if request.url.path == "/.well-known/jwks.json":
            if self.jwks_response is not None:
                return self.jwks_response
            key = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
            return httpx.Response(200, json={"keys": [{**key, "kid": "test", "alg": "RS256", "use": "sig"}]})
        form = parse_qs(request.content.decode())
        if request.url.path == "/api/accounts/oauth/revoke":
            self.revoke_calls.append(form)
            return self.revoke_response
        assert request.url.path == "/api/accounts/oauth/token"
        if form["grant_type"] == ["refresh_token"]:
            self.refresh_calls.append(form)
            if callable(self.refresh_response):
                return self.refresh_response()
            return self.refresh_response or httpx.Response(
                200, json={"access_token": "rotated-access", "refresh_token": "rotated-refresh",
                           "token_type": "Bearer", "expires_in": 3600}
            )
        assert form["grant_type"] == ["authorization_code"]
        self.code_calls.append(form)
        if self.code_response is not None:
            return self.code_response
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": form["client_id"][0], "sub": "original-user",
                  "email": "learner@example.test", "email_verified": True,
                  "exp": now + 300, "iat": now, "nonce": self.nonce, **self.claims}
        token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test"})
        reply = {
            "access_token": "original-access", "refresh_token": "original-refresh",
            "id_token": token, "token_type": "Bearer", "expires_in": self.access_lifetime,
            "scope": self.scope,
        }
        if self.omit_refresh_token:
            del reply["refresh_token"]
        return httpx.Response(200, json=reply)


def start(connection, provider):
    """Start through the safe handle and simulate the browser's OAuth redirect."""
    safe = connection.start_sign_in()
    url = connection.authorization_url(safe.launch_path.rsplit("/", 1)[-1])
    assert urlsplit(url).netloc == "auth.openai.com"
    assert urlsplit(url).path == "/api/accounts/authorize"
    params = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    provider.nonce = params["nonce"]
    return params


def callback(params, **overrides):
    """Send a loopback redirect without contacting OpenAI."""
    query = {"code": "mock-code", "state": params["state"], "client_id": CLIENT, **overrides}
    query = {key: value for key, value in query.items() if value is not None}
    with httpx.Client(trust_env=False, timeout=2) as client:
        return client.get(f'{params["redirect_uri"]}?{urlencode(query)}')


def wait_for(connection, state):
    """Wait briefly for the callback worker to report a terminal state."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = connection.get_status()
        if status.state == state:
            return status
        time.sleep(0.01)
    raise AssertionError(f"Expected {state}, got {connection.get_status()}")
