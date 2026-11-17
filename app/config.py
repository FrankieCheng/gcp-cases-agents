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

"""Configuration and multi-tiered secret resolution for Cloud Case Management Agent."""

from __future__ import annotations

import logging
import os
import re
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("CASE_AGENT_DATA_DIR", str(PROJECT_ROOT / ".data"))).resolve()
DB_PATH = DATA_DIR / "support_cases.db"
UPLOAD_DIR = DATA_DIR / "uploads"
SECRET_FILE_PATH = DATA_DIR / "session_secret.txt"

DEFAULT_ALLOWED_PROJECTS = (
    "gcp-prod-core-01,gcp-analytics-data-02,gcp-ai-sandbox-03"
)

_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9\-]{4,29}$")
_CASE_NAME_RE = re.compile(
    r"^projects/([a-z][a-z0-9\-]{4,29})/cases/([a-zA-Z0-9\-_]+)$"
)

_runtime_mode: str = os.getenv("SUPPORT_API_MODE", "MOCK").upper().strip()
if _runtime_mode not in ("MOCK", "LIVE"):
    _runtime_mode = "MOCK"

_ephemeral_secret: str | None = None


def ensure_data_directories() -> None:
    """Ensure local storage directories outside the web root exist with safe permissions."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
        os.chmod(UPLOAD_DIR, 0o700)
    except OSError:
        pass


def get_session_secret_key() -> str:
    """Multi-tiered secret resolution: Environment -> Local File -> Ephemeral CSPRNG.

    Never uses hardcoded literal secrets or literal fallbacks.
    """
    global _ephemeral_secret
    env_secret = os.getenv("SESSION_SECRET_KEY")
    if env_secret and len(env_secret.strip()) >= 32:
        return env_secret.strip()

    if SECRET_FILE_PATH.is_file():
        file_secret = SECRET_FILE_PATH.read_text(encoding="utf-8").strip()
        if len(file_secret) >= 32:
            return file_secret

    if _ephemeral_secret is None:
        logger.warning(
            "Generating ephemeral session secret via CSPRNG. Instance-isolated! "
            "Configure SESSION_SECRET_KEY via Secret Manager / KMS in production."
        )
        _ephemeral_secret = secrets.token_hex(32)
    return _ephemeral_secret


def get_support_mode() -> str:
    """Return active Support API mode ('MOCK' or 'LIVE')."""
    env_override = os.getenv("SUPPORT_API_MODE", "").upper().strip()
    if env_override in ("MOCK", "LIVE"):
        return env_override
    return _runtime_mode


def set_support_mode(mode: str) -> str:
    """Switch active Support API mode between 'MOCK' and 'LIVE'."""
    global _runtime_mode
    normalized = mode.upper().strip()
    if normalized not in ("MOCK", "LIVE"):
        raise ValueError("Invalid mode. Allowed values: 'MOCK', 'LIVE'.")
    _runtime_mode = normalized
    os.environ["SUPPORT_API_MODE"] = normalized
    return _runtime_mode


def is_force_test_case() -> bool:
    """Return whether LIVE mode forces test_case=True on Support API creation."""
    raw = os.getenv("FORCE_TEST_CASE", "true").strip().lower()
    return raw not in ("false", "0", "no")


def get_default_subscriber_emails() -> str:
    """Return optional comma-separated default subscriber emails from environment."""
    return os.getenv("DEFAULT_SUBSCRIBER_EMAILS", "").strip()


def get_allowed_projects() -> list[str]:
    """Return the list of authorized GCP project IDs."""
    raw = os.getenv("ALLOWED_PROJECTS", DEFAULT_ALLOWED_PROJECTS)
    projects = [p.strip() for p in raw.split(",") if p.strip()]
    return projects


def is_project_allowed(project_id: str) -> bool:
    """Check whether project_id matches expected format and is in ALLOWED_PROJECTS."""
    if not project_id or not _PROJECT_ID_RE.match(project_id.strip()):
        return False
    return project_id.strip() in get_allowed_projects()


def extract_project_id_from_case_name(case_name: str) -> str | None:
    """Extract project_id from resource name 'projects/<project_id>/cases/<case_id>'."""
    if not case_name:
        return None
    match = _CASE_NAME_RE.match(case_name.strip())
    if not match:
        return None
    return match.group(1)


def normalize_case_name(project_id_or_case_name: str, case_id: str | None = None) -> str:
    """Normalize a case resource name and validate its format."""
    if case_id is not None:
        candidate = f"projects/{project_id_or_case_name.strip()}/cases/{case_id.strip()}"
    else:
        candidate = project_id_or_case_name.strip()
    if not _CASE_NAME_RE.match(candidate):
        raise ValueError(
            "Invalid case resource name format. Expected 'projects/<project_id>/cases/<case_id>'."
        )
    return candidate


def validate_project_access(project_id_or_case_name: str) -> str:
    """Validate that a project_id or case_name belongs to ALLOWED_PROJECTS.

    Returns the validated project_id or raises PermissionError.
    """
    value = (project_id_or_case_name or "").strip()
    if value.startswith("projects/"):
        project_id = extract_project_id_from_case_name(value)
        if not project_id:
            raise ValueError(
                "Invalid case resource name. Expected 'projects/<project_id>/cases/<case_id>'."
            )
    else:
        project_id = value

    if not is_project_allowed(project_id):
        allowed = ", ".join(get_allowed_projects())
        raise PermissionError(
            f"Access denied: project '{project_id}' is not in ALLOWED_PROJECTS ({allowed})."
        )
    return project_id
