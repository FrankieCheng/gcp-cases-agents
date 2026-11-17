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

"""Dual-mode (MOCK SQLite & LIVE Google Cloud Support API v2) Support Client."""

from __future__ import annotations

import datetime
import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from app import config

logger = logging.getLogger(__name__)

VALID_SEVERITIES = ("S1", "S2", "S3", "S4")
SEVERITY_TO_PRIORITY = {
    "S1": "P1",
    "S2": "P2",
    "S3": "P3",
    "S4": "P4",
    "P0": "P1",
    "P1": "P1",
    "P2": "P2",
    "P3": "P3",
    "P4": "P4",
}
PRIORITY_TO_SEVERITY = {
    "P0": "S1",
    "P1": "S1",
    "P2": "S2",
    "P3": "S3",
    "P4": "S4",
    "PRIORITY_UNSPECIFIED": "S3",
    "S1": "S1",
    "S2": "S2",
    "S3": "S3",
    "S4": "S4",
}
VALID_ESCALATION_REASONS = (
    "RESOLUTION_TIME",
    "TECHNICAL_EXPERTISE",
    "BUSINESS_IMPACT",
)

DEFAULT_CLASSIFICATIONS: list[dict[str, str]] = [
    {
        "id": "100H41Q3DTMN0TBKCKD0SGRFDLO7AT3541LN4PR995I62SR991762R3581J62SBN",
        "display_name": "Compute Engine > Virtual Machines > Instance Connectivity",
        "keywords": "compute engine vm instance ssh connectivity network gce timeout",
    },
    {
        "id": "100H41Q3DTMN0TBKCKD0SGRFDLO7AT3541LN4PR995I62SR991762R3581J62SQL",
        "display_name": "Cloud SQL > PostgreSQL > Connectivity & High Availability",
        "keywords": "cloud sql postgres postgresql database connection timeout failover ha replica",
    },
    {
        "id": "100H41Q3DTMN0TBKCKD0SGRFDLO7AT3541LN4PR995I62SR991762R3581J62GKE",
        "display_name": "Google Kubernetes Engine > Cluster Autoscaler & Node Pools",
        "keywords": "gke kubernetes cluster autoscaler node pool pod scheduling oom",
    },
    {
        "id": "100H41Q3DTMN0TBKCKD0SGRFDLO7AT3541LN4PR995I62SR991762R3581J62VAI",
        "display_name": "Vertex AI > Gemini API & Online Prediction > Quota & Latency",
        "keywords": "vertex ai gemini llm endpoint prediction 429 quota latency throttling",
    },
    {
        "id": "100H41Q3DTMN0TBKCKD0SGRFDLO7AT3541LN4PR995I62SR991762R3581J62GCS",
        "display_name": "Cloud Storage > Buckets & Objects > IAM & Access Control",
        "keywords": "cloud storage gcs bucket object permission 403 iam signed url",
    },
    {
        "id": "100H41Q3DTMN0TBKCKD0SGRFDLO7AT3541LN4PR995I62SR991762R3581J62BQX",
        "display_name": "BigQuery > Query Execution > Slot Contention & Performance",
        "keywords": "bigquery bq sql query slot contention timeout performance reservation",
    },
    {
        "id": "100H41Q3DTMN0TBKCKD0SGRFDLO7AT3541LN4PR995I62SR991762R3581J62CLB",
        "display_name": "Cloud Networking > Cloud Load Balancing > 502/504 Gateway Errors",
        "keywords": "cloud load balancing lb networking 502 504 backend health check",
    },
]

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+$")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _proto_timestamp_to_iso(ts: Any, fallback: str | None = None) -> str:
    """Normalize a protobuf / DatetimeWithNanoseconds / ISO timestamp to UTC ISO-8601."""
    if ts is None:
        return fallback or _utc_now_iso()
    if isinstance(ts, datetime.datetime):
        dt = ts.replace(tzinfo=datetime.UTC) if ts.tzinfo is None else ts.astimezone(datetime.UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if hasattr(ts, "ToDatetime"):
        dt = ts.ToDatetime().replace(tzinfo=datetime.UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(ts).strip()
    return text or (fallback or _utc_now_iso())


def _enum_name(val: Any, default: str) -> str:
    """Extract clean enum name (e.g. 'IN_PROGRESS_GOOGLE_SUPPORT', 'P2') from proto enum or int/str."""
    if val is None:
        return default
    name = getattr(val, "name", None)
    if isinstance(name, str) and name:
        return name
    text = str(val).strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text or default


# -----------------------------------------------------------------------------
# LIVE classification catalog (real IDs from cloudsupport.googleapis.com)
# -----------------------------------------------------------------------------
# The API query language only supports field filters (e.g. display_name:"*SQL*")
# and rejects free text / Chinese, so we load the full catalog once (~3k items),
# cache it, and rank locally.
_LIVE_CATALOG_TTL_SECONDS = 6 * 3600
_live_catalog_lock = threading.Lock()
_live_catalog: list[dict[str, str]] = []
_live_catalog_loaded_at = 0.0

# Chinese terms -> English words that appear in classification display names.
_ZH_ALIASES: dict[str, str] = {
    "虚拟机": "compute engine vm",
    "云主机": "compute engine vm",
    "云服务器": "compute engine vm",
    "实例": "instance vm",
    "数据库": "sql database",
    "存储桶": "cloud storage bucket",
    "对象存储": "cloud storage",
    "存储": "storage",
    "负载均衡": "load balancer",
    "网络": "network vpc",
    "防火墙": "firewall",
    "集群": "kubernetes cluster",
    "容器": "kubernetes container",
    "节点": "node",
    "扩缩容": "autoscaler autoscaling",
    "自动扩容": "autoscaler autoscaling",
    "查询": "query",
    "慢": "performance",
    "大模型": "vertex ai gemini",
    "模型": "vertex ai model",
    "配额": "quota",
    "计费": "billing",
    "账单": "billing",
    "权限": "iam permission",
    "连接": "connect connectivity",
    "超时": "timeout",
    "延迟": "latency",
    "性能": "performance",
    "备份": "backup",
    "恢复": "restore",
    "升级": "upgrade",
    "日志": "logging",
    "监控": "monitoring",
    "域名": "dns",
    "证书": "certificate ssl",
}
_EN_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "not", "can", "cannot", "our", "my", "is", "are",
     "to", "of", "in", "on", "a", "an", "issue", "problem", "error", "google", "cloud", "gcp"}
)
# English acronyms -> words used in classification product names.
_EN_ALIASES: dict[str, str] = {
    "gke": "kubernetes engine",
    "k8s": "kubernetes engine",
    "gce": "compute engine",
    "vm": "compute engine vm",
    "gcs": "cloud storage",
    "bq": "bigquery",
    "lb": "load balancer",
    "sql": "cloud sql",
    "gemini": "gemini generative",
    "vertex": "agent platform models",
}
# Niche product families that should only win when explicitly mentioned.
_NICHE_MARKERS = ("distributed cloud", "air-gapped", "sandbox")


def _classification_product(name: str) -> str:
    """Return the product segment, skipping the 'Other Google Cloud Products' wrapper."""
    parts = [p.strip() for p in name.split(">")]
    if len(parts) > 1 and parts[0] == "other google cloud products":
        return parts[1]
    return parts[0]


_live_oauth_lock = threading.Lock()
_live_base_creds: Any = None


def _invalidate_live_oauth_token() -> None:
    """Clear cached OAuth2 credentials so the next LIVE call fetches a fresh token."""
    global _live_base_creds
    with _live_oauth_lock:
        _live_base_creds = None


