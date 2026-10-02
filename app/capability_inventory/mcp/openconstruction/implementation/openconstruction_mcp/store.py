from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .security import random_token, token_hash


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY,
  client_name TEXT NOT NULL,
  redirect_uris TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_requests (
  request_id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  state TEXT NOT NULL,
  scope TEXT NOT NULL,
  resource TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  scope TEXT NOT NULL,
  resource TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER
);

CREATE TABLE IF NOT EXISTS access_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  resource TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  resource TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER
);

CREATE TABLE IF NOT EXISTS provider_flows (
  state_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  code_verifier TEXT,
  redirect_uri TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_connections (
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  account_id TEXT,
  display_name TEXT,
  scopes TEXT NOT NULL,
  encrypted_access_token TEXT NOT NULL,
  encrypted_refresh_token TEXT,
  expires_at INTEGER,
  connected_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_access_user ON access_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_connections_user ON provider_connections(user_id);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if path == ":memory:":
            self._memory_connection = self._new_connection(path)
        else:
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(SCHEMA)

    @staticmethod
    def _new_connection(path: str) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._memory_connection or self._new_connection(self.path)
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                if self._memory_connection is None:
                    connection.close()

    @staticmethod
    def now() -> int:
        return int(time.time())

    def register_client(self, client_name: str, redirect_uris: list[str]) -> dict[str, Any]:
        client_id = random_token(24)
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO oauth_clients(client_id,client_name,redirect_uris,created_at) VALUES(?,?,?,?)",
                (client_id, client_name, json.dumps(redirect_uris), now),
            )
        return {"client_id": client_id, "client_name": client_name, "redirect_uris": redirect_uris, "created_at": now}

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["redirect_uris"] = json.loads(result["redirect_uris"])
        return result

    def create_oauth_request(self, values: dict[str, Any], ttl: int = 600) -> str:
        request_id = random_token(32)
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO oauth_requests
                (request_id,client_id,redirect_uri,code_challenge,state,scope,resource,expires_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    values["client_id"],
                    values["redirect_uri"],
                    values["code_challenge"],
                    values.get("state", ""),
                    values["scope"],
                    values["resource"],
                    self.now() + ttl,
                ),
            )
        return request_id

    def consume_oauth_request(self, request_id: str) -> dict[str, Any] | None:
        now = self.now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_requests WHERE request_id=? AND expires_at>?",
                (request_id, now),
            ).fetchone()
            if row:
                connection.execute("DELETE FROM oauth_requests WHERE request_id=?", (request_id,))
        return dict(row) if row else None

    def create_code(self, user_id: str, request: dict[str, Any], ttl: int = 300) -> str:
        code = random_token(32)
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO oauth_codes
                (code_hash,user_id,client_id,redirect_uri,code_challenge,scope,resource,expires_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    token_hash(code), user_id, request["client_id"], request["redirect_uri"],
                    request["code_challenge"], request["scope"], request["resource"], self.now() + ttl,
                ),
            )
        return code

    def consume_code(
        self,
        code: str,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        resource: str,
    ) -> dict[str, Any] | None:
        now = self.now()
        digest = token_hash(code)
        with self.connection() as connection:
            row = connection.execute(
                """SELECT * FROM oauth_codes
                WHERE code_hash=? AND client_id=? AND redirect_uri=? AND code_challenge=? AND resource=?
                  AND used_at IS NULL AND expires_at>?""",
                (digest, client_id, redirect_uri, code_challenge, resource, now),
            ).fetchone()
            if row:
                cursor = connection.execute(
                    "UPDATE oauth_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL",
                    (now, digest),
                )
                if cursor.rowcount != 1:
                    row = None
        return dict(row) if row else None

    def issue_tokens(self, user_id: str, client_id: str, scope: str, resource: str) -> dict[str, Any]:
        access_token = random_token(40)
        refresh_token = random_token(48)
        now = self.now()
        access_expires = now + 3600
        refresh_expires = now + 30 * 24 * 3600
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO access_tokens VALUES(?,?,?,?,?,?,NULL)",
                (token_hash(access_token), user_id, client_id, scope, resource, access_expires),
            )
            connection.execute(
                "INSERT INTO refresh_tokens VALUES(?,?,?,?,?,?,NULL)",
                (token_hash(refresh_token), user_id, client_id, scope, resource, refresh_expires),
            )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": access_expires - now,
            "refresh_token": refresh_token,
            "scope": scope,
        }

    def authenticate_access_token(self, token: str, resource: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT * FROM access_tokens
                WHERE token_hash=? AND resource=? AND revoked_at IS NULL AND expires_at>?""",
                (token_hash(token), resource, self.now()),
            ).fetchone()
        return dict(row) if row else None

    def rotate_refresh_token(self, token: str, client_id: str, resource: str) -> dict[str, Any] | None:
        now = self.now()
        digest = token_hash(token)
        with self.connection() as connection:
            row = connection.execute(
                """SELECT * FROM refresh_tokens
                WHERE token_hash=? AND client_id=? AND resource=? AND revoked_at IS NULL AND expires_at>?""",
                (digest, client_id, resource, now),
            ).fetchone()
            if row:
                connection.execute("UPDATE refresh_tokens SET revoked_at=? WHERE token_hash=?", (now, digest))
        if not row:
            return None
        return self.issue_tokens(row["user_id"], row["client_id"], row["scope"], row["resource"])

    def revoke_token(self, token: str) -> None:
        digest = token_hash(token)
        now = self.now()
        with self.connection() as connection:
            connection.execute("UPDATE access_tokens SET revoked_at=? WHERE token_hash=?", (now, digest))
            connection.execute("UPDATE refresh_tokens SET revoked_at=? WHERE token_hash=?", (now, digest))

    def create_provider_flow(self, user_id: str, provider: str, verifier: str, redirect_uri: str, ttl: int = 600) -> str:
        state = random_token(32)
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO provider_flows VALUES(?,?,?,?,?,?)",
                (token_hash(state), user_id, provider, verifier, redirect_uri, self.now() + ttl),
            )
        return state

    def consume_provider_flow(self, state: str, provider: str) -> dict[str, Any] | None:
        digest = token_hash(state)
        now = self.now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM provider_flows WHERE state_hash=? AND provider=? AND expires_at>?",
                (digest, provider, now),
            ).fetchone()
            if row:
                connection.execute("DELETE FROM provider_flows WHERE state_hash=?", (digest,))
        return dict(row) if row else None

    def upsert_connection(self, values: dict[str, Any]) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO provider_connections
                (user_id,provider,account_id,display_name,scopes,encrypted_access_token,
                 encrypted_refresh_token,expires_at,connected_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id,provider) DO UPDATE SET
                  account_id=excluded.account_id,
                  display_name=excluded.display_name,
                  scopes=excluded.scopes,
                  encrypted_access_token=excluded.encrypted_access_token,
                  encrypted_refresh_token=excluded.encrypted_refresh_token,
                  expires_at=excluded.expires_at,
                  updated_at=excluded.updated_at""",
                (
                    values["user_id"], values["provider"], values.get("account_id"),
                    values.get("display_name"), values.get("scopes", ""), values["encrypted_access_token"],
                    values.get("encrypted_refresh_token"), values.get("expires_at"), now, now,
                ),
            )

    def list_connections(self, user_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT provider,account_id,display_name,scopes,expires_at,connected_at,updated_at
                FROM provider_connections WHERE user_id=? ORDER BY provider""",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_connection(self, user_id: str, provider: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM provider_connections WHERE user_id=? AND provider=?",
                (user_id, provider),
            )
        return cursor.rowcount > 0

    def get_connection_secret(self, user_id: str, provider: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM provider_connections WHERE user_id=? AND provider=?",
                (user_id, provider),
            ).fetchone()
        return dict(row) if row else None
