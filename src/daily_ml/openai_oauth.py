"""OpenAI's documented public-client OAuth exchange and ID-token validation."""

import json
import logging
import time
from typing import Annotated

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .credentials import PLAN_SCOPE, Account, Token, Tokens

ISSUER = "https://auth.openai.com"
AUTHORIZE = f"{ISSUER}/api/accounts/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/api/accounts/oauth/token"
REVOKE_ENDPOINT = f"{ISSUER}/api/accounts/oauth/revoke"
JWKS = f"{ISSUER}/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
DYNAMIC_CLIENT = "dynamic_agent_client"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
MAX_BODY = 256 * 1024
HTTP_TIMEOUT = 4
TERMINAL_REFRESH = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused", "invalid_client",
}

logger = logging.getLogger(__name__)


class OAuthError(RuntimeError):
    """A redacted authorization or transport failure."""

    def __init__(self, message: str, *, code: str = "", transient: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.transient = transient


class TokenReply(BaseModel):
    """The bounded token response fields needed for this public client."""

    model_config = ConfigDict(strict=True)
    access_token: Token = Field(repr=False)
    refresh_token: Token | None = Field(default=None, repr=False)
    id_token: Token | None = Field(default=None, repr=False)
    token_type: str
    expires_in: Annotated[int, Field(gt=0, le=86_400)]
    scope: Annotated[str, Field(max_length=1024)] | None = None


class OpenAIOAuth:
    """Use a supplied HTTP client for interactions with fixed OpenAI endpoints."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def _request(self, method: str, url: str, *, data: dict | None = None) -> tuple[int, bytes]:
        """Enforce a bounded response and discard provider diagnostics."""
        try:
            with self._http.stream(
                method, url, data=data, timeout=HTTP_TIMEOUT, follow_redirects=False,
                headers={"Accept": "application/json"},
            ) as reply:
                body = bytearray()
                deadline = time.monotonic() + HTTP_TIMEOUT
                for chunk in reply.iter_bytes(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > MAX_BODY or time.monotonic() >= deadline:
                        raise OAuthError("ChatGPT returned an oversized or slow response.")
                return reply.status_code, bytes(body)
        except httpx.HTTPError:
            raise OAuthError("ChatGPT could not be reached. Try again.", transient=True) from None

    def _token(self, form: dict[str, str]) -> TokenReply:
        """Request a public-client grant and validate its shape."""
        status, body = self._request("POST", TOKEN_ENDPOINT, data={**form, "resource": RESOURCE})
        if status != 200:
            code = ""
            if status in {400, 401, 403}:
                try:
                    payload = json.loads(body)
                    value = payload.get("error") if isinstance(payload, dict) else None
                    if isinstance(value, str) and value in TERMINAL_REFRESH:
                        code = value
                    if isinstance(value, dict) and value.get("code") == "invalid_client":
                        code = "invalid_client"
                except ValueError:
                    pass
            if code == "invalid_client":
                raise OAuthError("ChatGPT rejected the registered client. Review the connection in ChatGPT.", code=code)
            if status >= 500 or status == 429:
                raise OAuthError("ChatGPT is temporarily unavailable. Try again.", transient=True)
            raise OAuthError("ChatGPT did not accept the authorization. Try signing in again.", code=code)
        try:
            reply = TokenReply.model_validate_json(body)
        except ValidationError:
            raise OAuthError("ChatGPT returned an invalid token response. Try signing in again.") from None
        if reply.token_type.casefold() != "bearer":
            raise OAuthError("ChatGPT returned an unsupported token type.")
        return reply

    def _identity(self, token: str, client_id: str, *, nonce: str | None = None) -> Account:
        """Verify a signed OpenID identity using PyJWT and OpenAI's fixed JWKS."""
        status, body = self._request("GET", JWKS)
        if status != 200:
            raise OAuthError("ChatGPT signing keys are temporarily unavailable.", transient=True)
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
                raise ValueError
            keys = jwt.PyJWKSet.from_dict(payload)
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            candidates = [
                key for key in keys.keys
                if isinstance(kid, str) and kid and key.key_id == kid
                and key.algorithm_name == "RS256" and key.public_key_use in (None, "sig")
            ]
            if len(candidates) != 1:
                raise ValueError
            claims = jwt.decode(
                token, candidates[0], algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "email", "email_verified"]},
            )
            aud = claims["aud"]
            if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != client_id:
                raise ValueError
            if "azp" in claims and claims["azp"] != client_id:
                raise ValueError
            if (
                (nonce is not None and claims.get("nonce") != nonce)
                or claims["email_verified"] is not True
                or type(claims["exp"]) is not int
                or type(claims["iat"]) is not int
                or not isinstance(claims["sub"], str)
                or not claims["sub"].strip()
                or not isinstance(claims["email"], str)
                or claims["email"].count("@") != 1
                or not all(claims["email"].split("@"))
                or any(character.isspace() for character in claims["email"])
            ):
                raise ValueError
            return Account(client_id=client_id, subject=claims["sub"], email=claims["email"])
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            raise OAuthError("ChatGPT account identity could not be verified. Try signing in again.") from None

    def exchange(
        self, *, client_id: str, code: str, verifier: str, redirect_uri: str, nonce: str
    ) -> tuple[Account, Tokens | None]:
        """Exchange a code, validate its ID token and return the granted scopes.

        Parameters
        ----------
        client_id : str
            Issued OAuth client for this callback.
        code : str
            Short-lived authorization code.
        verifier : str
            Original PKCE secret.
        redirect_uri : str
            Exact loopback URI sent during authorization.
        nonce : str
            Original OpenID challenge.

        Returns
        -------
        tuple of Account and Tokens or None
            Verified identity and credentials when the ChatGPT plan permission was granted.

        Raises
        ------
        OAuthError
            If the grant or identity is invalid.
        """
        reply = self._token({
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "code_verifier": verifier, "redirect_uri": redirect_uri,
        })
        if not reply.id_token:
            raise OAuthError("ChatGPT returned no account identity. Try signing in again.")
        account = self._identity(reply.id_token, client_id, nonce=nonce)
        if PLAN_SCOPE not in (reply.scope or "").split():
            return account, None
        if not reply.refresh_token:
            raise OAuthError("ChatGPT returned no renewable session. Try signing in again.")
        return account, Tokens(
            access_token=reply.access_token, refresh_token=reply.refresh_token,
            id_token=reply.id_token, scope=reply.scope or "",
            expires_at=time.time() + reply.expires_in,
        )

    def refresh(self, account: Account, previous: Tokens) -> Tokens:
        """Rotate a session using its issued client and original permission grant.

        Parameters
        ----------
        account : Account
            Selected verified registration.
        previous : Tokens
            Vested token set holding the refresh token.

        Returns
        -------
        Tokens
            Replacement token set.

        Raises
        ------
        OAuthError
            If refresh or any returned identity fails validation.
        """
        reply = self._token({
            "grant_type": "refresh_token", "client_id": account.client_id,
            "refresh_token": previous.refresh_token,
        })
        if not reply.refresh_token:
            raise OAuthError("ChatGPT returned no replacement refresh token. Reconnect in Settings.")
        if reply.id_token:
            refreshed = self._identity(reply.id_token, account.client_id)
            if refreshed.subject != account.subject:
                raise OAuthError("ChatGPT returned a different account. Reconnect the original account.")
        return Tokens(
            access_token=reply.access_token, refresh_token=reply.refresh_token,
            id_token=reply.id_token or previous.id_token,
            scope=reply.scope if reply.scope is not None else previous.scope,
            expires_at=time.time() + reply.expires_in,
        )

    def revoke(self, account: Account, tokens: Tokens) -> bool:
        """Attempt remote renewal revocation, with one bounded retry on temporary failure.

        Parameters
        ----------
        account : Account
            Issued client associated with the session.
        tokens : Tokens
            Latest session credentials.

        Returns
        -------
        bool
            Whether OpenAI confirmed revocation.
        """
        form = {"token": tokens.refresh_token, "token_type_hint": "refresh_token",
                "client_id": account.client_id}
        for attempt in range(2):
            try:
                status, body = self._request("POST", REVOKE_ENDPOINT, data=form)
                if status == 200 and not body:
                    return True
                if status < 500:
                    break
            except OAuthError as error:
                if not error.transient:
                    break
            if attempt == 0:
                time.sleep(0.25)
        logger.info("ChatGPT session revocation could not be confirmed")
        return False
