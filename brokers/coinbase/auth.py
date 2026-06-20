"""Coinbase Advanced Trade CDP JWT (ES256) helpers."""

from __future__ import annotations

import secrets
import time

import jwt
from cryptography.hazmat.primitives import serialization

_API_HOST = "api.coinbase.com"


def normalize_pem_secret(raw: str) -> str:
    pem = (raw or "").strip()
    if "\\n" in pem:
        pem = pem.replace("\\n", "\n")
    return pem


def build_rest_jwt(api_key: str, api_secret_pem: str, method: str, path: str) -> str:
    """Build a short-lived Bearer JWT for Coinbase REST (CDP API keys)."""
    pem = normalize_pem_secret(api_secret_pem)
    private_key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    rel = path if path.startswith("/") else f"/{path}"
    uri = f"{method.upper()} {_API_HOST}{rel}"
    now = int(time.time())
    payload = {
        "sub": api_key,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": uri,
    }
    token = jwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers={"kid": api_key, "nonce": secrets.token_hex()},
    )
    return token if isinstance(token, str) else token.decode("utf-8")
