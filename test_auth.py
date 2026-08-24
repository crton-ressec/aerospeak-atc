import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import auth_store
import server


class JsonRequest:
    def __init__(self, body, cookies=None):
        self._body = body
        self.cookies = cookies or {}
        self.state = SimpleNamespace()
        self.client = SimpleNamespace(host="127.0.0.9")

    async def json(self):
        return self._body


def test_scrypt_password_hashing():
    stored = auth_store._password_hash("a-long-personal-password")
    assert auth_store.verify_password("a-long-personal-password", stored)
    assert not auth_store.verify_password("wrong-password", stored)
    assert auth_store.valid_password("twelve_chars")
    assert not auth_store.valid_password("short")


def test_signup_issues_http_only_session_cookie():
    request = JsonRequest({"email": "pilot@example.com", "password": "a-long-personal-password"})
    with patch.object(server.auth_store, "configured", return_value=True), \
         patch.object(server, "ensure_database", return_value=True), \
         patch.object(server.auth_store, "create_user", return_value={"id": 7, "email": "pilot@example.com"}), \
         patch.object(server.auth_store, "issue_session", return_value="x" * 48), \
         patch.object(server.auth_store, "save_state"):
        response = asyncio.run(server.api_auth_signup(request))
    assert response.status_code == 201
    assert "aerospeak_auth=" in response.headers.get("set-cookie", "")


def test_login_required_when_database_enabled():
    request = JsonRequest({})
    state = server._blank_state()
    with patch.object(server.auth_store, "configured", return_value=True), \
         patch.object(server.auth_store, "user_for_session", return_value=None):
        response = server.require_access(request, state)
    assert response.status_code == 401


def test_account_state_restores_known_fields_only():
    state = server._state_from_account({"settings": {"callsign": "ACA144"}, "operation": {"phase": "TAXI_OUT"}, "unexpected": "ignored"})
    assert state["settings"]["callsign"] == "ACA144"
    assert state["operation"]["phase"] == "TAXI_OUT"
    assert "unexpected" not in state


if __name__ == "__main__":
    test_scrypt_password_hashing()
    test_signup_issues_http_only_session_cookie()
    test_login_required_when_database_enabled()
    test_account_state_restores_known_fields_only()
    print("AeroSpeak authentication tests passed")
