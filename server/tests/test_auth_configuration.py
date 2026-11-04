import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.common.database import Base, get_db
from app.config import load_config
from app.main import app
from app.modules.roles.models import RoleModel
from app.modules.stream.schemas import StreamToken
from app.modules.stream.utils.stream_token import (
    generate_stream_token,
    parse_stream_token,
    serializer,
)
from app.modules.users.models import UserModel


@pytest.mark.parametrize("secret", [None, "", "stremhu-source", "short-marker", " " * 32])
def test_invalid_signing_configuration_does_not_disclose_input(monkeypatch, secret):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    values = {} if secret is None else {"session_secret": secret}
    with pytest.raises(ValidationError) as error:
        load_config(load_dotenv=False, host_ip="192.0.2.1", **values)
    assert "session_secret" in str(error.value)
    if secret and secret.strip():
        assert secret not in str(error.value)


def test_dotenv_is_never_opened_in_isolated_loader(monkeypatch):
    original = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.name == ".env":
            pytest.fail("isolated configuration attempted dotenv access")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    key = secrets.token_urlsafe(48)
    first = load_config(load_dotenv=False, host_ip="192.0.2.1", session_secret=key)
    second = load_config(load_dotenv=False, host_ip="192.0.2.1", session_secret=key)
    assert first.session_secret == second.session_secret == key
    assert key not in repr(first)


def session_app(key):
    application = FastAPI()
    application.add_middleware(SessionMiddleware, secret_key=key)

    @application.post("/issue")
    def issue(request: Request):
        request.session["user_id"] = "fixture-user"
        return {}

    @application.get("/identity")
    def identity(request: Request):
        return {"user_id": request.session.get("user_id")}

    return application


def test_actual_session_middleware_survives_restart_and_rejects_other_key():
    key = secrets.token_urlsafe(48)
    issuing = TestClient(session_app(key))
    assert issuing.post("/issue").status_code == 200
    restarted = TestClient(session_app(key))
    restarted.cookies.update(issuing.cookies)
    assert restarted.get("/identity").json() == {"user_id": "fixture-user"}
    other = TestClient(session_app(secrets.token_urlsafe(48)))
    other.cookies.update(issuing.cookies)
    assert other.get("/identity").json() == {"user_id": None}


def test_stream_token_uses_configured_key_and_existing_salt():
    payload = StreamToken(indexer_id="fixture", torrent_id="1", file_index=0, playback_id="p")
    token = generate_stream_token(payload)
    assert parse_stream_token(token) == payload
    from app.config import config

    restarted = URLSafeSerializer(config.session_secret, salt="stream-token")
    assert restarted.loads(token) == payload.model_dump()
    foreign = URLSafeSerializer(secrets.token_urlsafe(48), salt="stream-token")
    with pytest.raises(BadSignature):
        foreign.loads(token)
    with pytest.raises(BadSignature):
        serializer.loads(foreign.dumps(payload.model_dump()))


def test_app_import_has_no_dotenv_fallback_with_missing_secret(tmp_path):
    # A tempting dotenv must not repair an incomplete explicit test environment.
    (tmp_path / ".env").write_text("SESSION_SECRET=" + secrets.token_urlsafe(48))
    environment = os.environ.copy()
    environment.pop("SESSION_SECRET", None)
    environment["STREMHU_LOAD_DOTENV"] = "false"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-B", "-c", "import app.config"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "session_secret" in result.stdout


def test_real_login_and_session_with_disposable_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    password = secrets.token_urlsafe(32)
    with Session(engine) as db:
        db.add(RoleModel(id="user", name="Fixture"))
        db.flush()
        db.add(UserModel(username="fixture", password_hash=PasswordHasher().hash(password)))
        db.commit()

    def fixture_db():
        with Session(engine) as db:
            yield db

    old_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = fixture_db
    try:
        client = TestClient(app, base_url="https://fixture.invalid")
        assert client.get("/api/me/").json() is None
        response = client.post("/api/auth/login", json={"username": "fixture", "password": password})
        assert response.status_code == 200
        assert client.get("/api/me/").json()["username"] == "fixture"
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/me/").json() is None
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
        engine.dispose()
