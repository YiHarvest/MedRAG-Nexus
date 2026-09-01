"""WebUI 密码、不透明 Session 与外层门锁令牌工具。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


class PasswordService:
    """使用明确的 Argon2id 配置计算并验证密码哈希。"""

    def __init__(self) -> None:
        self._hasher = PasswordHasher(type=Type.ID)

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def needs_rehash(self, encoded: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded)
        except InvalidHashError:
            return True


def new_session_token() -> str:
    """生成高熵 Session 令牌，数据库只保存其摘要。"""

    return secrets.token_urlsafe(48)


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


WEBUI_LOCK_COOKIE_NAME = "jd_knowledge_webui_session_v2"
_WEBUI_LOCK_PURPOSE = b"jd-knowledge:webui-session:v1"
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def verify_webui_lock_session(value: str | None, password: str, *, now: float | None = None) -> bool:
    """验证 Next.js 外层门锁签发的 Cookie，不允许直接绕过 WebUI 入口。"""

    if not value or not password:
        return False
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return False
    try:
        expires_at = int(parts[1])
    except ValueError:
        return False
    if str(expires_at) != parts[1] or expires_at <= int(now if now is not None else time.time()):
        return False
    signature_text = parts[3]
    if not _BASE64URL_PATTERN.fullmatch(signature_text):
        return False
    try:
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
    except (ValueError, TypeError):
        return False
    signing_key = hmac.new(password.encode("utf-8"), _WEBUI_LOCK_PURPOSE, hashlib.sha256).digest()
    payload = ".".join(parts[:3]).encode("utf-8")
    expected = hmac.new(signing_key, payload, hashlib.sha256).digest()
    return hmac.compare_digest(signature, expected)
