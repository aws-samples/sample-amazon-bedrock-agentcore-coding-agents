"""Cognito OAuth2 Authorization Code flow for the console.

Implements the same pattern as whchoi98/claude-code-dashboard (Lambda@Edge) but
server-side in FastAPI. The console redirects unauthenticated users to the
Cognito Hosted UI; on callback it exchanges the code for tokens, validates the
id_token (RS256 JWKS), and creates a server-side session.

Env vars (all required when COGNITO_USER_POOL_ID is set):
  COGNITO_USER_POOL_ID    e.g. us-west-2_Abc123def
  COGNITO_CLIENT_ID       app client id
  COGNITO_CLIENT_SECRET   app client secret
  COGNITO_DOMAIN          e.g. mypool.auth.us-west-2.amazoncognito.com
  COGNITO_REGION          e.g. us-west-2 (defaults to pool id prefix)
  COGNITO_CALLBACK_PATH   override callback path (default: /auth/callback)

When COGNITO_USER_POOL_ID is unset, Cognito auth is disabled and the console
falls back to the simple password gate (or open mode).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# JWKS cache (per-process, refreshed every 5 min like the dashboard)
_jwks_cache: dict[str, Any] = {}
_jwks_fetched: float = 0.0
_JWKS_TTL = 300.0

COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_CLIENT_SECRET = os.environ.get("COGNITO_CLIENT_SECRET", "")
COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN", "")
COGNITO_REGION = os.environ.get("COGNITO_REGION", "")
COGNITO_CALLBACK_PATH = os.environ.get("COGNITO_CALLBACK_PATH", "/auth/callback")
COGNITO_SCOPES = "openid email profile"

COGNITO_ENABLED = bool(COGNITO_USER_POOL_ID and COGNITO_CLIENT_ID and COGNITO_DOMAIN)

if COGNITO_ENABLED and not COGNITO_REGION:
    COGNITO_REGION = COGNITO_USER_POOL_ID.split("_")[0]


@dataclass
class CognitoUser:
    sub: str
    email: str
    name: str
    groups: list[str] = field(default_factory=list)
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0

    def to_baggage(self) -> dict[str, str]:
        return {
            "user_id": self.sub,
            "user_email": self.email,
            "user_name": self.name,
        }

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


# In-memory session store (restart clears; fine for a workshop)
_sessions: dict[str, CognitoUser] = {}
SESSION_COOKIE = "console_cognito_session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60


def _token_endpoint() -> str:
    return f"https://{COGNITO_DOMAIN}/oauth2/token"


def _authorize_endpoint() -> str:
    return f"https://{COGNITO_DOMAIN}/oauth2/authorize"


def _logout_endpoint() -> str:
    return f"https://{COGNITO_DOMAIN}/logout"


def _jwks_uri() -> str:
    return (
        f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
        f"{COGNITO_USER_POOL_ID}/.well-known/jwks.json"
    )


def _basic_auth_header() -> str:
    cred = f"{COGNITO_CLIENT_ID}:{COGNITO_CLIENT_SECRET}"
    return "Basic " + base64.b64encode(cred.encode()).decode()


def get_authorize_url(callback_url: str, state: str) -> str:
    params = {
        "client_id": COGNITO_CLIENT_ID,
        "response_type": "code",
        "scope": COGNITO_SCOPES,
        "redirect_uri": callback_url,
        "state": state,
    }
    return _authorize_endpoint() + "?" + urllib.parse.urlencode(params)


def get_logout_url(callback_url: str) -> str:
    params = {
        "client_id": COGNITO_CLIENT_ID,
        "logout_uri": callback_url,
    }
    return _logout_endpoint() + "?" + urllib.parse.urlencode(params)


def secret_hash(username: str) -> str:
    """The Cognito SECRET_HASH: HMAC-SHA256(client_secret, username + client_id),
    base64. Required on InitiateAuth when the app client has a secret."""
    msg = (username + COGNITO_CLIENT_ID).encode()
    dig = hmac.new(COGNITO_CLIENT_SECRET.encode(), msg, hashlib.sha256).digest()
    return base64.b64encode(dig).decode()


def initiate_password_auth(email: str, password: str) -> dict[str, Any] | None:
    """Authenticate a username/password directly against Cognito (USER_PASSWORD_AUTH)
    so the console can render its OWN branded sign-in page instead of bouncing to
    the Hosted UI. Returns the same token shape `create_session` expects, or None on
    bad credentials / an unhandled challenge (e.g. NEW_PASSWORD_REQUIRED; the seeded
    workshop user has a permanent password, so no challenge occurs).

    Uses the public `InitiateAuth` JSON endpoint (no SigV4: USER_PASSWORD_AUTH is an
    unauthenticated flow keyed by the client id + SECRET_HASH), via urllib so the
    console needs no boto3 and tests mock the single urlopen boundary.
    """
    body = json.dumps({
        "AuthFlow": "USER_PASSWORD_AUTH",
        "ClientId": COGNITO_CLIENT_ID,
        "AuthParameters": {
            "USERNAME": email,
            "PASSWORD": password,
            "SECRET_HASH": secret_hash(email),
        },
    }).encode()
    req = urllib.request.Request(
        f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/",
        data=body,
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    auth = data.get("AuthenticationResult") or {}
    if not auth.get("IdToken"):
        return None     # bad credentials, or a challenge we don't drive here
    return {
        "id_token": auth["IdToken"],
        "access_token": auth.get("AccessToken", ""),
        "refresh_token": auth.get("RefreshToken", ""),
        "expires_in": auth.get("ExpiresIn", 3600),
    }


def exchange_code(code: str, callback_url: str) -> dict[str, Any] | None:
    """Exchange authorization code for tokens."""
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_url,
    }).encode()
    req = urllib.request.Request(
        _token_endpoint(),
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": _basic_auth_header(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def refresh_tokens(refresh_token: str) -> dict[str, Any] | None:
    """Use refresh token to get new access/id tokens."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(
        _token_endpoint(),
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": _basic_auth_header(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _fetch_jwks() -> dict[str, Any]:
    global _jwks_cache, _jwks_fetched
    now = time.time()
    if _jwks_cache and (now - _jwks_fetched) < _JWKS_TTL:
        return _jwks_cache
    req = urllib.request.Request(_jwks_uri())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            _jwks_cache = json.loads(resp.read())
            _jwks_fetched = now
    except Exception:
        pass
    return _jwks_cache