def _get_live_oauth_token() -> str:
    """Obtain and refresh an OAuth2 Bearer access token (ADC on Cloud Run, gcloud/ADC locally)."""
    global _live_base_creds
    import os
    import shutil
    import subprocess

    import google.auth
    import google.auth.exceptions
    import google.auth.transport.requests
    import google.oauth2.credentials

    with _live_oauth_lock:
        if _live_base_creds is not None and _live_base_creds.valid and _live_base_creds.token:
            return str(_live_base_creds.token)

        is_cloud_run = bool(os.getenv("K_SERVICE"))
        has_explicit_adc = bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))

        if not is_cloud_run and not has_explicit_adc:
            gcloud_bin = shutil.which("gcloud")
            if gcloud_bin:
                proc = subprocess.run(
                    [gcloud_bin, "auth", "print-access-token"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                token = (proc.stdout or "").strip()
                if proc.returncode == 0 and token:
                    expiry = datetime.datetime.now(datetime.UTC).replace(
                        tzinfo=None
                    ) + datetime.timedelta(minutes=45)
                    _live_base_creds = google.oauth2.credentials.Credentials(
                        token=token, expiry=expiry
                    )
                    return token

        try:
            if _live_base_creds is None:
                _live_base_creds, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
            if not _live_base_creds.valid or not _live_base_creds.token:
                _live_base_creds.refresh(google.auth.transport.requests.Request())
            if _live_base_creds.token:
                return str(_live_base_creds.token)
        except google.auth.exceptions.GoogleAuthError as exc:
            _live_base_creds = None
            raise RuntimeError("Failed to obtain Google Cloud OAuth2 access token.") from exc

        raise RuntimeError("Failed to obtain Google Cloud OAuth2 access token.")


def _make_live_client(client_cls: Any) -> Any:
    """Instantiate a Cloud Support GAPIC client with OAuth2 Bearer credentials.

    GAPIC defaults to self-signed JWTs (`_use_jwt_access=True`) for Service Account keys,
    which `cloudsupport.googleapis.com` intermittently rejects with INVALID_AUTHENTICATOR.
    """
    if not isinstance(client_cls, type):
        return client_cls()
    import google.oauth2.credentials

    token = _get_live_oauth_token()
    quota_project = getattr(_live_base_creds, "quota_project_id", None)
    return client_cls(
        credentials=google.oauth2.credentials.Credentials(
            token=token, quota_project_id=quota_project
        )
    )


def _get_live_classification_catalog() -> list[dict[str, str]]:
    """Return the cached LIVE classification catalog, refreshing it when stale."""
    global _live_catalog, _live_catalog_loaded_at
    with _live_catalog_lock:
        if _live_catalog and time.monotonic() - _live_catalog_loaded_at < _LIVE_CATALOG_TTL_SECONDS:
            return _live_catalog
        from google.cloud import support_v2

        client = _make_live_client(support_v2.CaseServiceClient)
        request = support_v2.SearchCaseClassificationsRequest(query="", page_size=1000)
        items = [
            {"id": item.id, "display_name": item.display_name}
            for item in client.search_case_classifications(request=request)
            if item.id and item.display_name
        ]
        if not items:
            raise RuntimeError("LIVE classification catalog is empty.")
        _live_catalog = items
        _live_catalog_loaded_at = time.monotonic()
        logger.info("Loaded %d LIVE case classifications.", len(items))
        return _live_catalog


def _rank_live_classifications(
    catalog: list[dict[str, str]], query: str, limit: int = 10
) -> list[dict[str, str]]:
    """Rank real classifications against free text (English and common Chinese terms)."""
    text = (query or "").lower()
    tokens = {
        t for t in re.findall(r"[a-z0-9][a-z0-9\-\.]+", text)
        if len(t) >= 2 and t not in _EN_STOPWORDS
    }
    phrases: set[str] = set()
    expansions = [en for zh, en in _ZH_ALIASES.items() if zh in text]
    expansions += [en for abbr, en in _EN_ALIASES.items() if abbr in tokens]
    for en in expansions:
        tokens.update(en.split())
        if " " in en:
            phrases.add(en)
    # English multi-word product names typed by the user, e.g. "cloud sql".
    words = re.findall(r"[a-z0-9]+", text)
    phrases.update(f"{a} {b}" for a, b in zip(words, words[1:]))
    if not tokens:
        return []
    wants_niche = any(marker in text for marker in _NICHE_MARKERS)

    scored: list[tuple[int, int, dict[str, str]]] = []
    for item in catalog:
        name = item["display_name"].lower()
        product = _classification_product(name)
        score = 0
        for tok in tokens:
            if re.search(rf"\b{re.escape(tok)}", name):
                score += 3 if re.search(rf"\b{re.escape(tok)}", product) else 1
        for phrase in phrases:
            if phrase in product:
                score += 6
        if score and not wants_niche and any(m in name for m in _NICHE_MARKERS):
            score -= 4
        if score:
            # Prefer higher scores, then more general (shorter) entries.
            scored.append((score, -len(name), item))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [{"id": s[2]["id"], "display_name": s[2]["display_name"]} for s in scored[:limit]]


def mask_email(email: str) -> str:
    """Mask an email address for safe UI display (e.g., 'alice@example.com' -> 'a***@example.com')."""
    cleaned = (email or "").strip()
    if "@" not in cleaned:
        return "***"
    local, domain = cleaned.split("@", 1)
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def parse_subscriber_emails(raw: str | list[str] | None) -> list[str]:
    """Validate and normalize subscriber email addresses."""
    if not raw:
        return []
    if isinstance(raw, str):
        items = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        items = [str(item).strip() for item in raw if str(item).strip()]
    validated: list[str] = []
    for email in items[:10]:
        if _EMAIL_RE.match(email) and len(email) <= 128:
            validated.append(email)
    return validated


def evaluate_case_readiness(
    display_name: str,
    description: str,
    severity: str,
    classification_display_name: str = "",
    subscriber_emails: list[str] | None = None,
) -> list[str]:
    """Return non-blocking SRE diagnostic suggestions to prevent Round-1 TSE info-bounce."""
    tips: list[str] = []
    combined = f"{display_name or ''}\n{description or ''}"
    lower_all = f"{combined}\n{classification_display_name or ''}".lower()

    if not re.search(
        r"(\d{1,2}:\d{2}|20\d{2}[-/]\d{1,2}|\butc\b|\bgmt\b|凌晨|早上|上午|下午|昨晚|刚才|\d+\s*(?:分钟|小时|min|hour))",
        combined,
        re.IGNORECASE,
    ):
        tips.append(
            "建议补充精确故障时间窗（含时区，如 14:20 UTC 或北京时间），便于 Google 工程师直接拉取对应时段后台日志。"
        )

    if not re.search(
        r"\b(us-[a-z]+\d|asia-[a-z]+\d|europe-[a-z]+\d|australia-[a-z]+\d|southamerica-[a-z]+\d|northamerica-[a-z]+\d|me-[a-z]+\d|africa-[a-z]+\d|global|multi-region)\b",
        combined,
        re.IGNORECASE,
    ):
        tips.append(
            "建议补充受影响的 GCP Region / Zone（如 asia-east1、us-central1-a 或 global），避免首轮回复反复确认区域。"
        )

    if any(k in lower_all for k in ("kubernetes", "gke")):
        if not re.search(r"(cluster|集群|node\s*pool|节点池|kubectl|pod)", combined, re.IGNORECASE):
            tips.append(
                "GKE 排障建议附带：集群名、节点池/Pod 名称、GKE 版本号，或直接上传 yaml / kubectl 日志附件。"
            )
    elif any(k in lower_all for k in ("cloud sql", "postgresql", "mysql")):
        if not re.search(r"(instance|实例|proxy|psc|private\s*ip|[a-z0-9]+-[a-z0-9]+)", combined, re.IGNORECASE):
            tips.append(
                "Cloud SQL 排障建议附带：实例 ID、连接方式（Auth Proxy / PSC / Private IP）及数据库引擎版本。"
            )
    elif "bigquery" in lower_all:
        if not re.search(r"(job[_\s-]*id|bquxjob|reservation|slot)", combined, re.IGNORECASE):
            tips.append(
                "BigQuery 排障建议附带：报错或慢查询的 Job ID（project:location.job_id）或 Reservation 名称。"
            )
    elif "cloud run" in lower_all:
        if not re.search(r"(revision|trace|x-cloud-trace-context)", combined, re.IGNORECASE):
            tips.append(
                "Cloud Run 排障建议附带：具体 Revision 名称及失败请求的 X-Cloud-Trace-Context 头。"
            )
    elif any(k in lower_all for k in ("load balanc", "vpc", "network", "armor", "cdn", "iap")):
        if not re.search(r"(vip|\d{1,3}(?:\.\d{1,3}){3}|\.har\b|backend|url\s*map)", combined, re.IGNORECASE):
            tips.append(
                "网络/LB 排障建议附带：前端 VIP/域名、Backend Service 名，或直接上传浏览器导出的 .har 抓包文件。"
            )
    elif any(k in lower_all for k in ("vertex", "gemini", "ai platform")):
        if not re.search(r"(gemini-|model|模型|request[_\s-]*id|dsq|rpm|tpm)", combined, re.IGNORECASE):
            tips.append(
                "Vertex AI 排障建议附带：调用的模型 ID、Location（如 global）及报错 Request ID。"
            )

    if (severity or "").upper() == "S1":
        if not subscriber_emails:
            tips.append(
                "⚠️ S1 (P1) 紧急告警：当前未配置 7×24 值班订阅邮箱；Google 全球接力（Follow-the-Sun）若 1 小时内无法联系客户可能会降级为 P2，建议立即添加值班组邮箱。"
            )
        ascii_alpha_count = len(re.findall(r"[A-Za-z]", description or ""))
        if ascii_alpha_count < 25:
            tips.append(
                "🌐 S1 全球接力提示：夜间 P1 工单将自动流转至欧美时区英文团队接力，建议补充简短英文故障摘要（可直接让我帮您改写为中英双语描述）。"
            )

    return tips[:4]


class CloudSupportClient:
    """Dual-mode adapter for Google Cloud Support API v2 (MOCK SQLite & LIVE SDK)."""

    def __init__(self, db_path: Path | None = None) -> None:
        config.ensure_data_directories()
        self.db_path = db_path or config.DB_PATH
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        # TODO(security): In multi-tenant production deployments, migrate from local SQLite
        # to Cloud SQL / Spanner with mTLS and least-privilege IAM database authentication.
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initialize SQLite schema using parameterized statements and seed demo cases."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS classifications (
                        id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        keywords TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cases (
                        case_name TEXT PRIMARY KEY,
                        case_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        description TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        priority TEXT NOT NULL,
                        state TEXT NOT NULL,
                        classification_id TEXT NOT NULL,
                        classification_display_name TEXT NOT NULL,
                        time_zone TEXT NOT NULL,
                        subscriber_emails_json TEXT NOT NULL,
                        escalated INTEGER NOT NULL DEFAULT 0,
                        escalation_reason TEXT DEFAULT '',
                        escalation_justification TEXT DEFAULT '',
                        test_case INTEGER NOT NULL DEFAULT 1,
                        creator_name TEXT NOT NULL DEFAULT 'GCP Customer Portal',
                        create_time TEXT NOT NULL,
                        update_time TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS comments (
                        comment_id TEXT PRIMARY KEY,
                        case_name TEXT NOT NULL,
                        body TEXT NOT NULL,
                        author TEXT NOT NULL,
                        author_type TEXT NOT NULL DEFAULT 'CUSTOMER',
                        create_time TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS attachments (
                        attachment_id TEXT PRIMARY KEY,
                        case_name TEXT NOT NULL,
                        original_filename TEXT NOT NULL,
                        stored_filename TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        create_time TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS drafts (
                        draft_id TEXT PRIMARY KEY,
                        draft_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        case_name TEXT DEFAULT '',
                        payload_json TEXT NOT NULL,
                        result_case_name TEXT DEFAULT '',
                        create_time TEXT NOT NULL,
                        update_time TEXT NOT NULL
                    )
                    """
                )
                # Migration: nullable `owner` and `chat_id` columns record the IM requester
                # and origination chat for a draft. Safe on existing databases.
                draft_cols = {
                    r["name"] for r in conn.execute("PRAGMA table_info(drafts)").fetchall()
                }
                if "owner" not in draft_cols:
                    conn.execute("ALTER TABLE drafts ADD COLUMN owner TEXT")
                if "chat_id" not in draft_cols:
                    conn.execute("ALTER TABLE drafts ADD COLUMN chat_id TEXT DEFAULT ''")

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS case_subscriptions (
                        case_name TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        owner_open_id TEXT DEFAULT '',
                        active INTEGER NOT NULL DEFAULT 1,
                        create_time TEXT NOT NULL,
                        update_time TEXT NOT NULL,
                        PRIMARY KEY (case_name, chat_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS case_watch_state (
                        case_name TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        last_state TEXT NOT NULL,
                        last_priority TEXT NOT NULL,
                        last_escalated INTEGER NOT NULL DEFAULT 0,
                        last_update_time TEXT NOT NULL,
                        seen_comment_ids_json TEXT NOT NULL DEFAULT '[]',
                        seen_attachment_ids_json TEXT NOT NULL DEFAULT '[]',
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                watch_cols = {
                    r["name"]
                    for r in conn.execute("PRAGMA table_info(case_watch_state)").fetchall()
                }
                if "seen_attachment_ids_json" not in watch_cols:
                    conn.execute(
                        "ALTER TABLE case_watch_state ADD COLUMN seen_attachment_ids_json TEXT NOT NULL DEFAULT '[]'"
                    )

                # Seed classifications if empty
                cur = conn.execute("SELECT COUNT(*) AS cnt FROM classifications")
                if cur.fetchone()["cnt"] == 0:
                    for item in DEFAULT_CLASSIFICATIONS:
                        conn.execute(
                            "INSERT INTO classifications (id, display_name, keywords) VALUES (?, ?, ?)",
                            (item["id"], item["display_name"], item["keywords"]),
                        )

                # Seed realistic GCP support cases if empty
                cur_cases = conn.execute("SELECT COUNT(*) AS cnt FROM cases")
                if cur_cases.fetchone()["cnt"] == 0:
                    self._seed_initial_cases(conn)
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to initialize local support database: %s", type(exc).__name__)
            raise RuntimeError("Internal storage initialization error.") from exc

    def _seed_initial_cases(self, conn: sqlite3.Connection) -> None:
        seed_cases = [
            {
                "case_id": "84920101",
                "project_id": "gcp-prod-core-01",
                "display_name": "[PROD] Cloud SQL PostgreSQL 主备切换后应用连接池间歇性超时",
                "description": (
                    "生产环境 Cloud SQL for PostgreSQL 15 实例 (prod-pg-primary) 在今日凌晨 02:15 UTC "
                    "触发自动 HA 故障转移后，GKE 集群通过 Cloud SQL Auth Proxy 连接出现约 8% 的 "
                    "connection timeout 错误。已确认 VPC 防火墙与 Private Service Connect 状态正常。"
                ),
                "severity": "S2",
                "priority": "P2",
                "state": "IN_PROGRESS_GOOGLE_SUPPORT",
                "classification_id": DEFAULT_CLASSIFICATIONS[1]["id"],
                "classification_display_name": DEFAULT_CLASSIFICATIONS[1]["display_name"],
                "time_zone": "Asia/Shanghai",
                "subscriber_emails": ["sre-oncall@example.com", "dba-team@example.com"],
                "escalated": 0,
                "test_case": 1,
                "create_time": "2026-10-06T03:20:00Z",
                "update_time": "2026-10-06T09:45:00Z",
                "comments": [
                    {
                        "comment_id": "cmt-84920101-1",
                        "body": "已上传 pg_stat_activity 快照与 GKE Cloud SQL Auth Proxy 容器日志，请协助排查底层网络路径。",
                        "author": "sre-oncall@example.com",
                        "author_type": "CUSTOMER",
                        "create_time": "2026-10-06T03:25:00Z",
                    },
                    {
                        "comment_id": "cmt-84920101-2",
                        "body": (
                            "您好，我是 Google Cloud Support 数据库工程师。我们已检查实例 prod-pg-primary 的 HA 切换时间线，"
                            "发现备用区域节点在切换后存在约 90 秒的 ARP/PSC 路由收敛延迟。建议将连接池 tcp_keepalives_idle 调低至 30s。"
                        ),
                        "author": "Google Cloud Support (Database TSE)",
                        "author_type": "GOOGLE_SUPPORT",
                        "create_time": "2026-10-06T09:45:00Z",
                    },
                ],
            },
            {
                "case_id": "84920102",
                "project_id": "gcp-prod-core-01",
                "display_name": "GKE Cluster Autoscaler 在突发流量下扩容 c3-standard-8 节点池卡顿",
                "description": (
                    "我们在 us-central1-a 区域的 GKE 生产集群配置了 Cluster Autoscaler，当 Pod 处于 Pending 状态时，"
                    "节点池扩容提示 GCE_STOCKOUT 或等待超过 10 分钟未就绪。"
                ),
                "severity": "S3",
                "priority": "P3",
                "state": "OPEN",
                "classification_id": DEFAULT_CLASSIFICATIONS[2]["id"],
                "classification_display_name": DEFAULT_CLASSIFICATIONS[2]["display_name"],
                "time_zone": "Asia/Shanghai",
                "subscriber_emails": ["platform-eng@example.com"],
                "escalated": 0,
                "test_case": 1,
                "create_time": "2026-10-06T14:10:00Z",
                "update_time": "2026-10-06T14:10:00Z",
                "comments": [
                    {
                        "comment_id": "cmt-84920102-1",
                        "body": "工单已创建，等待分配 GKE 专项支持工程师。您可以随时追加集群事件日志或上传诊断附件。",
                        "author": "Google Cloud Support System",
                        "author_type": "GOOGLE_SUPPORT",
                        "create_time": "2026-10-06T14:10:05Z",
                    }
                ],
            },
            {
                "case_id": "84920103",
                "project_id": "gcp-analytics-data-02",
                "display_name": "BigQuery 每日凌晨批处理作业出现 Slot Contention 导致 SLA 延迟",
                "description": (
                    "项目 gcp-analytics-data-02 下的 Enterprise Edition Reservation (400 baseline slots) "
                    "在 01:00-02:30 UTC 期间出现大量查询排队。"
                ),
                "severity": "S3",
                "priority": "P3",
                "state": "CLOSED",
                "classification_id": DEFAULT_CLASSIFICATIONS[5]["id"],
                "classification_display_name": DEFAULT_CLASSIFICATIONS[5]["display_name"],
                "time_zone": "UTC",
                "subscriber_emails": ["data-eng@example.com"],
                "escalated": 0,
                "test_case": 1,
                "create_time": "2026-10-03T08:00:00Z",
                "update_time": "2026-10-04T16:30:00Z",
                "comments": [
                    {
                        "comment_id": "cmt-84920103-1",
                        "body": "通过 INFORMATION_SCHEMA.JOBS_BY_PROJECT 分析定位到 3 个全表笛卡尔积查询，优化分区裁剪后已恢复正常。",
                        "author": "Google Cloud Support (Data Analytics)",
                        "author_type": "GOOGLE_SUPPORT",
                        "create_time": "2026-10-04T16:20:00Z",
                    }
                ],
            },
            {
                "case_id": "84920104",
                "project_id": "gcp-ai-sandbox-03",
                "display_name": "Vertex AI Gemini API 在线推理偶发 429 Resource Exhausted",
                "description": (
                    "在 us-central1 调用 gemini-2.5-pro 时，尽管 RPM 远低于控制台显示的配额上限，仍偶发返回 429 错误。"
                ),
                "severity": "S2",
                "priority": "P2",
                "state": "OPEN",
                "classification_id": DEFAULT_CLASSIFICATIONS[3]["id"],
                "classification_display_name": DEFAULT_CLASSIFICATIONS[3]["display_name"],
                "time_zone": "Asia/Shanghai",
                "subscriber_emails": ["ai-team@example.com"],
                "escalated": 1,
                "escalation_reason": "BUSINESS_IMPACT",
                "escalation_justification": "AI 客服网关即将上线生产压测，需紧急确认 DSQ 与 Provisioned Throughput 切换方案。",
                "test_case": 1,
                "create_time": "2026-10-06T18:00:00Z",
                "update_time": "2026-10-06T20:15:00Z",
                "comments": [
                    {
                        "comment_id": "cmt-84920104-1",
                        "body": "已申请工单升级（BUSINESS_IMPACT），请求 Vertex AI 产品工程团队协助分析突发 Token 桶限流。",
                        "author": "ai-team@example.com",
                        "author_type": "CUSTOMER",
                        "create_time": "2026-10-06T20:15:00Z",
                    }
                ],
            },
        ]

        for sc in seed_cases:
            case_name = f"projects/{sc['project_id']}/cases/{sc['case_id']}"
            conn.execute(
                """
                INSERT INTO cases (
                    case_name, case_id, project_id, display_name, description,
                    severity, priority, state, classification_id, classification_display_name,
                    time_zone, subscriber_emails_json, escalated, escalation_reason,
                    escalation_justification, test_case, creator_name, create_time, update_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_name,
                    sc["case_id"],
                    sc["project_id"],
                    sc["display_name"],
                    sc["description"],
                    sc["severity"],
                    sc["priority"],
                    sc["state"],
                    sc["classification_id"],
                    sc["classification_display_name"],
                    sc["time_zone"],
                    json.dumps(sc["subscriber_emails"]),
                    sc["escalated"],
                    sc.get("escalation_reason", ""),
                    sc.get("escalation_justification", ""),
                    sc["test_case"],
                    "GCP Customer Portal",
                    sc["create_time"],
                    sc["update_time"],
                ),
            )
            for cmt in sc["comments"]:
                conn.execute(
                    """
                    INSERT INTO comments (comment_id, case_name, body, author, author_type, create_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cmt["comment_id"],
                        case_name,
                        cmt["body"],
                        cmt["author"],
                        cmt["author_type"],
                        cmt["create_time"],
                    ),
                )

    def _row_to_case_dict(
        self,
        row: sqlite3.Row,
        comments: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        raw_emails: list[str] = json.loads(row["subscriber_emails_json"] or "[]")
        masked_emails = [mask_email(e) for e in raw_emails]
        data: dict[str, Any] = {
            "name": row["case_name"],
            "case_id": row["case_id"],
            "project_id": row["project_id"],
            "display_name": row["display_name"],
            "description": row["description"],
            "severity": row["severity"],
            "priority": row["priority"],
            "state": row["state"],
            "classification": {
                "id": row["classification_id"],
                "display_name": row["classification_display_name"],
            },
            "time_zone": row["time_zone"],
            "subscriber_emails": masked_emails,
            "subscriber_count": len(raw_emails),
            "escalated": bool(row["escalated"]),
            "escalation_reason": row["escalation_reason"] or "",
            "escalation_justification": row["escalation_justification"] or "",
            "test_case": bool(row["test_case"]),
            "creator_name": row["creator_name"],
            "create_time": row["create_time"],
            "update_time": row["update_time"],
            "mode": config.get_support_mode(),
        }
        if comments is not None:
            data["comments"] = comments
        if attachments is not None:
            data["attachments"] = attachments
        return data

    # -------------------------------------------------------------------------
    # 1. Classification Search
    # -------------------------------------------------------------------------
    def search_classifications(self, query: str) -> list[dict[str, str]]:
        """Search valid Google Cloud Support case classifications by product/issue keywords."""
        cleaned_query = (query or "").strip()
        if config.get_support_mode() == "LIVE":
            # LIVE must only ever return real classification IDs; mock IDs are
            # rejected by cases.create with 400 "Invalid classification ID".
            return _rank_live_classifications(_get_live_classification_catalog(), cleaned_query)

        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT id, display_name, keywords FROM classifications"
                ).fetchall()

            if not cleaned_query:
                return [{"id": r["id"], "display_name": r["display_name"]} for r in rows]

            tokens = [t.lower() for t in re.split(r"\s+", cleaned_query) if t]
            scored: list[tuple[int, dict[str, str]]] = []
            for r in rows:
                haystack = f"{r['display_name']} {r['keywords']}".lower()
                score = sum(2 if tok in r["display_name"].lower() else 1 for tok in tokens if tok in haystack)
                if cleaned_query.lower() in haystack:
                    score += 3
                if score > 0:
                    scored.append((score, {"id": r["id"], "display_name": r["display_name"]}))

            scored.sort(key=lambda item: item[0], reverse=True)
            if scored:
                return [item[1] for item in scored]
            return [{"id": r["id"], "display_name": r["display_name"]} for r in rows[:4]]
        except sqlite3.Error as exc:
            logger.error("Database error in search_classifications: %s", type(exc).__name__)
            raise RuntimeError("Failed to query classifications.") from exc

    def _resolve_live_classification(
        self, classification_id: str, classification_display_name: str, text: str
    ) -> tuple[str, str]:
        """Return a classification that exists in the LIVE catalog.

        Keeps the given ID if it is real; otherwise re-matches from the case text.
        """
        catalog = _get_live_classification_catalog()
        by_id = {item["id"]: item["display_name"] for item in catalog}
        clean_id = (classification_id or "").strip()
        if clean_id and clean_id in by_id:
            return clean_id, by_id[clean_id]
        query = " ".join(p for p in (classification_display_name, text) if p)
        matches = _rank_live_classifications(catalog, query)
        if not matches:
            raise ValueError(
                "无法自动匹配有效的 Google Cloud Support 分类，请说明具体的 GCP 产品名称"
                "（例如 Compute Engine、Cloud SQL、GKE、BigQuery、Vertex AI）。"
            )
        return matches[0]["id"], matches[0]["display_name"]

    # -------------------------------------------------------------------------
    # 2. HITL Draft Management (Case Creation & Escalation)
    # -------------------------------------------------------------------------
    def find_similar_open_cases(
        self,
        project_id: str,
        query_text: str,
        classification_id: str = "",
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Detect existing non-CLOSED cases in the same project with overlapping product/symptom tokens."""
        validated_project = config.validate_project_access(project_id)
        lower_q = (query_text or "").lower()
        en_tokens = {
            t
            for t in re.findall(r"[a-z0-9][a-z0-9\-\.]{1,}", lower_q)
            if len(t) >= 3 and t not in _EN_STOPWORDS
        }
        zh_terms = {
            zh
            for zh in (
                "连接",
                "超时",
                "扩容",
                "限流",
                "配额",
                "宕机",
                "慢查询",
                "切换",
                "权限",
                "网络",
                "负载",
                "集群",
                "数据库",
                "存储",
            )
            if zh in lower_q
        }
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM cases WHERE project_id = ? AND state != 'CLOSED' ORDER BY update_time DESC LIMIT 50",
                    (validated_project,),
                ).fetchall()
        except sqlite3.Error:
            return []

        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            hay = f"{row['display_name']} {row['description']} {row['classification_display_name']}".lower()
            score = 0
            if classification_id and row["classification_id"] == classification_id:
                score += 4
            for tok in en_tokens:
                if tok in hay:
                    score += 2
            for zh in zh_terms:
                if zh in hay:
                    score += 2
            if score >= 4:
                scored.append(
                    (
                        score,
                        {
                            "case_name": row["case_name"],
                            "case_id": row["case_id"],
                            "display_name": row["display_name"],
                            "severity": row["severity"],
                            "state": row["state"],
                        },
                    )
                )
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def create_case_draft(
        self,
        project_id: str,
        display_name: str,
        description: str,
        severity: str = "S3",
        classification_id: str = "",
        classification_display_name: str = "",
        subscriber_emails: str | list[str] | None = None,
        time_zone: str = "Asia/Shanghai",
        followup_of: str = "",
    ) -> dict[str, Any]:
        """Create a pending HITL confirmation draft before calling cases.create."""
        validated_project = config.validate_project_access(project_id)
        from app.security import redact_sensitive_secrets

        raw_title = (display_name or "").strip()
        raw_desc = (description or "").strip()
        clean_title, redacted_title = redact_sensitive_secrets(raw_title)
        clean_desc, redacted_desc = redact_sensitive_secrets(raw_desc)
        redacted_types = list(dict.fromkeys([*redacted_title, *redacted_desc]))
        if len(clean_title) < 4 or len(clean_title) > 200:
            raise ValueError("Case display_name must be between 4 and 200 characters.")
        if len(clean_desc) < 10 or len(clean_desc) > 8000:
            raise ValueError("Case description must be between 10 and 8000 characters.")

        norm_sev = PRIORITY_TO_SEVERITY.get((severity or "S3").upper().strip(), "S3")
        if norm_sev not in VALID_SEVERITIES:
            norm_sev = "S3"

        if config.get_support_mode() == "LIVE":
            classification_id, classification_display_name = self._resolve_live_classification(
                classification_id, classification_display_name, f"{clean_title} {clean_desc}"
            )
        elif not classification_id or not classification_display_name:
            matches = self.search_classifications(f"{clean_title} {clean_desc}")
            chosen = matches[0] if matches else DEFAULT_CLASSIFICATIONS[0]
            classification_id = classification_id or chosen["id"]
            classification_display_name = (
                classification_display_name or chosen["display_name"]
            )

        emails = parse_subscriber_emails(subscriber_emails)
        masked_emails = [mask_email(e) for e in emails]
        similar_cases = self.find_similar_open_cases(
            validated_project,
            f"{clean_title} {clean_desc}",
            classification_id=classification_id.strip(),
        )
        diagnostic_tips = evaluate_case_readiness(
            display_name=clean_title,
            description=clean_desc,
            severity=norm_sev,
            classification_display_name=classification_display_name.strip(),
            subscriber_emails=emails,
        )
        draft_id = f"draft-case-{uuid.uuid4().hex[:10]}"
        now = _utc_now_iso()

        payload: dict[str, Any] = {
            "project_id": validated_project,
            "display_name": clean_title,
            "description": clean_desc,
            "severity": norm_sev,
            "priority": SEVERITY_TO_PRIORITY[norm_sev],
            "classification_id": classification_id.strip(),
            "classification_display_name": classification_display_name.strip(),
            "subscriber_emails": emails,
            "masked_subscriber_emails": masked_emails,
            "time_zone": (time_zone or "Asia/Shanghai").strip()[:64],
            "test_case": True if config.is_force_test_case() else False,
            "mode": config.get_support_mode(),
        }
        if similar_cases:
            payload["similar_open_cases"] = similar_cases
        if diagnostic_tips:
            payload["diagnostic_tips"] = diagnostic_tips
        if redacted_types:
            payload["redacted_secrets"] = redacted_types
        if followup_of:
            payload["followup_of"] = followup_of

        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO drafts (
                        draft_id, draft_type, status, project_id, case_name,
                        payload_json, result_case_name, create_time, update_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft_id,
                        "CREATE_CASE",
                        "PENDING_CONFIRMATION",
                        validated_project,
                        "",
                        json.dumps(payload, ensure_ascii=False),
                        "",
                        now,
                        now,
                    ),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to save case draft: %s", type(exc).__name__)
            raise RuntimeError("Failed to persist case draft.") from exc

        return self.get_draft(draft_id)

    def revise_case_draft(
        self,
        draft_id: str,
        display_name: str = "",
        description: str = "",
        severity: str = "",
        subscriber_emails: str | list[str] | None = None,
        time_zone: str = "",
    ) -> dict[str, Any]:
        """Modify fields of an existing PENDING_CONFIRMATION CREATE_CASE draft in place."""
        draft = self.get_draft(draft_id)
        if draft["draft_type"] != "CREATE_CASE":
            raise ValueError(f"Draft '{draft_id}' is not a CREATE_CASE draft.")
        if draft["status"] != "PENDING_CONFIRMATION":
            raise ValueError(
                f"Draft '{draft_id}' is in state '{draft['status']}' and can no longer be revised."
            )
        config.validate_project_access(draft["project_id"])

        payload = dict(draft["payload"])
        from app.security import redact_sensitive_secrets

        newly_redacted: list[str] = list(payload.get("redacted_secrets") or [])
        if display_name and display_name.strip():
            clean_t, red_t = redact_sensitive_secrets(display_name.strip())
            if len(clean_t) < 4 or len(clean_t) > 200:
                raise ValueError("Case display_name must be between 4 and 200 characters.")
            payload["display_name"] = clean_t
            newly_redacted.extend(red_t)
        if description and description.strip():
            clean_d, red_d = redact_sensitive_secrets(description.strip())
            if len(clean_d) < 10 or len(clean_d) > 8000:
                raise ValueError("Case description must be between 10 and 8000 characters.")
            payload["description"] = clean_d
            newly_redacted.extend(red_d)
        if severity and severity.strip():
            norm_sev = PRIORITY_TO_SEVERITY.get(severity.upper().strip(), payload["severity"])
            if norm_sev in VALID_SEVERITIES:
                payload["severity"] = norm_sev
                payload["priority"] = SEVERITY_TO_PRIORITY[norm_sev]
        if subscriber_emails is not None:
            emails = parse_subscriber_emails(subscriber_emails)
            payload["subscriber_emails"] = emails
            payload["masked_subscriber_emails"] = [mask_email(e) for e in emails]
        if time_zone and time_zone.strip():
            payload["time_zone"] = time_zone.strip()[:64]

        if newly_redacted:
            payload["redacted_secrets"] = list(dict.fromkeys(newly_redacted))
        refreshed_tips = evaluate_case_readiness(
            display_name=str(payload.get("display_name") or ""),
            description=str(payload.get("description") or ""),
            severity=str(payload.get("severity") or "S3"),
            classification_display_name=str(payload.get("classification_display_name") or ""),
            subscriber_emails=list(payload.get("subscriber_emails") or []),
        )
        if refreshed_tips:
            payload["diagnostic_tips"] = refreshed_tips
        else:
            payload.pop("diagnostic_tips", None)

        now = _utc_now_iso()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE drafts SET payload_json = ?, update_time = ? WHERE draft_id = ?",
                    (json.dumps(payload, ensure_ascii=False), now, draft["draft_id"]),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to revise case draft: %s", type(exc).__name__)
            raise RuntimeError("Failed to revise case draft.") from exc

        return self.get_draft(draft["draft_id"])

    def create_followup_case_draft(
        self,
        reference_case_name: str,
        followup_description: str,
        severity: str = "",
    ) -> dict[str, Any]:
        """Create a linked follow-up CREATE_CASE draft referencing a closed (or previous) support case."""
        config.validate_project_access(reference_case_name)
        norm_ref = config.normalize_case_name(reference_case_name)
        ref_case = self.get_case(norm_ref)

        raw_emails: list[str] = []
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT subscriber_emails_json FROM cases WHERE case_name = ?",
                    (norm_ref,),
                ).fetchone()
                if row and row["subscriber_emails_json"]:
                    raw_emails = list(json.loads(row["subscriber_emails_json"]))
        except Exception:
            raw_emails = []

        clean_followup = (followup_description or "").strip()
        if len(clean_followup) < 4:
            clean_followup = "原工单问题复发，请求基于历史排查上下文继续跟进。"

        ref_id = ref_case.get("case_id") or norm_ref.split("/")[-1]
        base_title = str(ref_case.get("display_name") or "Follow-up Support Case")
        prefix = f"[Follow-up #{ref_id}] "
        new_title = (
            base_title if base_title.startswith("[Follow-up") else f"{prefix}{base_title}"
        )[:200]
        combined_desc = (
            f"【关联历史工单】: {norm_ref}\n"
            f"【原工单标题】: {base_title}\n\n"
            f"【本次复发/后续跟进说明】:\n{clean_followup}\n\n"
            f"【原工单背景摘要】:\n{str(ref_case.get('description') or '')[:2000]}"
        )[:8000]
        chosen_sev = (severity or ref_case.get("severity") or "S3").strip()
        cls_info = ref_case.get("classification") or {}

        return self.create_case_draft(
            project_id=ref_case["project_id"],
            display_name=new_title,
            description=combined_desc,
            severity=chosen_sev,
            classification_id=str(cls_info.get("id") or ""),
            classification_display_name=str(cls_info.get("display_name") or ""),
            subscriber_emails=raw_emails,
            time_zone=str(ref_case.get("time_zone") or "Asia/Shanghai"),
            followup_of=norm_ref,
        )

    def create_escalation_draft(
        self,
        case_name: str,
        reason: str,
        justification: str,
    ) -> dict[str, Any]:
        """Create a pending HITL confirmation draft before calling cases.escalate."""
        validated_project = config.validate_project_access(case_name)
        normalized_case = config.normalize_case_name(case_name)
        existing_case = self.get_case(normalized_case)
        if existing_case["state"] == "CLOSED":
            raise ValueError("Cannot escalate a CLOSED support case.")
        if existing_case.get("escalated"):
            raise ValueError(
                f"Support case '{normalized_case}' is already escalated (该工单已处于升级状态 Escalated，无需重复提交升级；建议通过评论补充最新业务损失数据，或调用 update_support_case 提升严重级别至 S1)."
            )

        norm_reason = (reason or "BUSINESS_IMPACT").upper().strip()
        if norm_reason not in VALID_ESCALATION_REASONS:
            raise ValueError(
                f"Invalid escalation reason '{reason}'. Must be one of {VALID_ESCALATION_REASONS}."
            )
        clean_just = (justification or "").strip()
        if len(clean_just) < 10 or len(clean_just) > 2000:
            raise ValueError(
                "Escalation justification must be between 10 and 2000 characters."
            )

        draft_id = f"draft-esc-{uuid.uuid4().hex[:10]}"
        now = _utc_now_iso()
        payload = {
            "project_id": validated_project,
            "case_name": normalized_case,
            "case_display_name": existing_case["display_name"],
            "severity": existing_case["severity"],
            "reason": norm_reason,
            "justification": clean_just,
            "mode": config.get_support_mode(),
        }

        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO drafts (
                        draft_id, draft_type, status, project_id, case_name,
                        payload_json, result_case_name, create_time, update_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft_id,
                        "ESCALATE_CASE",
                        "PENDING_CONFIRMATION",
                        validated_project,
                        normalized_case,
                        json.dumps(payload),
                        normalized_case,
                        now,
                        now,
                    ),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to save escalation draft: %s", type(exc).__name__)
            raise RuntimeError("Failed to persist escalation draft.") from exc

        return self.get_draft(draft_id)

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        """Retrieve a draft by draft_id."""
        clean_id = (draft_id or "").strip()
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM drafts WHERE draft_id = ?", (clean_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.error("Failed to fetch draft: %s", type(exc).__name__)
            raise RuntimeError("Database error fetching draft.") from exc

        if not row:
            raise KeyError(f"Draft '{clean_id}' not found.")

        payload = json.loads(row["payload_json"])
        if "subscriber_emails" in payload:
            payload["masked_subscriber_emails"] = [
                mask_email(e) for e in payload["subscriber_emails"]
            ]
        return {
            "draft_id": row["draft_id"],
            "draft_type": row["draft_type"],
            "status": row["status"],
            "project_id": row["project_id"],
            "case_name": row["case_name"],
            "payload": payload,
            "result_case_name": row["result_case_name"],
            "create_time": row["create_time"],
            "update_time": row["update_time"],
        }

    def list_pending_drafts(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """List all drafts currently in PENDING_CONFIRMATION state."""
        try:
            with self._get_conn() as conn:
                if project_id:
                    validated_project = config.validate_project_access(project_id)
                    rows = conn.execute(
                        "SELECT draft_id FROM drafts WHERE status = ? AND project_id = ? ORDER BY create_time DESC",
                        ("PENDING_CONFIRMATION", validated_project),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT draft_id FROM drafts WHERE status = ? ORDER BY create_time DESC",
                        ("PENDING_CONFIRMATION",),
                    ).fetchall()
        except sqlite3.Error as exc:
            logger.error("Failed to list pending drafts: %s", type(exc).__name__)
            raise RuntimeError("Failed to list pending drafts.") from exc

        return [self.get_draft(r["draft_id"]) for r in rows]

    def cancel_draft(self, draft_id: str) -> dict[str, Any]:
        """Cancel a pending draft."""
        draft = self.get_draft(draft_id)
        config.validate_project_access(draft["project_id"])
        now = _utc_now_iso()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE drafts SET status = ?, update_time = ? WHERE draft_id = ?",
                    ("CANCELLED", now, draft["draft_id"]),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to cancel draft: %s", type(exc).__name__)
            raise RuntimeError("Failed to cancel draft.") from exc
        return self.get_draft(draft["draft_id"])

    def claim_draft_owner(self, draft_id: str, owner: str, chat_id: str = "") -> bool:
        """Record the IM requester (and originating chat_id) that owns a draft, only if no owner is set yet.

        Returns True if `owner` now owns the draft (newly claimed or already owned by them).
        """
        clean_id = (draft_id or "").strip()
        clean_owner = (owner or "").strip()
        clean_chat = (chat_id or "").strip()
        if not clean_id or not clean_owner:
            raise ValueError("draft_id and owner are required.")
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE drafts SET owner = ?, chat_id = ? WHERE draft_id = ? AND (owner IS NULL OR owner = '')",
                    (clean_owner, clean_chat, clean_id),
                )
                if clean_chat:
                    conn.execute(
                        "UPDATE drafts SET chat_id = ? WHERE draft_id = ? AND owner = ? AND (chat_id IS NULL OR chat_id = '')",
                        (clean_chat, clean_id, clean_owner),
                    )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to record draft owner: %s", type(exc).__name__)
            raise RuntimeError("Failed to record draft owner.") from exc
        return self.get_draft_owner(clean_id) == clean_owner

    def get_draft_owner(self, draft_id: str) -> str | None:
        """Return the recorded IM requester of a draft, or None if unset. Raises KeyError if missing."""
        clean_id = (draft_id or "").strip()
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT owner FROM drafts WHERE draft_id = ?", (clean_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.error("Failed to fetch draft owner: %s", type(exc).__name__)
            raise RuntimeError("Database error fetching draft owner.") from exc
        if not row:
            raise KeyError(f"Draft '{clean_id}' not found.")
        return row["owner"] or None

    def get_draft_chat_id(self, draft_id: str) -> str | None:
        """Return the recorded IM chat_id of a draft, or None if unset. Raises KeyError if missing."""
        clean_id = (draft_id or "").strip()
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT chat_id FROM drafts WHERE draft_id = ?", (clean_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.error("Failed to fetch draft chat_id: %s", type(exc).__name__)
            raise RuntimeError("Database error fetching draft chat_id.") from exc
        if not row:
            raise KeyError(f"Draft '{clean_id}' not found.")
        return row["chat_id"] or None

    # -------------------------------------------------------------------------
    # LIVE SDK Sync & Media Helpers
    # -------------------------------------------------------------------------
    def _extract_live_case_fields(
        self, gcp_case: Any, fallback_project: str = ""
    ) -> dict[str, Any]:
        """Extract normalized SQLite column values from a `google.cloud.support_v2.Case` message."""
        case_name = str(getattr(gcp_case, "name", "") or "").strip()
        case_id = case_name.split("/")[-1] if "/" in case_name else case_name
        project_id = config.extract_project_id_from_case_name(case_name) or fallback_project
        prio_raw = _enum_name(getattr(gcp_case, "priority", None), "P3")
        priority = prio_raw if prio_raw in ("P0", "P1", "P2", "P3", "P4") else "P3"
        severity = PRIORITY_TO_SEVERITY.get(priority, "S3")
        state_raw = _enum_name(getattr(gcp_case, "state", None), "NEW")
        state = "NEW" if state_raw in ("", "STATE_UNSPECIFIED") else state_raw
        cls_obj = getattr(gcp_case, "classification", None)
        cls_id = str(getattr(cls_obj, "id", "") or "")
        cls_name = str(getattr(cls_obj, "display_name", "") or "")
        creator = getattr(gcp_case, "creator", None)
        creator_name = str(
            getattr(creator, "display_name", "")
            or getattr(creator, "email", "")
            or "GCP Customer Portal"
        )
        emails = parse_subscriber_emails(
            list(getattr(gcp_case, "subscriber_email_addresses", None) or [])
        )
        create_time = _proto_timestamp_to_iso(getattr(gcp_case, "create_time", None))
        update_time = _proto_timestamp_to_iso(
            getattr(gcp_case, "update_time", None), fallback=create_time
        )
        return {
            "case_name": case_name,
            "case_id": case_id,
            "project_id": project_id,
            "display_name": str(getattr(gcp_case, "display_name", "") or ""),
            "description": str(getattr(gcp_case, "description", "") or ""),
            "severity": severity,
            "priority": priority,
            "state": state,
            "classification_id": cls_id,
            "classification_display_name": cls_name,
            "time_zone": str(getattr(gcp_case, "time_zone", "") or "UTC"),
            "subscriber_emails_json": json.dumps(emails),
            "escalated": 1 if bool(getattr(gcp_case, "escalated", False)) else 0,
            "test_case": 1 if bool(getattr(gcp_case, "test_case", False)) else 0,
            "creator_name": creator_name,
            "create_time": create_time,
            "update_time": update_time,
        }

    def _upsert_live_case_row(
        self, conn: sqlite3.Connection, fields: dict[str, Any]
    ) -> None:
        """Upsert a remote Cloud Support case into local SQLite while preserving local escalation notes."""
        conn.execute(
            """
            INSERT INTO cases (
                case_name, case_id, project_id, display_name, description,
                severity, priority, state, classification_id, classification_display_name,
                time_zone, subscriber_emails_json, escalated, escalation_reason,
                escalation_justification, test_case, creator_name, create_time, update_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_name) DO UPDATE SET
                display_name = excluded.display_name,
                description = CASE
                    WHEN excluded.description != '' THEN excluded.description
                    ELSE cases.description
                END,
                severity = excluded.severity,
                priority = excluded.priority,
                state = excluded.state,
                classification_id = CASE
                    WHEN excluded.classification_id != '' THEN excluded.classification_id
                    ELSE cases.classification_id
                END,
                classification_display_name = CASE
                    WHEN excluded.classification_display_name != '' THEN excluded.classification_display_name
                    ELSE cases.classification_display_name
                END,
                time_zone = excluded.time_zone,
                subscriber_emails_json = excluded.subscriber_emails_json,
                escalated = excluded.escalated,
                test_case = excluded.test_case,
                creator_name = excluded.creator_name,
                create_time = excluded.create_time,
                update_time = excluded.update_time
            """,
            (
                fields["case_name"],
                fields["case_id"],
                fields["project_id"],
                fields["display_name"],
                fields["description"],
                fields["severity"],
                fields["priority"],
                fields["state"],
                fields["classification_id"],
                fields["classification_display_name"],
                fields["time_zone"],
                fields["subscriber_emails_json"],
                fields["escalated"],
                "",
                "",
                fields["test_case"],
                fields["creator_name"],
                fields["create_time"],
                fields["update_time"],
            ),
        )

    def _sync_live_case_details(self, normalized_case: str) -> None:
        """Fetch case, comments, and attachments from Google Cloud Support API v2 into SQLite."""
        from google.cloud import support_v2

        try:
            from google.api_core import exceptions as gexc
        except ImportError:  # pragma: no cover
            gexc = None

        case_client = _make_live_client(support_v2.CaseServiceClient)
        try:
            gcp_case = case_client.get_case(
                request=support_v2.GetCaseRequest(name=normalized_case)
            )
        except Exception as exc:
            if gexc is not None and isinstance(exc, gexc.NotFound):
                raise KeyError(f"Support case '{normalized_case}' not found.") from exc
            raise

        fallback_project = config.extract_project_id_from_case_name(normalized_case) or ""
        fields = self._extract_live_case_fields(gcp_case, fallback_project=fallback_project)

        cmt_client = _make_live_client(support_v2.CommentServiceClient)
        remote_comments = list(
            cmt_client.list_comments(
                request=support_v2.ListCommentsRequest(parent=normalized_case, page_size=100)
            )
        )

        remote_attachments: list[Any] = []
        try:
            att_client = _make_live_client(support_v2.CaseAttachmentServiceClient)
            remote_attachments = list(
                att_client.list_attachments(
                    request=support_v2.ListAttachmentsRequest(
                        parent=normalized_case, page_size=100
                    )
                )
            )
        except Exception as exc:
            logger.warning(
                "Could not list remote attachments for %s (%s); retaining cached records.",
                normalized_case,
                type(exc).__name__,
            )

        try:
            with self._get_conn() as conn:
                self._upsert_live_case_row(conn, fields)
                for cmt in remote_comments:
                    raw_name = str(getattr(cmt, "name", "") or "").strip()
                    cmt_id = (
                        raw_name.split("/")[-1]
                        if "/" in raw_name
                        else (raw_name or f"cmt-remote-{uuid.uuid4().hex[:10]}")
                    )
                    creator = getattr(cmt, "creator", None)
                    is_google = bool(getattr(creator, "google_support", False))
                    author_type = "GOOGLE_SUPPORT" if is_google else "CUSTOMER"
                    author = str(
                        getattr(creator, "display_name", "")
                        or getattr(creator, "email", "")
                        or ("Google Cloud Support" if is_google else "Customer")
                    )
                    body = str(
                        getattr(cmt, "body", "")
                        or getattr(cmt, "plain_text_body", "")
                        or ""
                    )
                    ctime = _proto_timestamp_to_iso(getattr(cmt, "create_time", None))
                    conn.execute(
                        """
                        INSERT INTO comments (comment_id, case_name, body, author, author_type, create_time)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(comment_id) DO UPDATE SET
                            body = excluded.body,
                            author = excluded.author,
                            author_type = excluded.author_type,
                            create_time = excluded.create_time
                        """,
                        (cmt_id, normalized_case, body, author, author_type, ctime),
                    )
                for att in remote_attachments:
                    raw_att_name = str(getattr(att, "name", "") or "").strip()
                    att_id = (
                        raw_att_name.split("/")[-1]
                        if "/" in raw_att_name
                        else (raw_att_name or f"att-remote-{uuid.uuid4().hex[:10]}")
                    )
                    filename = str(getattr(att, "filename", "") or "attachment")
                    mime_type = str(
                        getattr(att, "mime_type", "") or "application/octet-stream"
                    )
                    size_bytes = int(getattr(att, "size_bytes", 0) or 0)
                    ctime = _proto_timestamp_to_iso(getattr(att, "create_time", None))
                    conn.execute(
                        """
                        INSERT INTO attachments (
                            attachment_id, case_name, original_filename,
                            stored_filename, mime_type, size_bytes, create_time
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(attachment_id) DO UPDATE SET
                            original_filename = excluded.original_filename,
                            mime_type = excluded.mime_type,
                            size_bytes = excluded.size_bytes,
                            create_time = excluded.create_time
                        """,
                        (att_id, normalized_case, filename, "", mime_type, size_bytes, ctime),
                    )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to sync live case details: %s", type(exc).__name__)
            raise RuntimeError("Database error syncing support case.") from exc

    @staticmethod
    def _get_google_access_token() -> str:
        """Obtain a short-lived OAuth2 Bearer token from Application Default Credentials."""
        return _get_live_oauth_token()

    def _upload_live_attachment(
        self,
        normalized_case: str,
        original_filename: str,
        mime_type: str,
        content_bytes: bytes,
    ) -> dict[str, Any]:
        """Upload raw attachment bytes to Google Cloud Support API v2 media.upload over HTTPS."""
        safe_case = config.normalize_case_name(normalized_case)
        encoded_name = urllib.parse.quote(original_filename, safe="")
        url = (
            f"https://cloudsupport.googleapis.com/upload/v2/{safe_case}/attachments"
            f"?attachment.filename={encoded_name}"
        )
        token = self._get_google_access_token()
        req = urllib.request.Request(
            url,
            data=content_bytes,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": mime_type or "application/octet-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            logger.error("Cloud Support attachment upload HTTP %s: %s", exc.code, detail)
            raise RuntimeError(
                f"Google Cloud Support attachment upload failed (HTTP {exc.code})."
            ) from exc

    def download_live_attachment_bytes(self, case_name: str, attachment_id: str) -> bytes:
        """Download remote attachment bytes from Google Cloud Support API v2 media.download."""
        safe_case = config.normalize_case_name(case_name)
        config.validate_project_access(safe_case)
        clean_att_id = (attachment_id or "").strip()
        if not re.match(r"^[A-Za-z0-9_\-]+$", clean_att_id):
            raise ValueError("Invalid attachment ID format.")
        url = (
            f"https://cloudsupport.googleapis.com/v2/{safe_case}/attachments/"
            f"{clean_att_id}:download?alt=media"
        )
        backoff_delays = (0.8, 1.5, 2.5)
        for attempt in range(len(backoff_delays) + 1):
            token = self._get_google_access_token()
            req = urllib.request.Request(
                url,
                method="GET",
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise KeyError(
                        f"Attachment '{clean_att_id}' not found on remote case."
                    ) from exc
                if exc.code in (401, 429, 500, 502, 503, 504) and attempt < len(backoff_delays):
                    if exc.code == 401:
                        _invalidate_live_oauth_token()
                    time.sleep(backoff_delays[attempt])
                    continue
                logger.error("Cloud Support attachment download HTTP %s", exc.code)
                raise RuntimeError(
                    f"Google Cloud Support attachment download failed (HTTP {exc.code})."
                ) from exc
        raise RuntimeError("Google Cloud Support attachment download failed.")

    def _mark_comment_seen_in_watch_state(
        self, conn: sqlite3.Connection, case_name: str, comment_id: str
    ) -> None:
        """If case_watch_state already tracks `case_name`, record `comment_id` to prevent self-echo."""
        row = conn.execute(
            "SELECT seen_comment_ids_json FROM case_watch_state WHERE case_name = ?",
            (case_name,),
        ).fetchone()
        if not row:
            return
        try:
            seen: list[str] = list(json.loads(row["seen_comment_ids_json"] or "[]"))
        except (ValueError, TypeError):
            seen = []
        if comment_id not in seen:
            seen.append(comment_id)
            conn.execute(
                "UPDATE case_watch_state SET seen_comment_ids_json = ?, updated_at = ? WHERE case_name = ?",
                (json.dumps(seen[-500:]), _utc_now_iso(), case_name),
            )

    def _mark_attachment_seen_in_watch_state(
        self, conn: sqlite3.Connection, case_name: str, attachment_id: str
    ) -> None:
        """If case_watch_state already tracks `case_name`, record `attachment_id` to prevent self-echo."""
        row = conn.execute(
            "SELECT seen_attachment_ids_json FROM case_watch_state WHERE case_name = ?",
            (case_name,),
        ).fetchone()
        if not row:
            return
        try:
            seen: list[str] = list(json.loads(row["seen_attachment_ids_json"] or "[]"))
        except (ValueError, TypeError):
            seen = []
        if attachment_id not in seen:
            seen.append(attachment_id)
            conn.execute(
                "UPDATE case_watch_state SET seen_attachment_ids_json = ?, updated_at = ? WHERE case_name = ?",
                (json.dumps(seen[-300:]), _utc_now_iso(), case_name),
            )

    # -------------------------------------------------------------------------
    # 3. Confirm & Create Case
    # -------------------------------------------------------------------------
    def confirm_and_create_case(
        self,
        draft_id: str,
        confirmed: bool,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Confirm a pending CREATE_CASE draft and execute cases.create."""
        if confirmed is not True:
            raise PermissionError(
                "HITL Guardrail: Case creation requires explicit user confirmation (confirmed=True)."
            )

        draft = self.get_draft(draft_id)
        if draft["draft_type"] != "CREATE_CASE":
            raise ValueError(f"Draft '{draft_id}' is not a CREATE_CASE draft.")
        if draft["status"] != "PENDING_CONFIRMATION":
            raise ValueError(
                f"Draft '{draft_id}' is in state '{draft['status']}' and cannot be confirmed again."
            )

        payload = dict(draft["payload"])
        if overrides:
            if "display_name" in overrides and overrides["display_name"]:
                payload["display_name"] = str(overrides["display_name"]).strip()[:200]
            if "description" in overrides and overrides["description"]:
                payload["description"] = str(overrides["description"]).strip()[:8000]
            if "severity" in overrides and overrides["severity"]:
                norm_sev = PRIORITY_TO_SEVERITY.get(
                    str(overrides["severity"]).upper().strip(), payload["severity"]
                )
                payload["severity"] = norm_sev
                payload["priority"] = SEVERITY_TO_PRIORITY[norm_sev]
            if "subscriber_emails" in overrides:
                payload["subscriber_emails"] = parse_subscriber_emails(
                    overrides["subscriber_emails"]
                )

        validated_project = config.validate_project_access(payload["project_id"])
        now = _utc_now_iso()
        case_id = str(uuid.uuid4().int)[:8]
        case_name = f"projects/{validated_project}/cases/{case_id}"
        test_case_flag = True if config.is_force_test_case() else bool(payload.get("test_case", True))
        case_state = "OPEN"
        case_priority = payload["priority"]
        case_severity = payload["severity"]
        escalated_int = 0
        test_case_int = 1 if test_case_flag else 0
        creator_name = "GCP Customer Portal"
        create_ts = now
        update_ts = now

        if config.get_support_mode() == "LIVE":
            from google.api_core import exceptions as gcp_exceptions
            from google.cloud import support_v2

            payload["classification_id"], payload["classification_display_name"] = (
                self._resolve_live_classification(
                    payload.get("classification_id", ""),
                    payload.get("classification_display_name", ""),
                    f"{payload['display_name']} {payload['description']}",
                )
            )
            client = _make_live_client(support_v2.CaseServiceClient)

            def _build_gcp_case(cls_id: str, cls_disp: str) -> Any:
                return support_v2.Case(
                    display_name=payload["display_name"],
                    description=payload["description"],
                    priority=getattr(
                        support_v2.Case.Priority,
                        payload["priority"],
                        support_v2.Case.Priority.P3,
                    ),
                    classification=support_v2.CaseClassification(
                        id=cls_id,
                        display_name=cls_disp,
                    ),
                    time_zone=payload.get("time_zone", "Asia/Shanghai"),
                    subscriber_email_addresses=payload.get("subscriber_emails", []),
                    test_case=test_case_flag,
                )

            try:
                created = client.create_case(
                    parent=f"projects/{validated_project}",
                    case=_build_gcp_case(
                        payload["classification_id"], payload["classification_display_name"]
                    ),
                )
            except gcp_exceptions.InvalidArgument as exc:
                if "classification" not in str(exc).lower():
                    raise
                logger.warning("Classification rejected by LIVE API; refreshing catalog and retrying once.")
                global _LIVE_CLASSIFICATION_CACHE
                _LIVE_CLASSIFICATION_CACHE = None
                new_cid, new_cdisp = self._resolve_live_classification(
                    "", "", f"{payload['display_name']} {payload['description']}"
                )
                payload["classification_id"] = new_cid
                payload["classification_display_name"] = new_cdisp
                created = client.create_case(
                    parent=f"projects/{validated_project}",
                    case=_build_gcp_case(new_cid, new_cdisp),
                )
            live_fields = self._extract_live_case_fields(
                created, fallback_project=validated_project
            )
            case_name = live_fields["case_name"] or case_name
            case_id = live_fields["case_id"] or case_name.split("/")[-1]
            case_state = live_fields["state"]
            case_priority = live_fields["priority"]
            case_severity = live_fields["severity"]
            escalated_int = live_fields["escalated"]
            test_case_int = live_fields["test_case"]
            creator_name = live_fields["creator_name"]
            create_ts = live_fields["create_time"]
            update_ts = live_fields["update_time"]

        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO cases (
                        case_name, case_id, project_id, display_name, description,
                        severity, priority, state, classification_id, classification_display_name,
                        time_zone, subscriber_emails_json, escalated, escalation_reason,
                        escalation_justification, test_case, creator_name, create_time, update_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(case_name) DO UPDATE SET
                        state = excluded.state,
                        priority = excluded.priority,
                        severity = excluded.severity,
                        update_time = excluded.update_time
                    """,
                    (
                        case_name,
                        case_id,
                        validated_project,
                        payload["display_name"],
                        payload["description"],
                        case_severity,
                        case_priority,
                        case_state,
                        payload["classification_id"],
                        payload["classification_display_name"],
                        payload.get("time_zone", "Asia/Shanghai"),
                        json.dumps(payload.get("subscriber_emails", [])),
                        escalated_int,
                        "",
                        "",
                        test_case_int,
                        creator_name,
                        create_ts,
                        update_ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO comments (comment_id, case_name, body, author, author_type, create_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"cmt-{case_id}-init",
                        case_name,
                        f"Support case created via Cloud Case Agent ({config.get_support_mode()} mode, test_case={bool(test_case_int)}).",
                        "Google Cloud Support System",
                        "GOOGLE_SUPPORT",
                        create_ts,
                    ),
                )
                conn.execute(
                    """
                    UPDATE drafts
                    SET status = ?, result_case_name = ?, payload_json = ?, update_time = ?
                    WHERE draft_id = ?
                    """,
                    (
                        "CONFIRMED",
                        case_name,
                        json.dumps(payload),
                        now,
                        draft["draft_id"],
                    ),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to create support case: %s", type(exc).__name__)
            raise RuntimeError("Database error creating support case.") from exc

        upload_errors: list[str] = []
        pending_atts = payload.get("pending_attachments") or []
        if isinstance(pending_atts, list) and pending_atts:
            from app.security import resolve_stored_upload_path

            for item in pending_atts[:10]:
                if not isinstance(item, dict):
                    continue
                stored_fn = str(item.get("stored_filename") or "").strip()
                orig_fn = str(item.get("original_filename") or "attachment").strip()
                mime_t = str(item.get("mime_type") or "application/octet-stream").strip()
                if not stored_fn:
                    continue
                try:
                    content_bytes = resolve_stored_upload_path(stored_fn).read_bytes()
                    self.add_attachment_metadata(
                        case_name=case_name,
                        original_filename=orig_fn,
                        stored_filename=stored_fn,
                        mime_type=mime_t,
                        size_bytes=len(content_bytes),
                        content_bytes=content_bytes,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed uploading staged draft attachment %s: %s",
                        orig_fn,
                        type(exc).__name__,
                    )
                    upload_errors.append(orig_fn)

        created_case = self._get_local_case(case_name)
        if upload_errors:
            created_case["attachment_upload_errors"] = upload_errors
        return created_case

    # -------------------------------------------------------------------------
    # 4. List & Get Cases
    # -------------------------------------------------------------------------
    def list_cases(
        self,
        project_id: str,
        state_filter: str = "ALL",
    ) -> list[dict[str, Any]]:
        """List support cases for an authorized GCP project with optional state filter."""
        validated_project = config.validate_project_access(project_id)
        norm_filter = (state_filter or "ALL").upper().strip()

        if config.get_support_mode() == "LIVE":
            from google.cloud import support_v2

            client = _make_live_client(support_v2.CaseServiceClient)
            remote_cases = list(
                client.list_cases(
                    request=support_v2.ListCasesRequest(
                        parent=f"projects/{validated_project}",
                        page_size=100,
                    )
                )
            )
            try:
                with self._get_conn() as conn:
                    for gcp_case in remote_cases:
                        fields = self._extract_live_case_fields(
                            gcp_case, fallback_project=validated_project
                        )
                        if fields["case_name"]:
                            self._upsert_live_case_row(conn, fields)
                    conn.commit()
            except sqlite3.Error as exc:
                logger.error("Failed to sync LIVE cases list: %s", type(exc).__name__)
                raise RuntimeError("Database error syncing support cases.") from exc

        try:
            with self._get_conn() as conn:
                if norm_filter in ("", "ALL"):
                    rows = conn.execute(
                        "SELECT * FROM cases WHERE project_id = ? ORDER BY update_time DESC",
                        (validated_project,),
                    ).fetchall()
                elif norm_filter == "ESCALATED":
                    rows = conn.execute(
                        "SELECT * FROM cases WHERE project_id = ? AND escalated = 1 ORDER BY update_time DESC",
                        (validated_project,),
                    ).fetchall()
                elif norm_filter == "OPEN":
                    rows = conn.execute(
                        "SELECT * FROM cases WHERE project_id = ? AND state != 'CLOSED' ORDER BY update_time DESC",
                        (validated_project,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM cases WHERE project_id = ? AND state = ? ORDER BY update_time DESC",
                        (validated_project, norm_filter),
                    ).fetchall()
        except sqlite3.Error as exc:
            logger.error("Failed to list support cases: %s", type(exc).__name__)
            raise RuntimeError("Failed to list support cases.") from exc

        return [self._row_to_case_dict(r) for r in rows]

    def _get_local_case(self, normalized_case: str) -> dict[str, Any]:
        """Read full case details, comments, and attachments from local SQLite."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM cases WHERE case_name = ?", (normalized_case,)
                ).fetchone()
                if not row:
                    raise KeyError(f"Support case '{normalized_case}' not found.")

                comment_rows = conn.execute(
                    "SELECT * FROM comments WHERE case_name = ? ORDER BY create_time ASC",
                    (normalized_case,),
                ).fetchall()
                attachment_rows = conn.execute(
                    "SELECT * FROM attachments WHERE case_name = ? ORDER BY create_time DESC",
                    (normalized_case,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.error("Failed to query case details: %s", type(exc).__name__)
            raise RuntimeError("Failed to query case details.") from exc

        comments = [
            {
                "comment_id": c["comment_id"],
                "case_name": c["case_name"],
                "body": c["body"],
                "author": mask_email(c["author"]) if "@" in c["author"] else c["author"],
                "author_type": c["author_type"],
                "create_time": c["create_time"],
            }
            for c in comment_rows
        ]
        attachments = [
            {
                "attachment_id": a["attachment_id"],
                "case_name": a["case_name"],
                "filename": a["original_filename"],
                "mime_type": a["mime_type"],
                "size_bytes": a["size_bytes"],
                "create_time": a["create_time"],
                "download_url": f"/api/attachments/{a['attachment_id']}/download",
            }
            for a in attachment_rows
        ]
        return self._row_to_case_dict(row, comments=comments, attachments=attachments)

    def get_case(self, case_name: str) -> dict[str, Any]:
        """Get full details, comments, and attachment metadata for a support case."""
        config.validate_project_access(case_name)
        normalized_case = config.normalize_case_name(case_name)

        if config.get_support_mode() == "LIVE":
            self._sync_live_case_details(normalized_case)

        return self._get_local_case(normalized_case)

    # -------------------------------------------------------------------------
    # 5. Update Case, Add Comment, Escalate, Close, Attachments
    # -------------------------------------------------------------------------
    def update_case(
        self,
        case_name: str,
        severity: str | None = None,
        subscriber_emails: str | list[str] | None = None,
        append_subscribers: bool = False,
    ) -> dict[str, Any]:
        """Update severity or subscriber emails on an open support case."""
        config.validate_project_access(case_name)
        normalized_case = config.normalize_case_name(case_name)
        existing = self.get_case(normalized_case)
        if existing["state"] == "CLOSED":
            raise ValueError(
                "Cannot update a CLOSED support case. Use prepare_followup_case_draft to create a follow-up case."
            )
        now = _utc_now_iso()

        new_sev = existing["severity"]
        new_prio = existing["priority"]
        parsed_emails: list[str] | None = None
        update_paths: list[str] = []

        if severity:
            norm_sev = PRIORITY_TO_SEVERITY.get(severity.upper().strip(), existing["severity"])
            if norm_sev in VALID_SEVERITIES:
                new_sev = norm_sev
                new_prio = SEVERITY_TO_PRIORITY[norm_sev]
                update_paths.append("priority")

        if subscriber_emails is not None:
            incoming_emails = parse_subscriber_emails(subscriber_emails)
            if append_subscribers:
                current_raw: list[str] = []
                try:
                    with self._get_conn() as conn:
                        row = conn.execute(
                            "SELECT subscriber_emails_json FROM cases WHERE case_name = ?",
                            (normalized_case,),
                        ).fetchone()
                        if row and row["subscriber_emails_json"]:
                            current_raw = list(json.loads(row["subscriber_emails_json"]))
                except Exception:
                    current_raw = []
                merged: list[str] = []
                for em in [*current_raw, *incoming_emails]:
                    if em not in merged:
                        merged.append(em)
                parsed_emails = merged[:20]
            else:
                parsed_emails = incoming_emails
            update_paths.append("subscriber_email_addresses")

        if config.get_support_mode() == "LIVE" and update_paths:
            from google.cloud import support_v2
            from google.protobuf import field_mask_pb2

            client = _make_live_client(support_v2.CaseServiceClient)
            patch_kwargs: dict[str, Any] = {"name": normalized_case}
            if "priority" in update_paths:
                patch_kwargs["priority"] = getattr(
                    support_v2.Case.Priority, new_prio, support_v2.Case.Priority.P3
                )
            if parsed_emails is not None:
                patch_kwargs["subscriber_email_addresses"] = parsed_emails
            updated_remote = client.update_case(
                request=support_v2.UpdateCaseRequest(
                    case=support_v2.Case(**patch_kwargs),
                    update_mask=field_mask_pb2.FieldMask(paths=update_paths),
                )
            )
            live_fields = self._extract_live_case_fields(
                updated_remote, fallback_project=existing["project_id"]
            )
            now = live_fields["update_time"]

        try:
            with self._get_conn() as conn:
                if parsed_emails is not None:
                    conn.execute(
                        "UPDATE cases SET severity = ?, priority = ?, subscriber_emails_json = ?, update_time = ? WHERE case_name = ?",
                        (new_sev, new_prio, json.dumps(parsed_emails), now, normalized_case),
                    )
                else:
                    conn.execute(
                        "UPDATE cases SET severity = ?, priority = ?, update_time = ? WHERE case_name = ?",
                        (new_sev, new_prio, now, normalized_case),
                    )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to update case: %s", type(exc).__name__)
            raise RuntimeError("Failed to update support case.") from exc

        return self._get_local_case(normalized_case)

    def add_comment(
        self,
        case_name: str,
        body: str,
        author: str = "customer-user@example.com",
    ) -> dict[str, Any]:
        """Add a comment to an existing support case."""
        config.validate_project_access(case_name)
        normalized_case = config.normalize_case_name(case_name)
        existing = self.get_case(normalized_case)
        if existing["state"] == "CLOSED":
            raise ValueError(
                "Cannot add comments to a CLOSED support case (工单已关闭；如需继续跟进，请基于原工单创建关联续单)."
            )

        from app.security import redact_sensitive_secrets

        raw_body = (body or "").strip()
        clean_body, redacted_secrets = redact_sensitive_secrets(raw_body)
        if not clean_body or len(clean_body) > 4000:
            raise ValueError("Comment body must be between 1 and 4000 characters.")

        comment_id = f"cmt-{uuid.uuid4().hex[:10]}"
        now = _utc_now_iso()
        next_state = existing["state"]
        if config.get_support_mode() == "LIVE":
            from google.cloud import support_v2

            client = _make_live_client(support_v2.CommentServiceClient)
            created_cmt = client.create_comment(
                parent=normalized_case,
                comment=support_v2.Comment(body=clean_body),
            )
            raw_cmt_name = str(getattr(created_cmt, "name", "") or "").strip()
            if raw_cmt_name:
                comment_id = raw_cmt_name.split("/")[-1]
            now = _proto_timestamp_to_iso(
                getattr(created_cmt, "create_time", None), fallback=now
            )
        elif existing["state"] in ("ACTION_REQUIRED", "SOLUTION_PROVIDED"):
            next_state = "IN_PROGRESS_GOOGLE_SUPPORT"

        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO comments (comment_id, case_name, body, author, author_type, create_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(comment_id) DO UPDATE SET
                        body = excluded.body,
                        create_time = excluded.create_time
                    """,
                    (comment_id, normalized_case, clean_body, author, "CUSTOMER", now),
                )
                conn.execute(
                    "UPDATE cases SET state = ?, update_time = ? WHERE case_name = ?",
                    (next_state, now, normalized_case),
                )
                if next_state != existing["state"]:
                    conn.execute(
                        "UPDATE case_watch_state SET last_state = ?, updated_at = ? WHERE case_name = ?",
                        (next_state, now, normalized_case),
                    )
                self._mark_comment_seen_in_watch_state(conn, normalized_case, comment_id)
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to add comment: %s", type(exc).__name__)
            raise RuntimeError("Failed to add comment to support case.") from exc

        comment_res: dict[str, Any] = {
            "comment_id": comment_id,
            "case_name": normalized_case,
            "body": clean_body,
            "author": mask_email(author) if "@" in author else author,
            "author_type": "CUSTOMER",
            "create_time": now,
            "case_state": next_state,
        }
        if redacted_secrets:
            comment_res["redacted_secrets"] = redacted_secrets
        return comment_res

    def confirm_and_escalate_case(
        self,
        draft_id: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Confirm a pending ESCALATE_CASE draft and execute cases.escalate."""
        if confirmed is not True:
            raise PermissionError(
                "HITL Guardrail: Case escalation requires explicit user confirmation (confirmed=True)."
            )

        draft = self.get_draft(draft_id)
        if draft["draft_type"] != "ESCALATE_CASE":
            raise ValueError(f"Draft '{draft_id}' is not an ESCALATE_CASE draft.")
        if draft["status"] != "PENDING_CONFIRMATION":
            raise ValueError(
                f"Draft '{draft_id}' is in state '{draft['status']}' and cannot be confirmed again."
            )

        payload = draft["payload"]
        case_name = config.normalize_case_name(payload["case_name"])
        config.validate_project_access(case_name)
        now = _utc_now_iso()
        new_state = "IN_PROGRESS_GOOGLE_SUPPORT"

        if config.get_support_mode() == "LIVE":
            from google.cloud import support_v2

            client = _make_live_client(support_v2.CaseServiceClient)
            reason_enum = getattr(
                support_v2.Escalation.Reason,
                payload["reason"],
                support_v2.Escalation.Reason.BUSINESS_IMPACT,
            )
            escalated_remote = client.escalate_case(
                request=support_v2.EscalateCaseRequest(
                    name=case_name,
                    escalation=support_v2.Escalation(
                        reason=reason_enum,
                        justification=payload["justification"],
                    ),
                )
            )
            if getattr(escalated_remote, "name", ""):
                live_fields = self._extract_live_case_fields(
                    escalated_remote, fallback_project=draft["project_id"]
                )
                new_state = live_fields["state"]
                now = live_fields["update_time"]

        esc_cmt_id = f"cmt-esc-{uuid.uuid4().hex[:8]}"
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE cases
                    SET escalated = 1,
                        escalation_reason = ?,
                        escalation_justification = ?,
                        state = ?,
                        update_time = ?
                    WHERE case_name = ?
                    """,
                    (
                        payload["reason"],
                        payload["justification"],
                        new_state,
                        now,
                        case_name,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO comments (comment_id, case_name, body, author, author_type, create_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        esc_cmt_id,
                        case_name,
                        f"[工单已升级 Escalated] 原因: {payload['reason']} | 说明: {payload['justification']}",
                        "Google Cloud Escalation Management",
                        "GOOGLE_SUPPORT",
                        now,
                    ),
                )
                self._mark_comment_seen_in_watch_state(conn, case_name, esc_cmt_id)
                conn.execute(
                    "UPDATE drafts SET status = ?, update_time = ? WHERE draft_id = ?",
                    ("CONFIRMED", now, draft["draft_id"]),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to escalate case: %s", type(exc).__name__)
            raise RuntimeError("Failed to escalate support case.") from exc

        return self._get_local_case(case_name)

    def close_case(self, case_name: str) -> dict[str, Any]:
        """Close an existing support case (`cases.close`)."""
        config.validate_project_access(case_name)
        normalized_case = config.normalize_case_name(case_name)
        existing = self.get_case(normalized_case)
        if existing["state"] == "CLOSED":
            return existing

        now = _utc_now_iso()
        if config.get_support_mode() == "LIVE":
            from google.cloud import support_v2

            client = _make_live_client(support_v2.CaseServiceClient)
            closed_remote = client.close_case(
                request=support_v2.CloseCaseRequest(name=normalized_case)
            )
            if getattr(closed_remote, "name", ""):
                live_fields = self._extract_live_case_fields(
                    closed_remote, fallback_project=existing["project_id"]
                )
                now = live_fields["update_time"]

        close_cmt_id = f"cmt-close-{uuid.uuid4().hex[:8]}"
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE cases SET state = 'CLOSED', escalated = 0, update_time = ? WHERE case_name = ?",
                    (now, normalized_case),
                )
                conn.execute(
                    """
                    INSERT INTO comments (comment_id, case_name, body, author, author_type, create_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        close_cmt_id,
                        normalized_case,
                        "工单已由客户确认关闭 (Case closed).",
                        "Google Cloud Support System",
                        "GOOGLE_SUPPORT",
                        now,
                    ),
                )
                self._mark_comment_seen_in_watch_state(conn, normalized_case, close_cmt_id)
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to close case: %s", type(exc).__name__)
            raise RuntimeError("Failed to close support case.") from exc

        return self._get_local_case(normalized_case)

    def create_close_draft(self, case_name: str, reason: str) -> dict[str, Any]:
        """Create a pending HITL confirmation draft before calling cases.close."""
        validated_project = config.validate_project_access(case_name)
        normalized_case = config.normalize_case_name(case_name)
        existing_case = self.get_case(normalized_case)
        if existing_case["state"] == "CLOSED":
            raise ValueError("Support case is already CLOSED.")

        clean_reason = (reason or "").strip()[:500] or "Issue resolved."
        draft_id = f"draft-close-{uuid.uuid4().hex[:10]}"
        now = _utc_now_iso()
        payload = {
            "project_id": validated_project,
            "case_name": normalized_case,
            "case_display_name": existing_case["display_name"],
            "severity": existing_case["severity"],
            "reason": clean_reason,
            "mode": config.get_support_mode(),
        }
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO drafts (
                        draft_id, draft_type, status, project_id, case_name,
                        payload_json, result_case_name, create_time, update_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft_id,
                        "CLOSE_CASE",
                        "PENDING_CONFIRMATION",
                        validated_project,
                        normalized_case,
                        json.dumps(payload),
                        normalized_case,
                        now,
                        now,
                    ),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to save close draft: %s", type(exc).__name__)
            raise RuntimeError("Failed to persist close draft.") from exc

        return self.get_draft(draft_id)

    def confirm_and_close_case(self, draft_id: str, confirmed: bool) -> dict[str, Any]:
        """Confirm a pending CLOSE_CASE draft and execute cases.close."""
        if confirmed is not True:
            raise PermissionError(
                "HITL Guardrail: Closing a case requires explicit user confirmation (confirmed=True)."
            )
        draft = self.get_draft(draft_id)
        if draft["draft_type"] != "CLOSE_CASE":
            raise ValueError(f"Draft '{draft_id}' is not a CLOSE_CASE draft.")
        if draft["status"] != "PENDING_CONFIRMATION":
            raise ValueError(
                f"Draft '{draft_id}' is in state '{draft['status']}' and cannot be confirmed again."
            )

        closed_case = self.close_case(draft["payload"]["case_name"])
        now = _utc_now_iso()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE drafts SET status = ?, update_time = ? WHERE draft_id = ?",
                    ("CONFIRMED", now, draft["draft_id"]),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to finalize close draft: %s", type(exc).__name__)
            raise RuntimeError("Failed to finalize close draft.") from exc
        return closed_case

    def execute_confirmed_draft(
        self,
        draft_id: str,
        confirmed: bool,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Single execution entry point for human-confirmed drafts (create / escalate / close).

        Only called from the human-facing confirm endpoint, never from agent tools.
        Returns a dict with `draft_type` and the resulting `case`.
        """
        draft = self.get_draft(draft_id)
        config.validate_project_access(draft["project_id"])
        draft_type = draft["draft_type"]
        if draft_type == "CREATE_CASE":
            case_obj = self.confirm_and_create_case(
                draft_id=draft_id, confirmed=confirmed, overrides=overrides
            )
        elif draft_type == "ESCALATE_CASE":
            case_obj = self.confirm_and_escalate_case(draft_id=draft_id, confirmed=confirmed)
        elif draft_type == "CLOSE_CASE":
            case_obj = self.confirm_and_close_case(draft_id=draft_id, confirmed=confirmed)
        else:
            raise ValueError(f"Unsupported draft_type: {draft_type}")
        return {"draft_type": draft_type, "case": case_obj}

    def add_attachment_metadata(
        self,
        case_name: str,
        original_filename: str,
        stored_filename: str,
        mime_type: str,
        size_bytes: int,
        content_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        """Record (and in LIVE mode upload) a validated attachment on a support case."""
        config.validate_project_access(case_name)
        normalized_case = config.normalize_case_name(case_name)
        self.get_case(normalized_case)

        attachment_id = f"att-{uuid.uuid4().hex[:12]}"
        now = _utc_now_iso()
        if config.get_support_mode() == "LIVE" and content_bytes is not None:
            uploaded = self._upload_live_attachment(
                normalized_case=normalized_case,
                original_filename=original_filename,
                mime_type=mime_type,
                content_bytes=content_bytes,
            )
            raw_att_name = str(uploaded.get("name", "") or "").strip()
            if raw_att_name:
                attachment_id = raw_att_name.split("/")[-1]
            if uploaded.get("createTime"):
                now = _proto_timestamp_to_iso(uploaded.get("createTime"), fallback=now)

        att_cmt_id = f"cmt-att-{uuid.uuid4().hex[:8]}"
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO attachments (
                        attachment_id, case_name, original_filename,
                        stored_filename, mime_type, size_bytes, create_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(attachment_id) DO UPDATE SET
                        stored_filename = excluded.stored_filename,
                        mime_type = excluded.mime_type,
                        size_bytes = excluded.size_bytes
                    """,
                    (
                        attachment_id,
                        normalized_case,
                        original_filename,
                        stored_filename,
                        mime_type,
                        size_bytes,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO comments (comment_id, case_name, body, author, author_type, create_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        att_cmt_id,
                        normalized_case,
                        f"已上传诊断附件: {original_filename} ({size_bytes} bytes)",
                        "GCP Customer Portal",
                        "CUSTOMER",
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE cases SET update_time = ? WHERE case_name = ?",
                    (now, normalized_case),
                )
                self._mark_comment_seen_in_watch_state(conn, normalized_case, att_cmt_id)
                self._mark_attachment_seen_in_watch_state(conn, normalized_case, attachment_id)
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to record attachment metadata: %s", type(exc).__name__)
            raise RuntimeError("Failed to record attachment metadata.") from exc

        return {
            "attachment_id": attachment_id,
            "case_name": normalized_case,
            "filename": original_filename,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "create_time": now,
            "download_url": f"/api/attachments/{attachment_id}/download",
        }

    def get_attachment_record(self, attachment_id: str) -> dict[str, Any]:
        """Retrieve attachment metadata and verify project access."""
        clean_id = (attachment_id or "").strip()
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM attachments WHERE attachment_id = ?", (clean_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.error("Failed to query attachment: %s", type(exc).__name__)
            raise RuntimeError("Failed to query attachment.") from exc

        if not row:
            raise KeyError(f"Attachment '{clean_id}' not found.")

        config.validate_project_access(row["case_name"])
        return {
            "attachment_id": row["attachment_id"],
            "case_name": row["case_name"],
            "original_filename": row["original_filename"],
            "stored_filename": row["stored_filename"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "create_time": row["create_time"],
        }

    def attach_files_to_draft(
        self, draft_id: str, files: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Bind validated stored files to a PENDING_CONFIRMATION CREATE_CASE draft."""
        draft = self.get_draft(draft_id)
        if draft["draft_type"] != "CREATE_CASE":
            raise ValueError("Attachments can only be staged on CREATE_CASE drafts.")
        if draft["status"] != "PENDING_CONFIRMATION":
            raise ValueError(f"Draft '{draft_id}' is no longer pending confirmation.")
        config.validate_project_access(draft["project_id"])

        payload = dict(draft["payload"])
        existing: list[dict[str, Any]] = list(payload.get("pending_attachments") or [])
        seen_stored = {str(x.get("stored_filename") or "") for x in existing if isinstance(x, dict)}
        for item in files or []:
            if not isinstance(item, dict):
                continue
            stored_fn = str(item.get("stored_filename") or "").strip()
            if not stored_fn or stored_fn in seen_stored:
                continue
            existing.append(
                {
                    "original_filename": str(item.get("original_filename") or "attachment")[:200],
                    "stored_filename": stored_fn,
                    "mime_type": str(item.get("mime_type") or "application/octet-stream")[:100],
                    "size_bytes": int(item.get("size_bytes") or 0),
                }
            )
            seen_stored.add(stored_fn)
            if len(existing) >= 10:
                break

        payload["pending_attachments"] = existing
        now = _utc_now_iso()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE drafts SET payload_json = ?, update_time = ? WHERE draft_id = ?",
                    (json.dumps(payload, ensure_ascii=False), now, draft["draft_id"]),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to attach files to draft: %s", type(exc).__name__)
            raise RuntimeError("Failed to attach files to draft.") from exc

        return self.get_draft(draft["draft_id"])

    # -------------------------------------------------------------------------
    # 6. Case Update Subscriptions & Watch State (Feishu Notification Support)
    # -------------------------------------------------------------------------
    def subscribe_case(
        self, case_name: str, chat_id: str, owner_open_id: str = ""
    ) -> dict[str, Any]:
        """Subscribe a Feishu chat (and optional owner) to updates on an authorized case."""
        config.validate_project_access(case_name)
        normalized_case = config.normalize_case_name(case_name)
        clean_chat = (chat_id or "").strip()
        clean_owner = (owner_open_id or "").strip()
        if not clean_chat:
            raise ValueError("chat_id is required to subscribe to case updates.")
        now = _utc_now_iso()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO case_subscriptions (
                        case_name, chat_id, owner_open_id, active, create_time, update_time
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(case_name, chat_id) DO UPDATE SET
                        owner_open_id = CASE
                            WHEN excluded.owner_open_id != '' THEN excluded.owner_open_id
                            ELSE case_subscriptions.owner_open_id
                        END,
                        active = 1,
                        update_time = excluded.update_time
                    """,
                    (normalized_case, clean_chat, clean_owner, now, now),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to subscribe case updates: %s", type(exc).__name__)
            raise RuntimeError("Database error subscribing to case updates.") from exc
        return {
            "case_name": normalized_case,
            "chat_id": clean_chat,
            "owner_open_id": clean_owner,
            "active": True,
        }

    def unsubscribe_case(self, case_name: str, chat_id: str) -> dict[str, Any]:
        """Deactivate Feishu update notifications for `(case_name, chat_id)`."""
        config.validate_project_access(case_name)
        normalized_case = config.normalize_case_name(case_name)
        clean_chat = (chat_id or "").strip()
        if not clean_chat:
            raise ValueError("chat_id is required to unsubscribe from case updates.")
        now = _utc_now_iso()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE case_subscriptions
                    SET active = 0, update_time = ?
                    WHERE case_name = ? AND chat_id = ?
                    """,
                    (now, normalized_case, clean_chat),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to unsubscribe case updates: %s", type(exc).__name__)
            raise RuntimeError("Database error unsubscribing from case updates.") from exc
        return {
            "case_name": normalized_case,
            "chat_id": clean_chat,
            "active": False,
        }

    def list_case_subscriptions(
        self, case_name: str | None = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        """List recorded Feishu case notification subscriptions."""
        clauses: list[str] = []
        params: list[Any] = []
        if case_name:
            normalized_case = config.normalize_case_name(case_name)
            config.validate_project_access(normalized_case)
            clauses.append("case_name = ?")
            params.append(normalized_case)
        if active_only:
            clauses.append("active = 1")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    f"SELECT * FROM case_subscriptions {where_sql} ORDER BY update_time DESC",
                    tuple(params),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.error("Failed to list case subscriptions: %s", type(exc).__name__)
            raise RuntimeError("Database error listing case subscriptions.") from exc
        return [
            {
                "case_name": r["case_name"],
                "chat_id": r["chat_id"],
                "owner_open_id": r["owner_open_id"] or "",
                "active": bool(r["active"]),
                "create_time": r["create_time"],
                "update_time": r["update_time"],
            }
            for r in rows
        ]

    def get_case_watch_state(self, case_name: str) -> dict[str, Any] | None:
        """Return the last observed state/comment/attachment snapshot for a case, or None if unseen."""
        normalized_case = config.normalize_case_name(case_name)
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM case_watch_state WHERE case_name = ?",
                    (normalized_case,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.error("Failed to fetch case watch state: %s", type(exc).__name__)
            raise RuntimeError("Database error reading case watch state.") from exc
        if not row:
            return None
        try:
            seen_ids = list(json.loads(row["seen_comment_ids_json"] or "[]"))
        except (ValueError, TypeError):
            seen_ids = []
        try:
            raw_att_col = row["seen_attachment_ids_json"] if "seen_attachment_ids_json" in row.keys() else "[]"
            seen_att_ids = list(json.loads(raw_att_col or "[]"))
        except (ValueError, TypeError):
            seen_att_ids = []
        return {
            "case_name": row["case_name"],
            "project_id": row["project_id"],
            "last_state": row["last_state"],
            "last_priority": row["last_priority"],
            "last_escalated": bool(row["last_escalated"]),
            "last_update_time": row["last_update_time"],
            "seen_comment_ids": seen_ids,
            "seen_attachment_ids": seen_att_ids,
            "updated_at": row["updated_at"],
        }

    def save_case_watch_state(
        self,
        case_name: str,
        project_id: str,
        state: str,
        priority: str,
        escalated: bool,
        update_time: str,
        seen_comment_ids: list[str],
        seen_attachment_ids: list[str] | None = None,
    ) -> None:
        """Persist the latest observed state, comment IDs, and attachment IDs for a support case."""
        normalized_case = config.normalize_case_name(case_name)
        now = _utc_now_iso()
        deduped_ids = list(dict.fromkeys(seen_comment_ids))[-500:]
        if seen_attachment_ids is None:
            existing = self.get_case_watch_state(normalized_case)
            seen_attachment_ids = list(existing["seen_attachment_ids"]) if existing else []
        deduped_att_ids = list(dict.fromkeys(seen_attachment_ids))[-300:]
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO case_watch_state (
                        case_name, project_id, last_state, last_priority,
                        last_escalated, last_update_time, seen_comment_ids_json,
                        seen_attachment_ids_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(case_name) DO UPDATE SET
                        last_state = excluded.last_state,
                        last_priority = excluded.last_priority,
                        last_escalated = excluded.last_escalated,
                        last_update_time = excluded.last_update_time,
                        seen_comment_ids_json = excluded.seen_comment_ids_json,
                        seen_attachment_ids_json = excluded.seen_attachment_ids_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_case,
                        project_id,
                        state,
                        priority,
                        1 if escalated else 0,
                        update_time,
                        json.dumps(deduped_ids),
                        json.dumps(deduped_att_ids),
                        now,
                    ),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to save case watch state: %s", type(exc).__name__)
            raise RuntimeError("Database error saving case watch state.") from exc

    def record_case_watch_snapshot(self, case_obj: dict[str, Any]) -> None:
        """Seed or refresh `case_watch_state` directly from a hydrated `case` dictionary."""
        case_name = str(case_obj.get("name") or case_obj.get("case_name") or "").strip()
        if not case_name:
            return
        project_id = str(
            case_obj.get("project_id")
            or config.extract_project_id_from_case_name(case_name)
            or ""
        )
        existing_watch = self.get_case_watch_state(case_name)
        seen_ids = list(existing_watch["seen_comment_ids"]) if existing_watch else []
        seen_att_ids = list(existing_watch["seen_attachment_ids"]) if existing_watch else []
        for cmt in case_obj.get("comments") or []:
            cid = str(cmt.get("comment_id") or "").strip()
            if cid and cid not in seen_ids:
                seen_ids.append(cid)
        for att in case_obj.get("attachments") or []:
            aid = str(att.get("attachment_id") or "").strip()
            if aid and aid not in seen_att_ids:
                seen_att_ids.append(aid)
        self.save_case_watch_state(
            case_name=case_name,
            project_id=project_id,
            state=str(case_obj.get("state") or "OPEN"),
            priority=str(case_obj.get("priority") or "P3"),
            escalated=bool(case_obj.get("escalated", False)),
            update_time=str(case_obj.get("update_time") or _utc_now_iso()),
            seen_comment_ids=seen_ids,
            seen_attachment_ids=seen_att_ids,
        )

    # -------------------------------------------------------------------------
    # SRE / Ops Enhancements: Delegation, Remote Attachment Reader, Search & Shift Handover
    # -------------------------------------------------------------------------
    def is_draft_authorized_operator(
        self,
        draft_id: str,
        operator_open_id: str,
        admin_open_ids: set[str] | None = None,
    ) -> bool:
        """Return True if `operator_open_id` is owner, delegated co-owner, or configured SRE admin."""
        clean_op = (operator_open_id or "").strip()
        if not clean_op:
            return False
        if admin_open_ids and clean_op in admin_open_ids:
            return True
        owner = self.get_draft_owner(draft_id)
        if owner and clean_op == owner:
            return True
        draft = self.get_draft(draft_id)
        co_owners = draft.get("payload", {}).get("co_owners") or []
        return clean_op in co_owners

    def delegate_draft_owner(
        self,
        draft_id: str,
        delegate_open_ids: list[str],
        operator_open_id: str = "",
        admin_open_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Authorize one or more War Room colleagues (`ou_...`) to confirm/cancel a pending draft."""
        draft = self.get_draft(draft_id)
        if draft["status"] != "PENDING_CONFIRMATION":
            raise ValueError(
                f"Draft '{draft_id}' is in state '{draft['status']}' and cannot be delegated."
            )
        if operator_open_id and not self.is_draft_authorized_operator(
            draft_id, operator_open_id, admin_open_ids=admin_open_ids
        ):
            raise PermissionError("只有草稿发起人、已授权协同人或值班管理员可以转交草稿确认权。")

        payload = dict(draft["payload"])
        co_owners = list(payload.get("co_owners") or [])
        for oid in delegate_open_ids:
            clean_oid = (oid or "").strip()
            if clean_oid and clean_oid not in co_owners:
                co_owners.append(clean_oid)
        payload["co_owners"] = co_owners[:10]
        now = _utc_now_iso()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE drafts SET payload_json = ?, update_time = ? WHERE draft_id = ?",
                    (json.dumps(payload, ensure_ascii=False), now, draft["draft_id"]),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed delegating draft owner: %s", type(exc).__name__)
            raise RuntimeError("Database error updating draft co-owners.") from exc
        return self.get_draft(draft["draft_id"])

    def inspect_attachment(self, case_name: str, attachment_ref: str = "") -> dict[str, Any]:
        """Fetch and safely inspect an attachment (local or LIVE remote TSE upload) on a support case."""
        import hashlib
        import io
        import zipfile

        from app.security import (
            TEXT_DIAGNOSTIC_EXTENSIONS,
            redact_sensitive_secrets,
            resolve_stored_upload_path,
        )

        config.validate_project_access(case_name)
        normalized_case = config.normalize_case_name(case_name)
        case_obj = self.get_case(normalized_case)
        attachments = list(case_obj.get("attachments") or [])
        if not attachments:
            raise KeyError(f"Support case '{normalized_case}' has no attachments.")

        ref = (attachment_ref or "").strip()
        matched: dict[str, Any] | None = None
        if not ref:
            matched = attachments[-1]
        else:
            ref_lower = ref.lower()
            for att in attachments:
                aid = str(att.get("attachment_id") or "")
                fname = str(att.get("filename") or att.get("original_filename") or "")
                if ref == aid or ref_lower == fname.lower():
                    matched = att
                    break
            if matched is None:
                for att in attachments:
                    fname = str(att.get("filename") or att.get("original_filename") or "")
                    if ref_lower in fname.lower() or ref_lower in str(att.get("attachment_id") or "").lower():
                        matched = att
                        break
        if matched is None:
            available = ", ".join(
                f"{a.get('filename') or a.get('original_filename')} ({a.get('attachment_id')})"
                for a in attachments[:10]
            )
            raise KeyError(
                f"Attachment '{ref}' not found on {normalized_case}. Available: {available}"
            )

        att_id = str(matched.get("attachment_id") or "")
        filename = str(matched.get("filename") or matched.get("original_filename") or "attachment")
        mime_type = str(matched.get("mime_type") or "application/octet-stream")
        stored_fn = str(matched.get("stored_filename") or "").strip()
        if not stored_fn and att_id:
            try:
                with self._get_conn() as conn:
                    att_row = conn.execute(
                        "SELECT stored_filename FROM attachments WHERE attachment_id = ? AND case_name = ?",
                        (att_id, normalized_case),
                    ).fetchone()
                    if att_row and att_row["stored_filename"]:
                        stored_fn = str(att_row["stored_filename"]).strip()
            except sqlite3.Error:
                stored_fn = ""

        raw_bytes: bytes | None = None
        if stored_fn:
            try:
                raw_bytes = resolve_stored_upload_path(stored_fn).read_bytes()
            except Exception:
                raw_bytes = None
        if raw_bytes is None and config.get_support_mode() == "LIVE":
            raw_bytes = self.download_live_attachment_bytes(normalized_case, att_id)
        if raw_bytes is None:
            return {
                "case_name": normalized_case,
                "attachment_id": att_id,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": int(matched.get("size_bytes") or 0),
                "preview_available": False,
                "note": "该附件仅有元数据记录，本地未缓存原始字节。",
            }

        ext = Path(filename).suffix.lower()
        sha_short = hashlib.sha256(raw_bytes).hexdigest()[:16]
        if (
            ext in TEXT_DIAGNOSTIC_EXTENSIONS
            or ext in (".json", ".har")
            or mime_type.startswith("text/")
            or mime_type in ("application/json", "application/xml", "application/x-yaml")
        ):
            decoded = raw_bytes.decode("utf-8", errors="replace")
            sanitized, redacted = redact_sensitive_secrets(decoded)
            truncated = len(sanitized) > 6000
            return {
                "case_name": normalized_case,
                "attachment_id": att_id,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(raw_bytes),
                "sha256_prefix": sha_short,
                "preview_available": True,
                "truncated": truncated,
                "redacted_secrets": redacted,
                "content_preview": sanitized[:6000],
            }
        if ext == ".zip":
            entries: list[dict[str, Any]] = []
            try:
                with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                    for info in zf.infolist()[:50]:
                        entries.append({"name": info.filename, "size_bytes": info.file_size})
            except Exception:
                pass
            return {
                "case_name": normalized_case,
                "attachment_id": att_id,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(raw_bytes),
                "sha256_prefix": sha_short,
                "preview_available": True,
                "archive_entries": entries,
            }
        return {
            "case_name": normalized_case,
            "attachment_id": att_id,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": len(raw_bytes),
            "sha256_prefix": sha_short,
            "preview_available": False,
            "note": "二进制图片/PDF/压缩包已下载校验可读，可通过 Web 门户下载原件。",
        }

    def search_cases(
        self,
        query: str = "",
        project_id: str = "",
        state_filter: str = "ALL",
        severity_filter: str = "",
    ) -> list[dict[str, Any]]:
        """Search support cases across one or all ALLOWED_PROJECTS by keyword, state, and severity."""
        projects = (
            [config.validate_project_access(project_id)]
            if project_id and project_id.strip()
            else config.get_allowed_projects()
        )
        sev_norm = (
            PRIORITY_TO_SEVERITY.get(severity_filter.upper().strip(), "")
            if severity_filter and severity_filter.strip()
            else ""
        )
        q_clean = (query or "").strip().lower()
        q_tokens = [t for t in re.split(r"\s+", q_clean) if t]

        results: list[dict[str, Any]] = []
        for proj in projects:
            cases = self.list_cases(project_id=proj, state_filter=state_filter or "ALL")
            comment_map: dict[str, str] = {}
            if q_tokens:
                try:
                    with self._get_conn() as conn:
                        c_rows = conn.execute(
                            """
                            SELECT c.case_name, GROUP_CONCAT(c.body, ' ') AS all_bodies
                            FROM comments c
                            JOIN cases k ON k.case_name = c.case_name
                            WHERE k.project_id = ?
                            GROUP BY c.case_name
                            """,
                            (proj,),
                        ).fetchall()
                        comment_map = {r["case_name"]: str(r["all_bodies"] or "") for r in c_rows}
                except sqlite3.Error:
                    comment_map = {}

            for c in cases:
                if sev_norm and c.get("severity") != sev_norm:
                    continue
                if q_tokens:
                    cls_disp = str((c.get("classification") or {}).get("display_name") or "")
                    hay = (
                        f"{c.get('name', '')} {c.get('case_id', '')} "
                        f"{c.get('display_name', '')} {c.get('description', '')} "
                        f"{cls_disp} {comment_map.get(str(c.get('name') or ''), '')}"
                    ).lower()
                    if not all(tok in hay for tok in q_tokens):
                        continue
                results.append(c)

        results.sort(key=lambda item: str(item.get("update_time") or ""), reverse=True)
        return results[:50]

    def get_ops_shift_handover(self, project_id: str = "") -> dict[str, Any]:
        """Build an SRE On-Call Shift Handover & SLA Stale-Case Radar across authorized projects."""
        projects = (
            [config.validate_project_access(project_id)]
            if project_id and project_id.strip()
            else config.get_allowed_projects()
        )
        sla_hours_by_sev = {"S1": 1.0, "S2": 4.0, "S3": 24.0, "S4": 72.0}
        now_dt = datetime.datetime.now(datetime.timezone.utc)

        all_open: list[dict[str, Any]] = []
        for proj in projects:
            all_open.extend(self.list_cases(project_id=proj, state_filter="OPEN"))

        high_prio_or_escalated: list[dict[str, Any]] = []
        action_required: list[dict[str, Any]] = []
        solution_provided: list[dict[str, Any]] = []
        stale_cases: list[dict[str, Any]] = []

        for c in all_open:
            sev = str(c.get("severity") or "S3")
            state = str(c.get("state") or "OPEN")
            esc = bool(c.get("escalated", False))
            upd_str = str(c.get("update_time") or c.get("create_time") or "")
            hours_idle = 0.0
            if upd_str:
                try:
                    parsed_dt = datetime.datetime.fromisoformat(upd_str.replace("Z", "+00:00"))
                    if parsed_dt.tzinfo is None:
                        parsed_dt = parsed_dt.replace(tzinfo=datetime.timezone.utc)
                    hours_idle = max(0.0, round((now_dt - parsed_dt).total_seconds() / 3600.0, 1))
                except ValueError:
                    hours_idle = 0.0

            summary_item = {
                "case_name": c.get("name"),
                "case_id": c.get("case_id"),
                "project_id": c.get("project_id"),
                "display_name": c.get("display_name"),
                "severity": sev,
                "priority": c.get("priority"),
                "state": state,
                "escalated": esc,
                "hours_since_update": hours_idle,
                "update_time": upd_str,
            }

            if sev in ("S1", "S2") or esc:
                high_prio_or_escalated.append(summary_item)
            if state == "ACTION_REQUIRED":
                action_required.append(
                    {
                        **summary_item,
                        "recommended_action": "Google 工程师正在等待我方补充日志/复现信息，请尽快回复以免超时自动关单。",
                    }
                )
            elif state == "SOLUTION_PROVIDED":
                solution_provided.append(
                    {
                        **summary_item,
                        "recommended_action": "工程师已提供解决方案，请验证业务恢复情况：若未解决请评论打回，已解决请发起关单。",
                    }
                )

            threshold = sla_hours_by_sev.get(sev, 24.0)
            if hours_idle >= threshold:
                if state == "ACTION_REQUIRED":
                    rec = f"我方已滞留 {hours_idle}h 未反馈（阈值 {threshold}h），需当班运维立即补齐信息。"
                elif esc:
                    rec = f"已升级工单仍超 {hours_idle}h 未更新，建议立即追加高优催办评论并同步 TAM。"
                else:
                    rec = (
                        f"距上次更新已 {hours_idle}h（超过 {sev} 巡检阈值 {threshold}h），"
                        "建议追加评论催促进展或发起工单升级 (Escalation)。"
                    )
                stale_cases.append(
                    {
                        **summary_item,
                        "sla_threshold_hours": threshold,
                        "recommended_action": rec,
                    }
                )

        stale_cases.sort(key=lambda x: float(x.get("hours_since_update") or 0.0), reverse=True)
        return {
            "generated_at": _utc_now_iso(),
            "projects_scanned": projects,
            "total_open_cases": len(all_open),
            "high_priority_or_escalated": high_prio_or_escalated,
            "action_required": action_required,
            "solution_provided": solution_provided,
            "stale_cases": stale_cases,
        }


_support_client_instance: CloudSupportClient | None = None


def get_support_client() -> CloudSupportClient:
    """Get or initialize the singleton CloudSupportClient instance."""
    global _support_client_instance
    if _support_client_instance is None:
        _support_client_instance = CloudSupportClient()
    return _support_client_instance


def set_support_client(client: CloudSupportClient | None) -> None:
    """Override the singleton CloudSupportClient (useful for isolated unit tests)."""
    global _support_client_instance
    _support_client_instance = client
