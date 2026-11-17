# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Security middleware, session management, CSRF protection, rate limiting, and file upload verification."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response, UploadFile
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app import config

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "__Host-session"
SESSION_TTL_SECONDS = 3600 * 4  # 4 hours finite session lifetime
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB limit

ALLOWED_HTTP_METHODS = {"GET", "POST", "HEAD", "OPTIONS"}

CSP_HEADER_VALUE = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

ALLOWED_UPLOAD_EXTENSIONS: dict[str, str] = {
    ".txt": "text/plain",
    ".log": "text/plain",
    ".out": "text/plain",
    ".err": "text/plain",
    ".conf": "text/plain",
    ".ini": "text/plain",
    ".sql": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".xml": "application/xml",
    ".json": "application/json",
    ".har": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".zip": "application/zip",
}

PROHIBITED_SECRET_EXTENSIONS: frozenset[str] = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".jks", ".tfstate", ".kdbx", ".env", ".crt_key"}
)

TEXT_DIAGNOSTIC_EXTENSIONS: frozenset[str] = frozenset(
    {".txt", ".log", ".out", ".err", ".conf", ".ini", ".sql", ".md", ".csv", ".yaml", ".yml", ".xml"}
)

_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_PEM_HEADER_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_BEARER_HEADER_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*['\"]?bearer\s+)[A-Za-z0-9._\-~+/=]{12,}"
)
_GOOGLE_OAUTH_TOKEN_RE = re.compile(r"\bya29\.[A-Za-z0-9._\-]{16,}")
_GCP_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")
_AWS_ACCESS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_CONN_URI_PASSWORD_RE = re.compile(
    r"(?i)\b((?:postgres(?:ql)?|mysql|mariadb|redis(?:s)?|mongodb(?:\+srv)?|amqp(?:s)?)://[^:/@\s]+:)([^@\s'\"`]+)(@)"
)
_INLINE_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(client_secret|db_password|secret_key|x-api-key)(\s*[:=]\s*['\"]?)([^\s'\",;]{8,})"
)


def redact_sensitive_secrets(text: str) -> tuple[str, list[str]]:
    """Detect and mask accidental credentials/keys/passwords in SRE logs, descriptions, or comments.

    Returns `(sanitized_text, list_of_redacted_categories)`.
    """
    if not text:
        return text, []

    out = text
    detected: list[str] = []

    if _PEM_PRIVATE_KEY_RE.search(out):
        out = _PEM_PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", out)
        detected.append("PEM 私钥")
    elif _PEM_HEADER_RE.search(out):
        out = _PEM_HEADER_RE.sub("[REDACTED_PRIVATE_KEY_HEADER]", out)
        detected.append("PEM 私钥头")

    if _BEARER_HEADER_RE.search(out):
        out = _BEARER_HEADER_RE.sub(r"\1[REDACTED_BEARER_TOKEN]", out)
        detected.append("Bearer 认证令牌")

    if _GOOGLE_OAUTH_TOKEN_RE.search(out):
        out = _GOOGLE_OAUTH_TOKEN_RE.sub("[REDACTED_GOOGLE_OAUTH_TOKEN]", out)
        if "Bearer 认证令牌" not in detected:
            detected.append("Google OAuth 令牌")

    if _GCP_API_KEY_RE.search(out):
        out = _GCP_API_KEY_RE.sub("[REDACTED_GCP_API_KEY]", out)
        detected.append("Google API Key")

    if _AWS_ACCESS_KEY_RE.search(out):
        out = _AWS_ACCESS_KEY_RE.sub("[REDACTED_AWS_ACCESS_KEY]", out)
        detected.append("云访问密钥 (AKIA)")

    if _CONN_URI_PASSWORD_RE.search(out):
        out = _CONN_URI_PASSWORD_RE.sub(r"\1[REDACTED_PASSWORD]\3", out)
        detected.append("数据库连接串密码")

    def _mask_assign(match: re.Match[str]) -> str:
        val = match.group(3)
        if val.startswith("[REDACTED"):
            return match.group(0)
        if "明文密钥字段" not in detected:
            detected.append("明文密钥字段")
        return f"{match.group(1)}{match.group(2)}[REDACTED_SECRET]"

    out = _INLINE_SECRET_ASSIGN_RE.sub(_mask_assign, out)
    return out, detected

_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._\-]")


