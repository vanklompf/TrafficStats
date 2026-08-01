import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import auth


OIDC_ENV = {
    "OIDC_ENABLED": "true",
    "OIDC_CLIENT_ID": "trafficstats",
    "OIDC_CLIENT_SECRET": "client-secret",
    "OIDC_ISSUER_URL": "https://idp.example/application/o/trafficstats/",
    "APP_URL": "https://trafficstats.example",
    "SESSION_SECRET": "a" * 32,
}


class SessionSecurityTests(unittest.TestCase):
    def test_oidc_requires_persistent_session_secret(self):
        env = {**OIDC_ENV}
        env.pop("SESSION_SECRET")
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SESSION_SECRET is required"):
                auth.session_secret()

    def test_oidc_rejects_short_session_secret(self):
        with patch.dict(
            os.environ, {**OIDC_ENV, "SESSION_SECRET": "guessable"}, clear=True
        ):
            with self.assertRaisesRegex(RuntimeError, "at least 32 bytes"):
                auth.session_secret()

    def test_current_user_requires_nonempty_subject(self):
        for user in ({}, {"sub": None}, {"sub": "  "}):
            request = SimpleNamespace(session={auth.SESSION_USER_KEY: user})
            self.assertIsNone(auth.current_user(request))

        user = {"sub": "user-123"}
        request = SimpleNamespace(session={auth.SESSION_USER_KEY: user})
        self.assertEqual(auth.current_user(request), user)


class OAuthConfigurationTests(unittest.TestCase):
    def test_oidc_requires_https_app_url(self):
        with patch.dict(
            os.environ, {**OIDC_ENV, "APP_URL": "http://trafficstats.example"}, clear=True
        ):
            with self.assertRaisesRegex(RuntimeError, "canonical HTTPS URL"):
                auth.build_oauth()

    def test_openid_scope_cannot_be_removed(self):
        registry = SimpleNamespace()
        registry.register = lambda **kwargs: setattr(registry, "registration", kwargs)
        with patch.dict(
            os.environ, {**OIDC_ENV, "OIDC_SCOPES": "email profile"}, clear=True
        ), patch.object(auth, "OAuth", return_value=registry):
            self.assertIs(auth.build_oauth(), registry)

        scopes = registry.registration["client_kwargs"]["scope"].split()
        self.assertIn("openid", scopes)


class CallbackValidationTests(unittest.IsolatedAsyncioTestCase):
    async def _callback(self, token):
        class Client:
            async def authorize_access_token(self, request):
                return token

        request = SimpleNamespace(session={"oidc_next": "/"})
        response = await auth.callback(request, SimpleNamespace(oidc=Client()))
        return request, response

    async def test_callback_requires_id_token(self):
        request, response = await self._callback(
            {"userinfo": {"sub": "user-123"}}
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(auth.SESSION_USER_KEY, request.session)

    async def test_callback_requires_validated_subject(self):
        request, response = await self._callback(
            {"id_token": "encoded-token", "userinfo": {}}
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(auth.SESSION_USER_KEY, request.session)

    async def test_callback_stores_validated_identity(self):
        request, response = await self._callback(
            {
                "id_token": "encoded-token",
                "userinfo": {"sub": "user-123", "email": "user@example.com"},
            }
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(request.session[auth.SESSION_USER_KEY]["sub"], "user-123")


if __name__ == "__main__":
    unittest.main()
