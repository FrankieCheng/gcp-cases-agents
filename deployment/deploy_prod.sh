#!/usr/bin/env bash
# Option A (single-project production) redeploy for cloud-case-agent.
#
# Deploy Cloud Case Management Agent to Cloud Run via gcloud.
#
# Prerequisites (already done once, see walkthrough):
#   - runtime SA cloud-case-agent-run@demo-core-prod with aiplatform.user,
#     cloudsupport.techSupportEditor, logging.logWriter, cloudtrace.agent
#   - Secret Manager secret SESSION_SECRET_KEY (runtime SA = secretAccessor)
#   - APIs: run, cloudbuild, secretmanager, aiplatform, iap, cloudsupport
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PROJECT="${PROJECT:-demo-core-prod}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-cloud-case-agent}"
RUNTIME_SA="${RUNTIME_SA:-cloud-case-agent-run@${PROJECT}.iam.gserviceaccount.com}"

# Optional Feishu bot: mount its secrets only if both exist in Secret Manager
# (runtime SA needs secretAccessor on them). Otherwise the bot stays disabled.
SECRETS="SESSION_SECRET_KEY=SESSION_SECRET_KEY:latest"
if gcloud secrets describe FEISHU_APP_ID --project "${PROJECT}" >/dev/null 2>&1 \
  && gcloud secrets describe FEISHU_APP_SECRET --project "${PROJECT}" >/dev/null 2>&1; then
  SECRETS="${SECRETS},FEISHU_APP_ID=FEISHU_APP_ID:latest,FEISHU_APP_SECRET=FEISHU_APP_SECRET:latest"
  echo "Feishu secrets found: enabling Feishu bot."
else
  echo "Feishu secrets not found: deploying without Feishu bot."
fi

FORCE_TEST_CASE="${FORCE_TEST_CASE:-true}"
CASE_POLL_INTERVAL_SECONDS="${CASE_POLL_INTERVAL_SECONDS:-60}"
CASE_AGENT_MODEL="${CASE_AGENT_MODEL:-gemini-4-hydrogen}"

gcloud beta run deploy "${SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" --source . \
  --memory 4Gi --cpu 1 --min-instances 1 --max-instances 1 --concurrency 8 \
  --no-allow-unauthenticated --no-cpu-throttling --iap \
  --service-account "${RUNTIME_SA}" \
  --update-env-vars "SUPPORT_API_MODE=LIVE,FORCE_TEST_CASE=${FORCE_TEST_CASE},CASE_POLL_INTERVAL_SECONDS=${CASE_POLL_INTERVAL_SECONDS},ALLOWED_PROJECTS=${PROJECT},CHAT_ENGINE=llm,CASE_AGENT_MODEL=${CASE_AGENT_MODEL},GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=true" \
  --update-secrets "${SECRETS}" \
  --labels created-by=agents-cli \
  --quiet
