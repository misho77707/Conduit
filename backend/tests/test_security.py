import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.security import (
    SecretKeyError, create_token, decode_token, decrypt_secrets, encrypt_secrets, hash_password, verify_password,
)


def test_password_hash_roundtrip():
    h = hash_password("hunter2")
    assert h.startswith("scrypt$") and "hunter2" not in h
    assert verify_password("hunter2", h)
    assert not verify_password("hunter3", h)
    assert not verify_password("x", "garbage")


def test_token_roundtrip_and_tamper(env):
    t = create_token("abc", "admin")
    assert decode_token(t)["sub"] == "abc"
    assert decode_token(t + "x") is None


def test_secrets_encrypted_and_recoverable(env):
    token = encrypt_secrets({"password": "s3cr3t-value"})
    assert "s3cr3t-value" not in token
    assert decrypt_secrets(token) == {"password": "s3cr3t-value"}
    assert decrypt_secrets("") == {}


def test_wrong_master_key_is_a_clear_error(env, monkeypatch):
    token = encrypt_secrets({"password": "x"})
    monkeypatch.setenv("CONDUIT_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    with pytest.raises(SecretKeyError, match="cannot be decrypted"):
        decrypt_secrets(token)


def test_missing_master_key_is_a_clear_error(env, monkeypatch):
    monkeypatch.setenv("CONDUIT_SECRET_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(SecretKeyError, match="not set"):
        encrypt_secrets({"a": "b"})
