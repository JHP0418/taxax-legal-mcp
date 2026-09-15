from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import AccessToken

from taxax.mcp import auth as auth_module
from taxax.mcp import server as server_module
from taxax.mcp.auth import JwtAuthConfiguration, JwtTokenVerifier, current_request_scope


class StaticJwkClient:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=self.key)


class JwtTokenVerifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self.private_key.public_key()
        self.configuration = JwtAuthConfiguration(
            issuer_url="https://issuer.example.com",
            audience="taxax-legal",
            resource_server_url="https://legal.example.com",
            jwks_url="https://issuer.example.com/.well-known/jwks.json",
            required_scopes=("legal.read",),
            algorithms=("RS256",),
        )
        self.verifier = JwtTokenVerifier(
            self.configuration,
            jwk_client=StaticJwkClient(self.public_key),
        )

    def claims(self, **updates):
        values = {
            "iss": self.configuration.issuer_url,
            "aud": self.configuration.audience,
            "resource": self.configuration.resource_server_url,
            "sub": "employee-1",
            "client_id": "tax-office-client",
            "org_id": "office-1",
            "scope": "legal.read",
            "exp": int(time.time()) + 600,
        }
        values.update(updates)
        return values

    def token(self, claims=None, *, algorithm="RS256", key=None):
        return jwt.encode(
            claims or self.claims(),
            key or self.private_key,
            algorithm=algorithm,
            headers={"kid": "fixture-key"},
        )

    async def test_valid_token_returns_minimal_scoped_access_token(self):
        access_token = await self.verifier.verify_token(self.token())
        self.assertIsNotNone(access_token)
        self.assertEqual(access_token.subject, "employee-1")
        self.assertEqual(access_token.client_id, "tax-office-client")
        self.assertEqual(access_token.scopes, ["legal.read"])
        self.assertEqual(access_token.resource, "https://legal.example.com")
        self.assertEqual(access_token.claims, {"org_id": "office-1"})

    async def test_invalid_claims_and_algorithm_are_rejected(self):
        cases = {
            "expired": self.claims(exp=int(time.time()) - 1),
            "issuer": self.claims(iss="https://other.example.com"),
            "audience": self.claims(aud="other-service"),
            "resource": self.claims(resource="https://other.example.com"),
            "scope": self.claims(scope="profile.read"),
            "missing_subject": self.claims(sub=None),
            "missing_client": self.claims(client_id=None),
            "missing_org": self.claims(org_id=None),
        }
        for name, claims in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(await self.verifier.verify_token(self.token(claims)))
        fixture_secret = "fixture-secret-with-at-least-32-bytes"
        hs_token = jwt.encode(
            self.claims(),
            fixture_secret,
            algorithm="HS256",
            headers={"kid": "fixture-key"},
        )
        symmetric = JwtTokenVerifier(
            self.configuration,
            jwk_client=StaticJwkClient(fixture_secret),
        )
        self.assertIsNone(await symmetric.verify_token(hs_token))
        self.assertIsNone(await self.verifier.verify_token("not-a-jwt"))
        self.assertIsNone(await self.verifier.verify_token("x" * (16 * 1024 + 1)))

    def test_request_scope_comes_only_from_auth_context(self):
        with patch.object(auth_module, "get_access_token", return_value=None):
            local = current_request_scope()
            with self.assertRaises(PermissionError):
                current_request_scope(auth_required=True)
        self.assertEqual((local.principal_id, local.org_id, local.authenticated), ("local-stdio", "local", False))
        token = AccessToken(
            token="redacted",
            client_id="client",
            scopes=["legal.read"],
            expires_at=int(time.time()) + 600,
            resource="https://legal.example.com",
            subject="employee-1",
            claims={"org_id": "office-1"},
        )
        with patch.object(auth_module, "get_access_token", return_value=token):
            scoped = current_request_scope()
        self.assertEqual((scoped.principal_id, scoped.org_id, scoped.authenticated), ("employee-1", "office-1", True))


class HostedServerConfigurationTests(unittest.TestCase):
    def auth_environment(self):
        return {
            "TAXAX_MCP_AUTH_ISSUER": "https://issuer.example.com",
            "TAXAX_MCP_AUTH_AUDIENCE": "taxax-legal",
            "TAXAX_MCP_AUTH_RESOURCE": "https://legal.example.com",
            "TAXAX_MCP_AUTH_JWKS_URL": "https://issuer.example.com/.well-known/jwks.json",
        }

    def test_partial_auth_configuration_fails_closed(self):
        with patch.dict(
            os.environ,
            {"TAXAX_MCP_AUTH_ISSUER": "https://issuer.example.com"},
            clear=True,
        ):
            with self.assertRaises(SystemExit) as caught:
                server_module.main(["--transport", "streamable-http"])
        self.assertIn("일부만", str(caught.exception))

    def test_hosted_auth_requires_allowlists_and_matching_resource_host(self):
        environment = self.auth_environment()
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(SystemExit) as missing:
                server_module.main(["--transport", "streamable-http", "--host", "0.0.0.0"])
        self.assertIn("ALLOWED_HOSTS", str(missing.exception))

        environment.update(
            {
                "TAXAX_MCP_ALLOWED_HOSTS": "other.example.com",
                "TAXAX_MCP_ALLOWED_ORIGINS": "https://client.example.com",
            }
        )
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(SystemExit) as mismatch:
                server_module.main(["--transport", "streamable-http", "--host", "0.0.0.0"])
        self.assertIn("resource host", str(mismatch.exception))

    def test_valid_hosted_configuration_builds_authenticated_server(self):
        environment = {
            **self.auth_environment(),
            "TAXAX_MCP_ALLOWED_HOSTS": "legal.example.com",
            "TAXAX_MCP_ALLOWED_ORIGINS": "https://client.example.com",
        }
        fake_server = SimpleNamespace(run=lambda **kwargs: None)
        with patch.dict(os.environ, environment, clear=True), patch.object(
            server_module,
            "create_server",
            return_value=fake_server,
        ) as create_server:
            result = server_module.main(
                ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
            )
        self.assertEqual(result, 0)
        configuration = create_server.call_args.kwargs["auth_configuration"]
        self.assertIsInstance(configuration, JwtAuthConfiguration)
        self.assertEqual(configuration.required_scopes, ("legal.read",))

    def test_configuration_rejects_insecure_urls_and_symmetric_algorithms(self):
        with self.assertRaises(ValueError):
            JwtAuthConfiguration(
                issuer_url="http://issuer.example.com",
                audience="taxax-legal",
                resource_server_url="https://legal.example.com",
                jwks_url="https://issuer.example.com/jwks.json",
            )
        with self.assertRaises(ValueError):
            JwtAuthConfiguration(
                issuer_url="https://issuer.example.com",
                audience="taxax-legal",
                resource_server_url="https://legal.example.com",
                jwks_url="https://issuer.example.com/jwks.json",
                algorithms=("HS256",),
            )


if __name__ == "__main__":
    unittest.main()
