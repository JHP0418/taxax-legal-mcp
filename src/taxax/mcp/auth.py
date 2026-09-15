from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

_ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "EdDSA"})


def _https_url(value: str, name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{name}은 userinfo·fragment가 없는 HTTPS URL이어야 합니다.")
    return value


def _claim_values(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return [item.strip() for item in value]
    return []


def _scopes(claims: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    scope = claims.get("scope")
    if isinstance(scope, str):
        values.extend(scope.split())
    scp = claims.get("scp")
    if isinstance(scp, str):
        values.extend(scp.split())
    elif isinstance(scp, list):
        values.extend(item for item in scp if isinstance(item, str))
    return sorted({value.strip() for value in values if value.strip()})


@dataclass(frozen=True)
class JwtAuthConfiguration:
    issuer_url: str
    audience: str
    resource_server_url: str
    jwks_url: str
    required_scopes: tuple[str, ...] = ("legal.read",)
    algorithms: tuple[str, ...] = ("RS256",)
    org_claim: str = "org_id"

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer_url", _https_url(self.issuer_url, "issuer_url"))
        object.__setattr__(self, "resource_server_url", _https_url(self.resource_server_url, "resource_server_url"))
        object.__setattr__(self, "jwks_url", _https_url(self.jwks_url, "jwks_url"))
        if not self.audience.strip() or len(self.audience) > 512:
            raise ValueError("audience가 올바르지 않습니다.")
        if not self.required_scopes or any(not scope.strip() for scope in self.required_scopes):
            raise ValueError("required_scopes는 비어 있을 수 없습니다.")
        if not self.algorithms or any(algorithm not in _ALLOWED_ALGORITHMS for algorithm in self.algorithms):
            raise ValueError("허용되지 않은 JWT signing algorithm입니다.")
        if not self.org_claim.strip() or len(self.org_claim) > 128:
            raise ValueError("org_claim이 올바르지 않습니다.")

    @classmethod
    def from_environment(cls, *, required: bool = False) -> JwtAuthConfiguration | None:
        names = {
            "issuer_url": "TAXAX_MCP_AUTH_ISSUER",
            "audience": "TAXAX_MCP_AUTH_AUDIENCE",
            "resource_server_url": "TAXAX_MCP_AUTH_RESOURCE",
            "jwks_url": "TAXAX_MCP_AUTH_JWKS_URL",
        }
        values = {field: os.environ.get(environment, "").strip() for field, environment in names.items()}
        present = [field for field, value in values.items() if value]
        if not present:
            if required:
                raise ValueError("non-loopback HTTP에는 issuer, audience, resource, JWKS 설정이 모두 필요합니다.")
            return None
        missing = [names[field] for field, value in values.items() if not value]
        if missing:
            raise ValueError("HTTP auth 설정이 일부만 제공됐습니다: " + ", ".join(missing))
        scopes = tuple(scope for scope in os.environ.get("TAXAX_MCP_AUTH_REQUIRED_SCOPES", "legal.read").split() if scope)
        algorithms = tuple(value.strip() for value in os.environ.get("TAXAX_MCP_AUTH_ALGORITHMS", "RS256").split(",") if value.strip())
        return cls(
            issuer_url=values["issuer_url"],
            audience=values["audience"],
            resource_server_url=values["resource_server_url"],
            jwks_url=values["jwks_url"],
            required_scopes=scopes,
            algorithms=algorithms,
            org_claim=os.environ.get("TAXAX_MCP_AUTH_ORG_CLAIM", "org_id"),
        )

    def settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=AnyHttpUrl(self.issuer_url),
            resource_server_url=AnyHttpUrl(self.resource_server_url),
            required_scopes=list(self.required_scopes),
            validate_token_resource=True,
        )


class JwtTokenVerifier(TokenVerifier):
    def __init__(self, configuration: JwtAuthConfiguration, *, jwk_client=None):
        self.configuration = configuration
        self.jwk_client = jwk_client or jwt.PyJWKClient(
            configuration.jwks_url,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=300,
            timeout=5,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 16 * 1024:
            return None
        try:
            signing_key = self.jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.configuration.algorithms),
                audience=self.configuration.audience,
                issuer=self.configuration.issuer_url,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
            subject = claims.get("sub")
            expires_at = claims.get("exp")
            client_id = claims.get("client_id") or claims.get("azp")
            if not isinstance(subject, str) or not subject.strip():
                return None
            if not isinstance(expires_at, int) or isinstance(expires_at, bool):
                return None
            if not isinstance(client_id, str) or not client_id.strip():
                return None
            token_scopes = _scopes(claims)
            if not set(self.configuration.required_scopes).issubset(token_scopes):
                return None
            resources = _claim_values(claims.get("resource"))
            expected_resource = self.configuration.resource_server_url.rstrip("/")
            if expected_resource not in {value.rstrip("/") for value in resources}:
                return None
            org_id = claims.get(self.configuration.org_claim)
            if not isinstance(org_id, str) or not org_id.strip():
                return None
            safe_claims = {"org_id": org_id.strip()}
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=token_scopes,
                expires_at=expires_at,
                resource=self.configuration.resource_server_url,
                subject=subject,
                claims=safe_claims,
            )
        except (jwt.PyJWTError, ValueError, TypeError, OSError):
            return None


@dataclass(frozen=True)
class RequestScope:
    principal_id: str
    org_id: str
    authenticated: bool


def current_request_scope(*, auth_required: bool = False) -> RequestScope:
    token = get_access_token()
    if token is None:
        if auth_required:
            raise PermissionError("인증 HTTP 요청에 access token이 없습니다.")
        return RequestScope(principal_id="local-stdio", org_id="local", authenticated=False)
    subject = (token.subject or "").strip()
    claims = token.claims or {}
    org_id = claims.get("org_id")
    if not subject or not isinstance(org_id, str) or not org_id.strip():
        raise PermissionError("인증 token에 subject 또는 organization scope가 없습니다.")
    return RequestScope(principal_id=subject, org_id=org_id.strip(), authenticated=True)