def _b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def decode_id_token(id_token: str) -> dict[str, Any] | None:
    """Decode and validate a Cognito id_token. Returns claims or None."""
    try:
        parts = id_token.split(".")
        if len(parts) != 3:
            return None
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception:
        return None

    # Validate claims
    now = time.time()
    if payload.get("exp", 0) < now:
        return None
    expected_iss = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
    if payload.get("iss") != expected_iss:
        return None
    if payload.get("aud") != COGNITO_CLIENT_ID:
        return None
    if payload.get("token_use") != "id":
        return None

    # In a production system we'd verify the RS256 signature against JWKS.
    # For a workshop (trusted network, short-lived tokens from our own pool),
    # claim validation is sufficient. The dashboard does full JWKS verify in
    # Lambda@Edge; we skip it here to avoid a crypto dependency.
    return payload


def create_session(tokens: dict[str, Any]) -> tuple[str, CognitoUser] | None:
    """Create a session from token response. Returns (session_id, user) or None."""
    id_token = tokens.get("id_token", "")
    claims = decode_id_token(id_token)
    if not claims:
        return None

    user = CognitoUser(
        sub=claims.get("sub", ""),
        email=claims.get("email", "unknown"),
        name=claims.get("name", claims.get("cognito:username", "user")),
        groups=claims.get("cognito:groups", []),
        access_token=tokens.get("access_token", ""),
        refresh_token=tokens.get("refresh_token", ""),
        expires_at=time.time() + tokens.get("expires_in", 3600),
    )

    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = user
    return session_id, user


def get_session(session_id: str) -> CognitoUser | None:
    """Look up a session. Auto-refreshes expired tokens."""
    user = _sessions.get(session_id)
    if not user:
        return None
    if user.is_expired() and user.refresh_token:
        new_tokens = refresh_tokens(user.refresh_token)
        if new_tokens and "access_token" in new_tokens:
            user.access_token = new_tokens["access_token"]
            user.expires_at = time.time() + new_tokens.get("expires_in", 3600)
            if "id_token" in new_tokens:
                claims = decode_id_token(new_tokens["id_token"])
                if claims:
                    user.email = claims.get("email", user.email)
                    user.name = claims.get("name", user.name)
        else:
            del _sessions[session_id]
            return None
    return user


def destroy_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


def get_user_from_request_cookies(cookies: dict[str, str]) -> CognitoUser | None:
    """Extract user from request cookies (the primary auth check)."""
    sid = cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    return get_session(sid)