@dataclass
class SessionData:
    """Server-side session state with finite expiration and CSRF synchronizer token."""

    session_id: str
    csrf_token: str
    user_id: str = "gcp-customer-operator"
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + SESSION_TTL_SECONDS)
    invalidated: bool = False


# Server-side session store and rate-limiting buckets
# TODO(security): Integrate OAuth 2.0 / OpenID Connect (Google Identity-Aware Proxy / Workforce Identity)
# and MFA for multi-user enterprise deployments.
_SESSIONS: dict[str, SessionData] = {}
_RATE_LIMIT_BUCKETS: dict[str, list[float]] = {}
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_MAX_REQUESTS = 180


def _sign_value(value: str) -> str:
    secret = config.get_session_secret_key().encode("utf-8")
    sig = hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def _verify_signed_value(signed: str) -> str | None:
    if not signed or "." not in signed:
        return None
    value, sig = signed.rsplit(".", 1)
    secret = config.get_session_secret_key().encode("utf-8")
    expected_sig = hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    return value


def create_session() -> SessionData:
    """Create a cryptographically random server-side session with finite expiration."""
    session_id = secrets.token_urlsafe(32)
    csrf_nonce = secrets.token_urlsafe(24)
    csrf_token = _sign_value(f"{session_id}:{csrf_nonce}")
    sess = SessionData(session_id=session_id, csrf_token=csrf_token)
    _SESSIONS[session_id] = sess
    return sess


def get_session_from_request(request: Request) -> SessionData | None:
    """Resolve and validate an active server-side session from cookie or signed CSRF token."""
    now = time.time()
    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_cookie:
        session_id = _verify_signed_value(raw_cookie)
        if session_id:
            sess = _SESSIONS.get(session_id)
            if sess and not sess.invalidated and sess.expires_at > now:
                return sess
            return None

    # Fallback resolution via signed X-CSRF-Token for HTTP test clients that do not forward __Host- Secure cookies over plain HTTP
    csrf_header = request.headers.get("X-CSRF-Token", "").strip()
    if csrf_header:
        verified = _verify_signed_value(csrf_header)
        if verified and ":" in verified:
            session_id, _ = verified.split(":", 1)
            sess = _SESSIONS.get(session_id)
            if (
                sess
                and not sess.invalidated
                and sess.expires_at > now
                and hmac.compare_digest(sess.csrf_token, csrf_header)
            ):
                return sess
    return None


def get_or_create_session(request: Request, response: Response) -> SessionData:
    """Retrieve existing valid session or provision a new hardened session cookie."""
    sess = get_session_from_request(request)
    if sess is None:
        sess = create_session()
    attach_session_cookie(response, sess)
    return sess


def attach_session_cookie(response: Response, session: SessionData) -> None:
    """Attach hardened __Host-session cookie (HttpOnly, Secure, SameSite=Lax, Path=/, no Domain)."""
    signed_session_id = _sign_value(session.session_id)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=signed_session_id,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def invalidate_session(request: Request, response: Response) -> None:
    """Invalidate the current session server-side and clear the client session cookie."""
    sess = get_session_from_request(request)
    if sess is not None:
        sess.invalidated = True
        _SESSIONS.pop(sess.session_id, None)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def require_csrf(request: Request) -> SessionData:
    """Validate Synchronizer / Double-Submit CSRF token on all state-changing POST requests."""
    csrf_header = request.headers.get("X-CSRF-Token", "").strip()
    if not csrf_header:
        raise HTTPException(
            status_code=403,
            detail="CSRF validation failed: missing X-CSRF-Token header.",
        )

    sess = get_session_from_request(request)
    if sess is None:
        raise HTTPException(
            status_code=403,
            detail="CSRF validation failed: invalid or expired session.",
        )

    if not hmac.compare_digest(sess.csrf_token, csrf_header):
        raise HTTPException(
            status_code=403,
            detail="CSRF validation failed: token mismatch.",
        )
    return sess


def _check_rate_limit(client_key: str) -> bool:
    now = time.time()
    timestamps = _RATE_LIMIT_BUCKETS.get(client_key, [])
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    recent = [t for t in timestamps if t > cutoff]
    if len(recent) >= RATE_LIMIT_MAX_REQUESTS:
        _RATE_LIMIT_BUCKETS[client_key] = recent
        return False
    recent.append(now)
    _RATE_LIMIT_BUCKETS[client_key] = recent
    return True


