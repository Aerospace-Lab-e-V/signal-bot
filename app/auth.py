from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import HTTPException, Request, status
from starlette.responses import RedirectResponse

from app.config import Settings, settings


DEVELOPMENT_USER = {
    "sub": "development-auth-bypass",
    "name": "Development Admin",
    "preferred_username": "dev-admin",
    "email": "dev-admin@localhost",
}


def build_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    if settings.oidc_enabled:
        oauth.register(
            name="keycloak",
            server_metadata_url=settings.oidc_metadata_url,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            client_kwargs={"scope": "openid email profile"},
        )
    return oauth


def _claim_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, list | tuple | set):
        return {str(item) for item in value if item}
    return set()


def _claim_groups(user: dict[str, Any]) -> set[str]:
    return _claim_values(user.get("groups"))


def _claim_roles(user: dict[str, Any]) -> set[str]:
    roles = set()
    realm_access = user.get("realm_access") or {}
    roles.update(realm_access.get("roles") or [])

    resource_access = user.get("resource_access") or {}
    for client_access in resource_access.values():
        roles.update(client_access.get("roles") or [])

    return roles


def is_authorized(user: dict[str, Any], settings: Settings) -> bool:
    allowed_groups = settings.allowed_oidc_groups
    if not allowed_groups and not settings.oidc_allowed_role:
        return True

    if allowed_groups and allowed_groups.intersection(_claim_groups(user)):
        return True

    if settings.oidc_allowed_role and settings.oidc_allowed_role in _claim_roles(user):
        return True

    return False


def oidc_debug_claims(user: dict[str, Any], settings: Settings) -> dict[str, Any]:
    return {
        "claim_keys": sorted(user.keys()),
        "subject": user.get("sub"),
        "username": user.get("preferred_username"),
        "groups": sorted(_claim_groups(user)),
        "allowed_groups": sorted(settings.allowed_oidc_groups),
        "roles": sorted(_claim_roles(user)),
        "allowed_role": settings.oidc_allowed_role,
    }


def current_user(request: Request) -> dict[str, Any]:
    user = request.session.get("user")
    if not user and settings.auth_bypass_for_development:
        return DEVELOPMENT_USER

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def require_user(request: Request) -> dict[str, Any]:
    return current_user(request)


def auth_redirect(request: Request) -> RedirectResponse:
    request.session["next_url"] = str(request.url)
    return RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)


def user_display_name(user: dict[str, Any]) -> str:
    return (
        user.get("name")
        or user.get("preferred_username")
        or user.get("email")
        or "Authenticated user"
    )


__all__ = [
    "DEVELOPMENT_USER",
    "OAuthError",
    "auth_redirect",
    "build_oauth",
    "is_authorized",
    "oidc_debug_claims",
    "require_user",
    "user_display_name",
]
