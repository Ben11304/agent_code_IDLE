from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .config import Settings


def random_token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce(verifier: str, expected: str) -> bool:
    if not verifier or not expected:
        return False
    try:
        actual = pkce_challenge(verifier)
    except (UnicodeEncodeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def valid_verifier(value: str) -> bool:
    if not 43 <= len(value) <= 128:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
    return all(char in allowed for char in value)


class TokenCipher:
    def __init__(self, key: str):
        if not key:
            raise ValueError("OC_TOKEN_ENCRYPTION_KEY is required to store provider tokens")
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("OC_TOKEN_ENCRYPTION_KEY must be a URL-safe Fernet key") from exc

    def encrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Stored provider token could not be decrypted") from exc


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str = ""


class IdentityVerifier:
    async def verify(self, token: str) -> Identity | None:
        raise NotImplementedError


class SupabaseIdentityVerifier(IdentityVerifier):
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client

    async def verify(self, token: str) -> Identity | None:
        if (
            self.settings.environment == "development"
            and self.settings.dev_user_token
            and self.settings.dev_user_id
            and hmac.compare_digest(token, self.settings.dev_user_token)
        ):
            return Identity(self.settings.dev_user_id, "developer@localhost")
        if not self.settings.supabase_url or not self.settings.supabase_anon_key or not token:
            return None
        headers = {
            "Authorization": f"Bearer {token}",
            "apikey": self.settings.supabase_anon_key,
        }
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=10)
        try:
            response = await client.get(f"{self.settings.supabase_url}/auth/v1/user", headers=headers)
            if response.status_code != 200:
                return None
            payload: dict[str, Any] = response.json()
            user_id = str(payload.get("id") or "").strip()
            if not user_id:
                return None
            return Identity(user_id=user_id, email=str(payload.get("email") or ""))
        except httpx.HTTPError:
            return None
        finally:
            if owns_client:
                await client.aclose()