class SecurityHeadersAndMethodMiddleware(BaseHTTPMiddleware):
    """Enforces HTTP method allow-list, rate limiting, and strict security headers."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.method.upper() not in ALLOWED_HTTP_METHODS:
            return JSONResponse(
                status_code=405,
                content={
                    "error": f"Method '{request.method}' not allowed. Only GET and POST are permitted."
                },
                headers=_build_security_headers(),
            )

        client_host = request.client.host if request.client else "127.0.0.1"
        if not _check_rate_limit(client_host):
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded. Please retry later."},
                headers=_build_security_headers(),
            )

        response: Response = await call_next(request)
        for header_name, header_val in _build_security_headers().items():
            response.headers.setdefault(header_name, header_val)
        return response


def _build_security_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": CSP_HEADER_VALUE,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Cache-Control": "no-store",
    }


def sniff_extension_from_magic(content: bytes) -> str | None:
    """Infer a safe file extension strictly from verified magic bytes when no extension is present."""
    if not content:
        return None
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if content.startswith(b"%PDF-"):
        return ".pdf"
    if content.startswith(b"\x1f\x8b"):
        return ".gz"
    if content.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return ".zip"
    return None


def _reject_embedded_private_keys(decoded_text: str) -> None:
    """Reject uploaded files containing raw private keys or GCP Service Account key JSON."""
    if _PEM_HEADER_RE.search(decoded_text):
        raise HTTPException(
            status_code=400,
            detail="安全拦截：检测到文件中包含 PEM 私钥（PRIVATE KEY），严禁将私钥文件直接上传至外部工单，请脱敏后重试。",
        )


def _validate_file_magic_and_structure(ext: str, content: bytes) -> None:
    """Inspect magic bytes and file structure to verify uploaded file content matches its extension."""
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Reject executable headers regardless of extension
    if content.startswith((b"MZ", b"\x7fELF", b"#!")):
        raise HTTPException(
            status_code=400,
            detail="Executable binaries or scripts are strictly prohibited.",
        )

    if ext == ".png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(
                status_code=400,
                detail="File content magic bytes do not match PNG format.",
            )
    elif ext in (".jpg", ".jpeg"):
        if not content.startswith(b"\xff\xd8\xff"):
            raise HTTPException(
                status_code=400,
                detail="File content magic bytes do not match JPEG format.",
            )
    elif ext == ".webp":
        if not (len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"):
            raise HTTPException(
                status_code=400,
                detail="File content magic bytes do not match WEBP format.",
            )
    elif ext == ".gif":
        if not content.startswith((b"GIF87a", b"GIF89a")):
            raise HTTPException(
                status_code=400,
                detail="File content magic bytes do not match GIF format.",
            )
    elif ext == ".pdf":
        # TODO(security): Integrate sandboxed antivirus scanning and Content Disarm & Reconstruction (CDR)
        # in production environments before persisting complex documents.
        if not content.startswith(b"%PDF-"):
            raise HTTPException(
                status_code=400,
                detail="File content magic bytes do not match PDF format.",
            )
        lowered = content.lower()
        if b"/javascript" in lowered or b"/js" in lowered or b"/launch" in lowered:
            raise HTTPException(
                status_code=400,
                detail="PDF contains prohibited embedded script or launch actions.",
            )
    elif ext in (".gz", ".tgz"):
        if not content.startswith(b"\x1f\x8b"):
            raise HTTPException(
                status_code=400,
                detail="File content magic bytes do not match GZIP format.",
            )
    elif ext == ".zip":
        if not content.startswith((b"PK\x03\x04", b"PK\x05\x06")):
            raise HTTPException(
                status_code=400,
                detail="File content magic bytes do not match ZIP archive format.",
            )
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                infos = zf.infolist()
                if len(infos) > 200:
                    raise HTTPException(
                        status_code=400,
                        detail="ZIP archive contains too many entries (max 200).",
                    )
                total_uncompressed = sum(max(0, info.file_size) for info in infos)
                if total_uncompressed > 50 * 1024 * 1024:
                    raise HTTPException(
                        status_code=400,
                        detail="ZIP archive uncompressed size exceeds safety limit (50MB).",
                    )
                for info in infos:
                    entry_ext = Path(info.filename).suffix.lower()
                    if entry_ext in PROHIBITED_SECRET_EXTENSIONS or entry_ext in {
                        ".exe",
                        ".dll",
                        ".bat",
                        ".cmd",
                        ".sh",
                    }:
                        raise HTTPException(
                            status_code=400,
                            detail=f"ZIP archive contains prohibited file '{info.filename}'.",
                        )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid or corrupted ZIP archive.",
            ) from exc
    elif ext in (".json", ".har"):
        try:
            decoded = content.decode("utf-8")
            parsed = json.loads(decoded)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid JSON/HAR file structure or UTF-8 encoding.",
            ) from exc
        _reject_embedded_private_keys(decoded)
        if isinstance(parsed, dict) and (
            parsed.get("type") == "service_account" and "private_key" in parsed
        ):
            raise HTTPException(
                status_code=400,
                detail="安全拦截：检测到 GCP Service Account 密钥 JSON 文件，严禁上传至外部工单。",
            )
    elif ext in TEXT_DIAGNOSTIC_EXTENSIONS:
        if b"\x00" in content:
            raise HTTPException(
                status_code=400,
                detail="Binary null bytes detected in text/config upload.",
            )
        try:
            decoded_txt = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="Text/config diagnostic file must be valid UTF-8.",
            ) from exc
        _reject_embedded_private_keys(decoded_txt)


def validate_and_store_raw_bytes(
    content: bytes, raw_filename: str | None = None
) -> dict[str, Any]:
    """Validate extension allow-list, size limit, magic bytes, and store with UUID filename outside web root."""
    config.ensure_data_directories()

    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum allowed size of {MAX_UPLOAD_BYTES // (1024 * 1024)}MB.",
        )

    base_name = os.path.basename((raw_filename or "").strip())
    lower_base = base_name.lower()
    if lower_base == ".env" or lower_base.endswith(".env"):
        raise HTTPException(
            status_code=400,
            detail="安全拦截：禁止直接上传 .env 环境变量文件，请脱敏后仅提供必要配置项。",
        )
    sanitized_name = _SAFE_FILENAME_RE.sub("_", base_name).strip("._")
    ext = Path(sanitized_name).suffix.lower() if sanitized_name else ""

    if ext in PROHIBITED_SECRET_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"安全拦截：禁止上传高危密钥或状态文件 ('{ext}')，请脱敏后仅上传必要的排障日志或配置片段。",
        )

    if not ext:
        sniffed_ext = sniff_extension_from_magic(content)
        if sniffed_ext:
            ext = sniffed_ext
            stem = sanitized_name or "feishu_attachment"
            sanitized_name = f"{stem}{ext}"
        elif not sanitized_name:
            sanitized_name = "attachment.txt"
            ext = ".txt"

    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        allowed_list = ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS.keys()))
        raise HTTPException(
            status_code=400,
            detail=f"File extension '{ext}' is not allowed. Allowed extensions: {allowed_list}",
        )

    _validate_file_magic_and_structure(ext, content)

    stored_filename = f"{uuid.uuid4().hex}{ext}"
    sandbox_root = config.UPLOAD_DIR.resolve()
    target_path = (sandbox_root / stored_filename).resolve()

    # Strict directory boundary check with trailing separator to prevent partial-prefix traversal
    if not str(target_path).startswith(str(sandbox_root) + os.sep):
        raise HTTPException(status_code=400, detail="Invalid upload storage path.")

    target_path.write_bytes(content)
    try:
        os.chmod(target_path, 0o600)
    except OSError:
        pass

    return {
        "original_filename": sanitized_name,
        "stored_filename": stored_filename,
        "mime_type": ALLOWED_UPLOAD_EXTENSIONS[ext],
        "size_bytes": len(content),
    }


async def validate_and_store_upload(upload: UploadFile) -> dict[str, Any]:
    """Validate and store a FastAPI UploadFile using unified security checks."""
    content = await upload.read(MAX_UPLOAD_BYTES + 1)
    return validate_and_store_raw_bytes(content, upload.filename or "attachment.txt")


def resolve_stored_upload_path(stored_filename: str) -> Path:
    """Safely resolve a stored upload file within UPLOAD_DIR enforcing strict path boundary checks."""
    clean_name = os.path.basename(stored_filename or "")
    if not clean_name or clean_name != stored_filename:
        raise HTTPException(status_code=400, detail="Invalid attachment file reference.")

    sandbox_root = config.UPLOAD_DIR.resolve()
    resolved = (sandbox_root / clean_name).resolve()
    if not str(resolved).startswith(str(sandbox_root) + os.sep):
        raise HTTPException(status_code=403, detail="Path boundary violation.")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Attachment file not found on disk.")
    return resolved
