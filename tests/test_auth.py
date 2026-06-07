import pytest
import os
from shellwire.auth import ensure_token, read_token, rotate_token, validate_token

def test_auth_token_generation(tmp_path, monkeypatch):
    monkeypatch.setattr("shellwire.auth.TOKEN_DIR", tmp_path)
    monkeypatch.setattr("shellwire.auth.TOKEN_FILE", tmp_path / "auth.token")
    
    token = ensure_token()
    assert token is not None
    assert len(token) == 64
    
    # ensure idempotency
    token2 = ensure_token()
    assert token == token2
    
    # validate
    assert validate_token(token) is True
    assert validate_token("invalid") is False
    
    # rotate
    token3 = rotate_token()
    assert token3 != token
    assert validate_token(token3) is True
    assert validate_token(token) is False
