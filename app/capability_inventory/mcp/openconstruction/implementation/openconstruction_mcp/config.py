from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _clean_url(value: str) -> str:
    return value.strip().rstrip("/")


@dataclass(frozen=True)
class Settings:
    public_url: str = "http://127.0.0.1:8000"
    site_url: str = "http://127.0.0.1:8000"
    database_path: str = ".openconstruction/state.db"
    supabase_url: str = ""
    supabase_anon_key: str = ""
    token_encryption_key: str = ""
    environment: str = "development"
    dev_user_token: str = ""
    dev_user_id: str = ""
    static_dir: str = ""
    cors_origins: tuple[str, ...] = ()
    connected_accounts_enabled: bool = False

    @property
    def issuer(self) -> str:
        return self.public_url

    @property
    def mcp_resource(self) -> str:
        return f"{self.public_url}/mcp"

    @classmethod
    def from_env(cls) -> "Settings":
        public_url = _clean_url(os.getenv("OC_PUBLIC_URL", "http://127.0.0.1:8000"))
        site_url = _clean_url(os.getenv("OC_SITE_URL", public_url))
        origins = tuple(
            origin.strip().rstrip("/")
            for origin in os.getenv("OC_CORS_ORIGINS", site_url).split(",")
            if origin.strip()
        )
        return cls(
            public_url=public_url,
            site_url=site_url,
            database_path=os.getenv("OC_AUTH_DB", ".openconstruction/state.db"),
            supabase_url=_clean_url(os.getenv("SUPABASE_URL", "")),
            supabase_anon_key=os.getenv("SUPABASE_ANON_KEY", "").strip(),
            token_encryption_key=os.getenv("OC_TOKEN_ENCRYPTION_KEY", "").strip(),
            environment=os.getenv("OC_ENV", "development").strip().lower(),
            dev_user_token=os.getenv("OC_DEV_USER_TOKEN", "").strip(),
            dev_user_id=os.getenv("OC_DEV_USER_ID", "").strip(),
            static_dir=os.getenv("OC_STATIC_DIR", "").strip(),
            cors_origins=origins,
            connected_accounts_enabled=os.getenv("OC_CONNECTED_ACCOUNTS_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"},
        )

    def ensure_state_parent(self) -> None:
        if self.database_path == ":memory:":
            return
        Path(self.database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
