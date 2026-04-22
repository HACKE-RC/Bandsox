import hashlib
import hmac
import secrets
import time
import json
import os
import logging
import struct
import base64
from pathlib import Path
from typing import Optional
from fastapi import Request, HTTPException, WebSocket

logger = logging.getLogger("bandsox-auth")

AUTH_CONFIG_FILENAME = "auth.json"
SESSION_COOKIE_NAME = "bandsox_session"
SESSION_MAX_AGE = 86400
LOGIN_RATE_LIMIT_WINDOW = 60
LOGIN_RATE_LIMIT_MAX = 10
API_KEY_PREFIX = "bsx_"

_login_attempts: dict = {}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sign_token(secret: str, expires_at: int) -> str:
    payload = struct.pack(">Q", expires_at)
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    raw = payload + sig
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _verify_token(secret: str, token: str) -> bool:
    try:
        padding = 4 - len(token) % 4
        if padding != 4:
            token += "=" * padding
        raw = base64.urlsafe_b64decode(token)
    except Exception:
        return False
    if len(raw) != 8 + 32:
        return False
    payload = raw[:8]
    sig = raw[8:]
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False
    expires_at = struct.unpack(">Q", payload)[0]
    return time.time() < expires_at


def load_auth_config(storage_dir: Path) -> Optional[dict]:
    path = Path(storage_dir) / AUTH_CONFIG_FILENAME
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def save_auth_config(storage_dir: Path, config: dict):
    path = Path(storage_dir) / AUTH_CONFIG_FILENAME
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)


def _get_session_secret(storage_dir: Path) -> str:
    config = load_auth_config(storage_dir)
    if config is None:
        raise RuntimeError("Auth not configured")
    secret = config.get("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        config["session_secret"] = secret
        save_auth_config(storage_dir, config)
    return secret


def init_auth_config(storage_dir: Path) -> tuple:
    password = secrets.token_hex(16)
    api_key = API_KEY_PREFIX + secrets.token_hex(32)
    key_hash = _hash(api_key)
    key_id = "bsx_k_" + key_hash[:8]

    config = {
        "admin_password_hash": _hash(password),
        "session_secret": secrets.token_hex(32),
        "api_keys": [
            {
                "id": key_id,
                "name": "initial-key",
                "key_hash": key_hash,
                "created_at": time.time(),
                "last_used_at": None,
            }
        ],
    }
    save_auth_config(storage_dir, config)
    return password, api_key, key_id


def verify_password(config: dict, password: str) -> bool:
    return hmac.compare_digest(_hash(password), config["admin_password_hash"])


def set_password(storage_dir: Path, new_password: str):
    config = load_auth_config(storage_dir)
    if config is None:
        config = {"admin_password_hash": "", "api_keys": [], "session_secret": secrets.token_hex(32)}
    config["admin_password_hash"] = _hash(new_password)
    save_auth_config(storage_dir, config)


def create_api_key(storage_dir: Path, name: str) -> tuple:
    config = load_auth_config(storage_dir)
    if config is None:
        raise RuntimeError("Auth not configured")
    api_key = API_KEY_PREFIX + secrets.token_hex(32)
    key_hash = _hash(api_key)
    key_id = "bsx_k_" + key_hash[:8]
    config["api_keys"].append(
        {
            "id": key_id,
            "name": name,
            "key_hash": key_hash,
            "created_at": time.time(),
            "last_used_at": None,
        }
    )
    save_auth_config(storage_dir, config)
    return key_id, api_key


def list_api_keys(storage_dir: Path) -> list:
    config = load_auth_config(storage_dir)
    if config is None:
        return []
    return [
        {
            "id": k["id"],
            "name": k["name"],
            "created_at": k["created_at"],
            "last_used_at": k.get("last_used_at"),
        }
        for k in config["api_keys"]
    ]


def revoke_api_key(storage_dir: Path, key_id: str) -> bool:
    config = load_auth_config(storage_dir)
    if config is None:
        return False
    original_len = len(config["api_keys"])
    config["api_keys"] = [k for k in config["api_keys"] if k["id"] != key_id]
    if len(config["api_keys"]) == original_len:
        return False
    save_auth_config(storage_dir, config)
    return True


def verify_api_key(config: dict, key: str) -> Optional[dict]:
    key_hash = _hash(key)
    for k in config.get("api_keys", []):
        if hmac.compare_digest(key_hash, k["key_hash"]):
            return k
    return None


def create_session(storage_dir: Path) -> str:
    secret = _get_session_secret(storage_dir)
    expires_at = int(time.time()) + SESSION_MAX_AGE
    return _sign_token(secret, expires_at)


def validate_session(token: str, storage_dir: Path) -> bool:
    config = load_auth_config(storage_dir)
    if config is None:
        return False
    secret = config.get("session_secret", "")
    if not secret:
        return False
    return _verify_token(secret, token)


def check_rate_limit(client_ip: str) -> bool:
    now = time.time()
    window_start = now - LOGIN_RATE_LIMIT_WINDOW
    attempts = _login_attempts.get(client_ip, [])
    attempts = [t for t in attempts if t > window_start]
    _login_attempts[client_ip] = attempts
    if len(attempts) >= LOGIN_RATE_LIMIT_MAX:
        return False
    attempts.append(now)
    return True


def auth_enabled(storage_dir: Path) -> bool:
    return load_auth_config(Path(storage_dir)) is not None


def get_auth_dependency(storage_dir):
    storage_path = Path(storage_dir)

    def require_auth(request: Request):
        config = load_auth_config(storage_path)
        if config is None:
            return

        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            key = auth_header[7:]
            if verify_api_key(config, key) is not None:
                return

        session_token = request.cookies.get(SESSION_COOKIE_NAME)
        if session_token and validate_session(session_token, storage_path):
            return

        raise HTTPException(status_code=401, detail="Unauthorized")

    return require_auth


async def authenticate_websocket(websocket: WebSocket, storage_dir: Path) -> bool:
    config = load_auth_config(storage_dir)
    if config is None:
        return True

    token = websocket.query_params.get("token", "")
    if not token:
        return False

    if validate_session(token, storage_dir):
        return True

    if verify_api_key(config, token) is not None:
        return True

    return False
