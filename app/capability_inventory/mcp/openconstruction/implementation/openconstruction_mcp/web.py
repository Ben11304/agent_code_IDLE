from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .providers import ProviderClient, new_provider_verifier, providers_from_env
from .security import Identity, IdentityVerifier, SupabaseIdentityVerifier, TokenCipher, pkce_challenge, valid_verifier, verify_pkce
from .server import handle_request
from .store import Store


LOGGER = logging.getLogger("openconstruction.remote")
MCP_SCOPE = "mcp:tools"


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


def valid_registered_redirect(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    if parsed.fragment or parsed.username or parsed.password or not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def redirect_matches(requested: str, registered: list[str]) -> bool:
    if requested in registered:
        return True
    try:
        wanted = urlparse(requested)
    except ValueError:
        return False
    if wanted.scheme != "http" or wanted.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    for item in registered:
        try:
            saved = urlparse(item)
        except ValueError:
            continue
        if (
            saved.scheme == wanted.scheme
            and saved.hostname == wanted.hostname
            and saved.path == wanted.path
            and saved.query == wanted.query
            and saved.fragment == wanted.fragment
        ):
            return True
    return False


async def request_data(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    raw = (await request.body()).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


def connection_redirect(settings: Settings, provider: str, status: str, message: str = "") -> str:
    query = {"tab": "connections", "connection": status, "provider": provider}
    if message:
        query["message"] = message[:160]
    return f"{settings.site_url}/account.html?{urlencode(query)}"


def append_query(uri: str, values: dict[str, str]) -> str:
    parsed = urlparse(uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in values.items() if value != "")
    return urlunparse(parsed._replace(query=urlencode(query)))


def create_app(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    identity_verifier: IdentityVerifier | None = None,
    provider_client: ProviderClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_state_parent()
    store = store or Store(settings.database_path)
    identity_verifier = identity_verifier or SupabaseIdentityVerifier(settings)
    provider_client = provider_client or ProviderClient()
    providers = providers_from_env()

    app = FastAPI(
        title="OpenConstruction Remote MCP",
        version="0.1.0",
        docs_url="/api/docs" if settings.environment == "development" else None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.identity_verifier = identity_verifier
    app.state.provider_client = provider_client
    app.state.providers = providers

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "MCP-Protocol-Version"],
        )

    async def supabase_identity(request: Request) -> Identity:
        identity = await identity_verifier.verify(bearer_token(request))
        if not identity:
            raise HTTPException(status_code=401, detail="A valid OpenConstruction session is required")
        return identity

    def require_connected_accounts() -> None:
        if not settings.connected_accounts_enabled:
            raise HTTPException(status_code=404, detail="Connected Accounts is not enabled on this OC deployment")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def protected_resource_metadata() -> dict[str, Any]:
        return {
            "resource": settings.mcp_resource,
            "authorization_servers": [settings.issuer],
            "scopes_supported": [MCP_SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_documentation": f"{settings.site_url}/mcp.html",
        }

    @app.get("/.well-known/oauth-authorization-server")
    async def authorization_server_metadata() -> dict[str, Any]:
        return {
            "issuer": settings.issuer,
            "authorization_endpoint": f"{settings.issuer}/authorize",
            "token_endpoint": f"{settings.issuer}/token",
            "registration_endpoint": f"{settings.issuer}/register",
            "revocation_endpoint": f"{settings.issuer}/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [MCP_SCOPE],
        }

    @app.post("/register")
    async def register(request: Request) -> JSONResponse:
        payload = await request_data(request)
        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return oauth_error("invalid_redirect_uri", "redirect_uris must be a non-empty array")
        if len(redirect_uris) > 10 or any(not isinstance(uri, str) or not valid_registered_redirect(uri) for uri in redirect_uris):
            return oauth_error("invalid_redirect_uri", "Redirect URIs must use HTTPS or an HTTP loopback address and cannot contain fragments")
        if payload.get("token_endpoint_auth_method", "none") != "none":
            return oauth_error("invalid_client_metadata", "Only public clients with token_endpoint_auth_method=none are supported")
        client = store.register_client(str(payload.get("client_name") or "MCP client")[:120], redirect_uris)
        return JSONResponse(
            {
                **client,
                "client_id_issued_at": client["created_at"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
            status_code=201,
        )

    @app.get("/authorize")
    async def authorize(request: Request) -> Response:
        query = request.query_params
        client_id = query.get("client_id", "")
        redirect_uri = query.get("redirect_uri", "")
        client = store.get_client(client_id)
        if not client or not redirect_matches(redirect_uri, client["redirect_uris"]):
            return oauth_error("invalid_request", "Unknown client or redirect_uri", 400)
        if query.get("response_type") != "code":
            return oauth_error("unsupported_response_type", "Only response_type=code is supported")
        if query.get("code_challenge_method") != "S256" or not query.get("code_challenge"):
            return oauth_error("invalid_request", "PKCE with code_challenge_method=S256 is required")
        resource = query.get("resource", "")
        if resource != settings.mcp_resource:
            return oauth_error("invalid_target", f"resource must be {settings.mcp_resource}")
        requested_scopes = set(query.get("scope", MCP_SCOPE).split())
        if MCP_SCOPE not in requested_scopes:
            return oauth_error("invalid_scope", f"The {MCP_SCOPE} scope is required")
        request_id = store.create_oauth_request(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": query["code_challenge"],
                "state": query.get("state", ""),
                "scope": MCP_SCOPE,
                "resource": resource,
            }
        )
        return RedirectResponse(f"{settings.site_url}/auth/mcp-authorize.html?request_id={request_id}", status_code=302)

    @app.get("/oauth/request/{request_id}")
    async def oauth_request_summary(request_id: str) -> dict[str, Any]:
        with store.connection() as connection:
            row = connection.execute(
                """SELECT r.request_id,r.scope,r.expires_at,c.client_name
                FROM oauth_requests r JOIN oauth_clients c ON c.client_id=r.client_id
                WHERE r.request_id=? AND r.expires_at>?""",
                (request_id, store.now()),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Authorization request is invalid or expired")
        return dict(row)

    @app.post("/oauth/consent")
    async def oauth_consent(request: Request) -> JSONResponse:
        identity = await supabase_identity(request)
        payload = await request_data(request)
        request_id = str(payload.get("request_id") or "")
        authorization = store.consume_oauth_request(request_id)
        if not authorization:
            return oauth_error("invalid_request", "Authorization request is invalid or expired")
        redirect_uri = authorization["redirect_uri"]
        if payload.get("approved", True) is False:
            params = {"error": "access_denied", "state": authorization["state"]}
        else:
            code = store.create_code(identity.user_id, authorization)
            params = {"code": code, "state": authorization["state"]}
        return JSONResponse({"redirect_to": append_query(redirect_uri, params)})

    @app.post("/token")
    async def token(request: Request) -> JSONResponse:
        payload = await request_data(request)
        grant_type = str(payload.get("grant_type") or "")
        client_id = str(payload.get("client_id") or "")
        if not store.get_client(client_id):
            return oauth_error("invalid_client", "Unknown client", 401)
        resource = str(payload.get("resource") or "")
        if resource != settings.mcp_resource:
            return oauth_error("invalid_target", f"resource must be {settings.mcp_resource}")
        if grant_type == "authorization_code":
            verifier = str(payload.get("code_verifier") or "")
            if not valid_verifier(verifier):
                return oauth_error("invalid_grant", "Invalid PKCE code_verifier")
            expected_challenge = ""
            try:
                expected_challenge = pkce_challenge(verifier)
            except (UnicodeEncodeError, ValueError):
                pass
            code = store.consume_code(
                str(payload.get("code") or ""),
                client_id=client_id,
                redirect_uri=str(payload.get("redirect_uri") or ""),
                code_challenge=expected_challenge,
                resource=resource,
            )
            if not code:
                return oauth_error("invalid_grant", "Authorization code is invalid, expired, or already used")
            if not verify_pkce(verifier, code["code_challenge"]):
                return oauth_error("invalid_grant", "Authorization code validation failed")
            return JSONResponse(store.issue_tokens(code["user_id"], client_id, code["scope"], resource))
        if grant_type == "refresh_token":
            tokens = store.rotate_refresh_token(str(payload.get("refresh_token") or ""), client_id, resource)
            if not tokens:
                return oauth_error("invalid_grant", "Refresh token is invalid, expired, or already used")
            return JSONResponse(tokens)
        return oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token")

    @app.post("/revoke")
    async def revoke(request: Request) -> Response:
        payload = await request_data(request)
        store.revoke_token(str(payload.get("token") or ""))
        return Response(status_code=200)

    def mcp_challenge(error: str = "invalid_token") -> JSONResponse:
        metadata = f"{settings.issuer}/.well-known/oauth-protected-resource/mcp"
        headers = {"WWW-Authenticate": f'Bearer error="{error}", scope="{MCP_SCOPE}", resource_metadata="{metadata}"'}
        return JSONResponse({"error": error}, status_code=401, headers=headers)

    @app.post("/mcp")
    async def remote_mcp(request: Request) -> Response:
        token_value = bearer_token(request)
        token_row = store.authenticate_access_token(token_value, settings.mcp_resource) if token_value else None
        if not token_row:
            return mcp_challenge()
        if MCP_SCOPE not in set(str(token_row["scope"]).split()):
            metadata = f"{settings.issuer}/.well-known/oauth-protected-resource/mcp"
            return JSONResponse(
                {"error": "insufficient_scope"},
                status_code=403,
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{MCP_SCOPE}", resource_metadata="{metadata}"'},
            )
        try:
            message = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
        if not isinstance(message, dict):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}, status_code=400)
        result = handle_request(message, allow_download_execution=False)
        if result is None:
            return Response(status_code=202)
        return JSONResponse(result, media_type="application/json")

    @app.get("/mcp")
    async def remote_mcp_get(request: Request) -> Response:
        if not bearer_token(request) or not store.authenticate_access_token(bearer_token(request), settings.mcp_resource):
            return mcp_challenge()
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.get("/api/connections")
    async def list_connections(request: Request) -> dict[str, Any]:
        require_connected_accounts()
        identity = await supabase_identity(request)
        rows = {row["provider"]: row for row in store.list_connections(identity.user_id)}
        return {"providers": [provider.public_dict(rows.get(provider.id)) for provider in providers.values()]}

    @app.post("/api/connections/{provider_id}/start")
    async def start_connection(provider_id: str, request: Request) -> JSONResponse:
        require_connected_accounts()
        identity = await supabase_identity(request)
        provider = providers.get(provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Unknown provider")
        if not provider.configured:
            raise HTTPException(status_code=503, detail=f"{provider.name} OAuth is not configured on this OC deployment")
        redirect_uri = f"{settings.public_url}/api/connections/{provider.id}/callback"
        verifier = new_provider_verifier()
        state = store.create_provider_flow(identity.user_id, provider.id, verifier, redirect_uri)
        return JSONResponse({"authorization_url": provider_client.authorization_url(provider, redirect_uri, state, verifier)})

    @app.get("/api/connections/{provider_id}/callback")
    async def connection_callback(provider_id: str, request: Request) -> Response:
        require_connected_accounts()
        provider = providers.get(provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Unknown provider")
        provider_error = request.query_params.get("error", "")
        state = request.query_params.get("state", "")
        flow = store.consume_provider_flow(state, provider_id) if state else None
        if not flow:
            return RedirectResponse(connection_redirect(settings, provider_id, "error", "The connection request expired or failed state validation"))
        if provider_error:
            return RedirectResponse(connection_redirect(settings, provider_id, "error", request.query_params.get("error_description", provider_error)))
        code = request.query_params.get("code", "")
        if not code:
            return RedirectResponse(connection_redirect(settings, provider_id, "error", "The provider returned no authorization code"))
        try:
            cipher = TokenCipher(settings.token_encryption_key)
            token_payload = await provider_client.exchange_code(provider, code, flow["redirect_uri"], flow.get("code_verifier") or "")
            access_token = str(token_payload["access_token"])
            profile = await provider_client.profile(provider, access_token)
            scopes = token_payload.get("scope") or provider.scopes
            if isinstance(scopes, list):
                scopes = " ".join(str(item) for item in scopes)
            store.upsert_connection(
                {
                    "user_id": flow["user_id"],
                    "provider": provider.id,
                    "account_id": profile.get("account_id"),
                    "display_name": profile.get("display_name"),
                    "scopes": str(scopes),
                    "encrypted_access_token": cipher.encrypt(access_token),
                    "encrypted_refresh_token": cipher.encrypt(str(token_payload.get("refresh_token") or "")),
                    "expires_at": provider_client.connection_expiry(token_payload),
                }
            )
        except Exception as exc:
            LOGGER.exception("Provider callback failed for %s", provider_id)
            return RedirectResponse(connection_redirect(settings, provider_id, "error", str(exc)))
        return RedirectResponse(connection_redirect(settings, provider_id, "success"))

    @app.delete("/api/connections/{provider_id}")
    async def disconnect(provider_id: str, request: Request) -> dict[str, Any]:
        require_connected_accounts()
        identity = await supabase_identity(request)
        if provider_id not in providers:
            raise HTTPException(status_code=404, detail="Unknown provider")
        return {"disconnected": store.delete_connection(identity.user_id, provider_id)}

    static_dir = Path(settings.static_dir).expanduser().resolve() if settings.static_dir else None
    if static_dir and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="site")

    return app


def main() -> None:
    settings = Settings.from_env()
    parsed = urlparse(settings.public_url)
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=parsed.port or (443 if parsed.scheme == "https" else 8000),
        reload=False,
    )


if __name__ == "__main__":
    main()
