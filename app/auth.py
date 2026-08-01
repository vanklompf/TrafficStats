"""
OIDC authentication against Authentik (or any OpenID Connect provider).

When OIDC_ENABLED is false/unset, all helpers are no-ops and the app stays
open (local development). When enabled, SessionMiddleware + middleware in
main.py gate every route except health and the auth endpoints.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Request
from starlette.responses import RedirectResponse, JSONResponse, Response

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "oidc_user"

# Paths that remain reachable without a session when OIDC is on.
PUBLIC_PATHS = frozenset({
    "/api/health",
    "/auth/login",
    "/auth/oidc/callback",
    "/auth/logout",
})


def oidc_enabled() -> bool:
    return os.environ.get("OIDC_ENABLED", "").lower() in ("1", "true", "yes")


def auto_login() -> bool:
    return os.environ.get("OIDC_AUTO_LOGIN", "true").lower() in ("1", "true", "yes")


def app_url() -> str:
    """Public base URL used for redirect_uri (no trailing slash)."""
    raw = os.environ.get("APP_URL", "").strip().rstrip("/")
    return raw


def session_secret() -> str:
    secret = os.environ.get("SESSION_SECRET", "").strip()
    if secret:
        if oidc_enabled() and len(secret.encode("utf-8")) < 32:
            raise RuntimeError(
                "SESSION_SECRET must contain at least 32 bytes when OIDC is enabled"
            )
        return secret
    if oidc_enabled():
        raise RuntimeError("SESSION_SECRET is required when OIDC is enabled")
    # Ephemeral fallback so local runs without SESSION_SECRET still work;
    # sessions will not survive restarts.
    generated = secrets.token_urlsafe(32)
    logger.warning(
        "SESSION_SECRET is unset; using an ephemeral secret "
        "(sessions reset on restart)"
    )
    return generated


def _issuer_url() -> str:
    return os.environ.get("OIDC_ISSUER_URL", "").rstrip("/") + "/"


def build_oauth() -> OAuth | None:
    """Create the Authlib OAuth registry, or None when OIDC is disabled."""
    if not oidc_enabled():
        return None

    client_id = os.environ.get("OIDC_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
    issuer = _issuer_url()
    if not client_id or not client_secret or issuer == "/":
        raise RuntimeError(
            "OIDC_ENABLED is set but OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, "
            "or OIDC_ISSUER_URL is missing"
        )

    public_url = app_url()
    parsed_public_url = urlsplit(public_url)
    if (
        parsed_public_url.scheme != "https"
        or not parsed_public_url.netloc
        or parsed_public_url.username is not None
        or parsed_public_url.password is not None
        or parsed_public_url.query
        or parsed_public_url.fragment
    ):
        raise RuntimeError(
            "APP_URL must be a canonical HTTPS URL when OIDC is enabled"
        )

    scopes = os.environ.get("OIDC_SCOPES", "openid email profile").split()
    if "openid" not in scopes:
        scopes.insert(0, "openid")

    oauth = OAuth()
    oauth.register(
        name="oidc",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=f"{issuer}.well-known/openid-configuration",
        client_kwargs={
            "scope": " ".join(scopes),
        },
    )
    return oauth


def current_user(request: Request) -> dict[str, Any] | None:
    user = request.session.get(SESSION_USER_KEY)
    if not isinstance(user, dict):
        return None
    subject = user.get("sub")
    return user if isinstance(subject, str) and subject.strip() else None


def _redirect_uri(request: Request) -> str:
    explicit = os.environ.get("OIDC_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    base = app_url()
    if base:
        return f"{base}/auth/oidc/callback"
    # Fall back to the request URL (works for direct LAN access).
    return str(request.url_for("auth_oidc_callback"))


async def login(request: Request, oauth: OAuth) -> Response:
    redirect_uri = _redirect_uri(request)
    # Preserve deep-link after login when the middleware bounced the user.
    next_path = request.query_params.get("next") or "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    request.session["oidc_next"] = next_path
    return await oauth.oidc.authorize_redirect(request, redirect_uri)


async def callback(request: Request, oauth: OAuth) -> Response:
    try:
        token = await oauth.oidc.authorize_access_token(request)
    except OAuthError as exc:
        logger.error("OIDC callback failed: %s", exc)
        return JSONResponse(
            status_code=400,
            content={"detail": f"OIDC login failed: {exc.error}"},
        )

    if not isinstance(token.get("id_token"), str) or not token["id_token"]:
        logger.error("OIDC callback returned no ID token")
        return JSONResponse(
            status_code=400,
            content={"detail": "OIDC login failed: ID token is required"},
        )

    # Authlib validates the ID token and exposes its claims as userinfo during
    # authorize_access_token(). Do not fall back to unvalidated OAuth UserInfo.
    userinfo = token.get("userinfo")
    if not isinstance(userinfo, Mapping):
        logger.error("OIDC callback returned no validated ID token claims")
        return JSONResponse(
            status_code=400,
            content={"detail": "OIDC login failed: validated claims are required"},
        )
    subject = userinfo.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        logger.error("OIDC callback returned no valid subject claim")
        return JSONResponse(
            status_code=400,
            content={"detail": "OIDC login failed: subject claim is required"},
        )

    request.session[SESSION_USER_KEY] = {
        "sub": subject,
        "email": userinfo.get("email"),
        "name": userinfo.get("name") or userinfo.get("preferred_username"),
        "preferred_username": userinfo.get("preferred_username"),
    }
    next_path = request.session.pop("oidc_next", "/")
    if not isinstance(next_path, str) or not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    return RedirectResponse(url=next_path, status_code=302)


async def logout(request: Request, oauth: OAuth | None) -> Response:
    request.session.clear()
    base = app_url() or str(request.base_url).rstrip("/")
    post_logout = base + "/"

    # Best-effort Authentik / OIDC end-session redirect.
    if oauth is not None:
        try:
            metadata = await oauth.oidc.load_server_metadata()
            end_session = metadata.get("end_session_endpoint")
            if end_session:
                params = urlencode({"post_logout_redirect_uri": post_logout})
                return RedirectResponse(
                    url=f"{end_session}?{params}",
                    status_code=302,
                )
        except Exception as exc:  # noqa: BLE001 — logout must always succeed
            logger.warning("OIDC end-session lookup failed: %s", exc)

    return RedirectResponse(url="/", status_code=302)


def unauthorized_response(request: Request) -> Response:
    """401 for API/fetch; redirect browsers to login when auto-login is on."""
    accept = request.headers.get("accept", "")
    wants_html = "text/html" in accept
    is_api = request.url.path.startswith("/api/") or request.url.path.startswith("/media/")

    if wants_html and not is_api and auto_login():
        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        login_url = "/auth/login?" + urlencode({"next": next_path})
        return RedirectResponse(url=login_url, status_code=302)

    return JSONResponse(status_code=401, content={"detail": "Authentication required"})


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    # Favicon / static assets used on the login redirect bounce.
    if path.startswith("/static/"):
        return True
    return False
