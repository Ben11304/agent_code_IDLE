from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .security import pkce_challenge, random_token


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    description: str
    authorize_url: str
    token_url: str
    profile_url: str
    client_id: str
    client_secret: str
    scopes: str
    use_pkce: bool = False
    documentation_url: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def public_dict(self, connection: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "configured": self.configured,
            "connected": connection is not None,
            "account_id": connection.get("account_id") if connection else None,
            "display_name": connection.get("display_name") if connection else None,
            "scopes": (connection.get("scopes") or "").split() if connection else [],
            "expires_at": connection.get("expires_at") if connection else None,
            "connected_at": connection.get("connected_at") if connection else None,
            "updated_at": connection.get("updated_at") if connection else None,
            "documentation_url": self.documentation_url,
        }


def providers_from_env() -> dict[str, Provider]:
    providers = [
        Provider(
            id="github",
            name="GitHub",
            description="Use your GitHub identity for repository access when a dataset requires it.",
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            profile_url="https://api.github.com/user",
            client_id=os.getenv("OC_GITHUB_CLIENT_ID", "").strip(),
            client_secret=os.getenv("OC_GITHUB_CLIENT_SECRET", "").strip(),
            scopes=os.getenv("OC_GITHUB_SCOPES", "read:user").strip(),
            documentation_url="https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps",
        ),
        Provider(
            id="huggingface",
            name="Hugging Face",
            description="Access gated datasets and repositories already approved for your Hugging Face account.",
            authorize_url="https://huggingface.co/oauth/authorize",
            token_url="https://huggingface.co/oauth/token",
            profile_url="https://huggingface.co/api/whoami-v2",
            client_id=os.getenv("OC_HF_CLIENT_ID", "").strip(),
            client_secret=os.getenv("OC_HF_CLIENT_SECRET", "").strip(),
            scopes=os.getenv("OC_HF_SCOPES", "openid profile read-repos gated-repos").strip(),
            use_pkce=True,
            documentation_url="https://huggingface.co/docs/hub/en/oauth",
        ),
        Provider(
            id="baidu",
            name="Baidu Netdisk",
            description="Authorize downloads from Baidu Netdisk links with your own Baidu account and quota.",
            authorize_url="https://openapi.baidu.com/oauth/2.0/authorize",
            token_url="https://openapi.baidu.com/oauth/2.0/token",
            profile_url="https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo",
            client_id=os.getenv("OC_BAIDU_CLIENT_ID", "").strip(),
            client_secret=os.getenv("OC_BAIDU_CLIENT_SECRET", "").strip(),
            scopes=os.getenv("OC_BAIDU_SCOPES", "basic netdisk").strip(),
            documentation_url="https://openauth.baidu.com/doc/doc.html",
        ),
    ]
    return {provider.id: provider for provider in providers}


class ProviderClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client

    def authorization_url(self, provider: Provider, redirect_uri: str, state: str, verifier: str) -> str:
        params = {
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": provider.scopes,
            "state": state,
        }
        if provider.id == "baidu":
            params["display"] = "page"
            params["force_login"] = "0"
        if provider.use_pkce:
            params["code_challenge"] = pkce_challenge(verifier)
            params["code_challenge_method"] = "S256"
        return f"{provider.authorize_url}?{urlencode(params)}"

    async def exchange_code(
        self,
        provider: Provider,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if provider.use_pkce:
            data["code_verifier"] = verifier
        headers = {"Accept": "application/json"}
        if provider.id == "huggingface":
            basic = base64.b64encode(f"{provider.client_id}:{provider.client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
            data.pop("client_secret", None)
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        try:
            response = await client.post(provider.token_url, data=data, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if payload.get("error") or not payload.get("access_token"):
                raise ValueError(payload.get("error_description") or payload.get("error") or "Provider returned no access token")
            return payload
        finally:
            if owns_client:
                await client.aclose()

    async def profile(self, provider: Provider, access_token: str) -> dict[str, str]:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        try:
            if provider.id == "baidu":
                response = await client.get(provider.profile_url, params={"access_token": access_token})
            else:
                response = await client.get(
                    provider.profile_url,
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                )
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns_client:
                await client.aclose()

        if provider.id == "github":
            return {"account_id": str(payload.get("id") or ""), "display_name": str(payload.get("login") or payload.get("name") or "")}
        if provider.id == "huggingface":
            return {"account_id": str(payload.get("id") or payload.get("sub") or ""), "display_name": str(payload.get("name") or payload.get("preferred_username") or payload.get("fullname") or "")}
        return {"account_id": str(payload.get("uk") or payload.get("baidu_name") or ""), "display_name": str(payload.get("netdisk_name") or payload.get("baidu_name") or "")}

    @staticmethod
    def connection_expiry(token_payload: dict[str, Any]) -> int | None:
        try:
            seconds = int(token_payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            return None
        return int(time.time()) + seconds if seconds > 0 else None


def new_provider_verifier() -> str:
    return random_token(48)
