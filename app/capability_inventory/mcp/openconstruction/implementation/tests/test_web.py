import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.fernet import Fernet

from openconstruction_mcp.config import Settings
from openconstruction_mcp.providers import ProviderClient
from openconstruction_mcp.security import Identity, IdentityVerifier, TokenCipher, pkce_challenge
from openconstruction_mcp.store import Store
from openconstruction_mcp.web import create_app


class FakeIdentityVerifier(IdentityVerifier):
    async def verify(self, token):
        return Identity("user-123", "user@example.org") if token == "oc-session" else None


class FakeProviderClient(ProviderClient):
    async def exchange_code(self, provider, code, redirect_uri, verifier):
        if code != "provider-code":
            raise ValueError("bad code")
        return {
            "access_token": "provider-access-secret",
            "refresh_token": "provider-refresh-secret",
            "expires_in": 3600,
            "scope": provider.scopes,
        }

    async def profile(self, provider, access_token):
        return {"account_id": "hf-42", "display_name": "oc-researcher"}


class RemoteMcpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            public_url="https://api.example.test",
            site_url="https://www.example.test",
            database_path=os.path.join(self.tmp.name, "state.db"),
            token_encryption_key=Fernet.generate_key().decode(),
            environment="test",
        )
        self.store = Store(self.settings.database_path)
        app = create_app(self.settings, store=self.store, identity_verifier=FakeIdentityVerifier())
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.example.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def register_client(self):
        response = await self.client.post(
            "/register",
            json={
                "client_name": "Test MCP Client",
                "redirect_uris": ["http://127.0.0.1/callback"],
                "token_endpoint_auth_method": "none",
            },
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["client_id"]

    async def authorize(self, client_id):
        verifier = "a" * 64
        response = await self.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:49282/callback",
                "scope": "mcp:tools",
                "state": "client-state",
                "code_challenge": pkce_challenge(verifier),
                "code_challenge_method": "S256",
                "resource": self.settings.mcp_resource,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        request_id = parse_qs(urlparse(response.headers["location"]).query)["request_id"][0]
        consent = await self.client.post(
            "/oauth/consent",
            headers={"Authorization": "Bearer oc-session"},
            json={"request_id": request_id, "approved": True},
        )
        self.assertEqual(consent.status_code, 200)
        redirect = urlparse(consent.json()["redirect_to"])
        values = parse_qs(redirect.query)
        self.assertEqual(values["state"], ["client-state"])
        return values["code"][0], verifier

    async def test_oauth_pkce_to_remote_mcp_and_refresh_rotation(self):
        metadata = (await self.client.get("/.well-known/oauth-protected-resource/mcp")).json()
        self.assertEqual(metadata["resource"], self.settings.mcp_resource)
        self.assertEqual(metadata["authorization_servers"], [self.settings.issuer])

        unauthenticated = await self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertIn("resource_metadata", unauthenticated.headers["www-authenticate"])

        client_id = await self.register_client()
        code, verifier = await self.authorize(client_id)
        wrong_pkce = await self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": "http://127.0.0.1:49282/callback",
                "code_verifier": "b" * 64,
                "resource": self.settings.mcp_resource,
            },
        )
        self.assertEqual(wrong_pkce.status_code, 400)
        token_response = await self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": "http://127.0.0.1:49282/callback",
                "code_verifier": verifier,
                "resource": self.settings.mcp_resource,
            },
        )
        self.assertEqual(token_response.status_code, 200)
        tokens = token_response.json()

        replay = await self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": "http://127.0.0.1:49282/callback",
                "code_verifier": verifier,
                "resource": self.settings.mcp_resource,
            },
        )
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.json()["error"], "invalid_grant")

        mcp = await self.client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
        )
        self.assertEqual(mcp.status_code, 200)
        self.assertIn("search_resources", [tool["name"] for tool in mcp.json()["result"]["tools"]])

        refresh = await self.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
                "resource": self.settings.mcp_resource,
            },
        )
        self.assertEqual(refresh.status_code, 200)
        reused_refresh = await self.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
                "resource": self.settings.mcp_resource,
            },
        )
        self.assertEqual(reused_refresh.status_code, 400)

    async def test_rejects_unsafe_redirects_and_missing_pkce(self):
        unsafe = await self.client.post("/register", json={"redirect_uris": ["http://evil.example/callback"]})
        self.assertEqual(unsafe.status_code, 400)
        client_id = await self.register_client()
        response = await self.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1/callback",
                "resource": self.settings.mcp_resource,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")


class ConnectedAccountsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.settings = Settings(
            public_url="https://api.example.test",
            site_url="https://www.example.test",
            database_path=os.path.join(self.tmp.name, "state.db"),
            token_encryption_key=self.key,
            environment="test",
            connected_accounts_enabled=True,
        )
        self.store = Store(self.settings.database_path)
        env = {
            "OC_HF_CLIENT_ID": "hf-client",
            "OC_HF_CLIENT_SECRET": "hf-secret",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        app = create_app(
            self.settings,
            store=self.store,
            identity_verifier=FakeIdentityVerifier(),
            provider_client=FakeProviderClient(),
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.example.test")
        self.headers = {"Authorization": "Bearer oc-session"}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env_patch.stop()
        self.tmp.cleanup()

    async def test_connect_list_without_secrets_and_disconnect(self):
        providers = await self.client.get("/api/connections", headers=self.headers)
        self.assertEqual(providers.status_code, 200)
        huggingface = next(item for item in providers.json()["providers"] if item["id"] == "huggingface")
        self.assertTrue(huggingface["configured"])
        self.assertFalse(huggingface["connected"])

        start = await self.client.post("/api/connections/huggingface/start", headers=self.headers)
        self.assertEqual(start.status_code, 200)
        authorization_url = start.json()["authorization_url"]
        query = parse_qs(urlparse(authorization_url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        state = query["state"][0]

        callback = await self.client.get(
            "/api/connections/huggingface/callback",
            params={"state": state, "code": "provider-code"},
            follow_redirects=False,
        )
        self.assertEqual(callback.status_code, 307)
        self.assertIn("connection=success", callback.headers["location"])

        connected = (await self.client.get("/api/connections", headers=self.headers)).json()
        huggingface = next(item for item in connected["providers"] if item["id"] == "huggingface")
        self.assertTrue(huggingface["connected"])
        self.assertEqual(huggingface["display_name"], "oc-researcher")
        self.assertNotIn("access_token", str(connected))
        self.assertNotIn("provider-access-secret", str(connected))

        stored = self.store.get_connection_secret("user-123", "huggingface")
        cipher = TokenCipher(self.key)
        self.assertEqual(cipher.decrypt(stored["encrypted_access_token"]), "provider-access-secret")
        self.assertNotEqual(stored["encrypted_access_token"], "provider-access-secret")

        disconnected = await self.client.delete("/api/connections/huggingface", headers=self.headers)
        self.assertEqual(disconnected.status_code, 200)
        self.assertTrue(disconnected.json()["disconnected"])

    async def test_connections_require_oc_session(self):
        response = await self.client.get("/api/connections")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
