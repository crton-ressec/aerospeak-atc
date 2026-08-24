"""PostgreSQL-backed accounts and durable per-user AeroSpeak state."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SESSION_DAYS = 30


def configured() -> bool:
    return bool(DATABASE_URL)


def _connection():
    if not DATABASE_URL:
        raise RuntimeError("Database is not configured.")
    return psycopg.connect(DATABASE_URL, connect_timeout=8, autocommit=True)


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def valid_password(password: str) -> bool:
    return isinstance(password, str) and len(password) >= 12 and len(password) <= 256


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_value, digest_value = stored.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode())
        expected = base64.urlsafe_b64decode(digest_value.encode())
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def initialize():
    if not configured():
        return False
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash CHAR(64) PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS auth_sessions_user_idx ON auth_sessions(user_id);
            CREATE TABLE IF NOT EXISTS account_states (
                user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                state JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
    return True


def create_user(email: str, password: str) -> dict:
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized or len(normalized) > 254:
        raise ValueError("Enter a valid email address.")
    if not valid_password(password):
        raise ValueError("Use a password with at least 12 characters.")
    try:
        with _connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id, email", (normalized, _password_hash(password)))
            row = cursor.fetchone()
            cursor.execute("INSERT INTO account_states (user_id, state) VALUES (%s, %s)", (row[0], Jsonb({})))
            return {"id": int(row[0]), "email": row[1]}
    except psycopg.errors.UniqueViolation as error:
        raise ValueError("An account already exists for that email address.") from error


def authenticate(email: str, password: str) -> dict | None:
    normalized = (email or "").strip().lower()
    if not normalized or not password:
        return None
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id, email, password_hash FROM users WHERE email = %s", (normalized,))
        row = cursor.fetchone()
    if not row or not verify_password(password, row[2]):
        return None
    return {"id": int(row[0]), "email": row[1]}


def issue_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("INSERT INTO auth_sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)", (token_digest(token), user_id, expires))
    return token


def user_for_session(token: str) -> dict | None:
    if not token or len(token) < 32:
        return None
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("""
            SELECT u.id, u.email
            FROM auth_sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.expires_at > NOW()
        """, (token_digest(token),))
        row = cursor.fetchone()
    return {"id": int(row[0]), "email": row[1]} if row else None


def revoke_session(token: str):
    if not token:
        return
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM auth_sessions WHERE token_hash = %s", (token_digest(token),))


def load_state(user_id: int) -> dict:
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM account_states WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
    return row[0] if row and isinstance(row[0], dict) else {}


def save_state(user_id: int, state: dict):
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO account_states (user_id, state, updated_at) VALUES (%s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state, updated_at = NOW()
        """, (user_id, Jsonb(state)))


def free_tier_notice() -> str:
    return "Personal account data is stored on a Free Render PostgreSQL database that expires 30 days after creation; export or upgrade it before expiration."
