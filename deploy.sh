#!/usr/bin/env bash
#
# Deploy the Google Workspace MCP (fork) to Cloud Run, consistent with the rest
# of the AW MCP fleet: project/region pinned, OAuth client secret in Secret
# Manager, non-secret config as plain env vars, Firestore-backed session
# persistence, audit logging, email allowlist, and the 5-day/30-day re-auth
# policy all enabled.
#
# Usage:
#   ./deploy.sh              Deploy to the live revision (serves traffic).
#   ./deploy.sh --tag dev    Deploy a preview revision tagged "dev" with NO
#                            traffic (safe verification before promotion).
#
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-idyllic-kiln-489511-t4}"
PROJECT_NUMBER="${PROJECT_NUMBER:-420082496003}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-workspace-mcp}"

# --- Preview (--tag dev) support: build a no-traffic tagged revision ----------
DEPLOY_TAG=""
NO_TRAFFIC_ARGS=()
if [[ "${1:-}" == "--tag" && "${2:-}" != "" ]]; then
  DEPLOY_TAG="$2"
  NO_TRAFFIC_ARGS=(--no-traffic --tag "$DEPLOY_TAG")
  echo "== Preview deploy: revision tag '$DEPLOY_TAG' with NO traffic =="
fi

# Load local (gitignored) config.
set -a; source ./.env; set +a

echo "== Enabling required APIs =="
gcloud services enable secretmanager.googleapis.com firestore.googleapis.com \
  --project "$PROJECT_ID"

# This script deliberately does NOT create or update secret versions. Deploying
# and rotating are separate jobs: a deploy that also writes the secret it is
# about to mount will serve an unreviewed value, which is how a bad value
# reaches production without anyone approving it. Provision and rotate
# google-oauth-client-secret out of band, then set OAUTH_SECRET_VERSION below.

# ALLOWED_EMAILS is a comma-separated list, so use gcloud's custom-delimiter
# syntax (^##^) to avoid gcloud splitting the value on commas.
ENV_VARS="^##^MCP_ENABLE_OAUTH21=true"
ENV_VARS+="##SERVICE_NAME=${SERVICE}"
ENV_VARS+="##ALLOWED_EMAILS=${ALLOWED_EMAILS}"
ENV_VARS+="##GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID}"
ENV_VARS+="##WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND=firestore"
ENV_VARS+="##WORKSPACE_MCP_OAUTH_PROXY_FIRESTORE_COLLECTION=workspace_mcp_oauth_proxy"
ENV_VARS+="##WORKSPACE_MCP_AW_STORE_BACKEND=firestore"
ENV_VARS+="##WORKSPACE_MCP_AW_REAUTH_COLLECTION=aw_reauth_policy"
ENV_VARS+="##REAUTH_INACTIVITY_DAYS=${REAUTH_INACTIVITY_DAYS:-5}"
ENV_VARS+="##REAUTH_MAX_DAYS=${REAUTH_MAX_DAYS:-30}"
ENV_VARS+="##WORKSPACE_MCP_BRAND=on"
ENV_VARS+="##WORKSPACE_MCP_BRAND_VERIFIED_DOMAIN=${WORKSPACE_MCP_BRAND_VERIFIED_DOMAIN:-claude.ai}"
ENV_VARS+="##WORKSPACE_MCP_BRAND_HELP_URL=${WORKSPACE_MCP_BRAND_HELP_URL:-https://arachnidworks.com/mcp-help}"
# Blast-radius limits from the fleet review (section 7): restrict tools/scopes.
ENV_VARS+="##TOOLS=${TOOLS:-gmail calendar drive docs sheets}"
ENV_VARS+="##TOOL_TIER=${TOOL_TIER:-core}"

# Pinned to an explicit numeric version, never a floating alias. An alias means a
# new secret version changes what production serves with no deploy having
# happened, which is exactly the outage this fleet already had once.
SECRETS="GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:${OAUTH_SECRET_VERSION:-29}"

echo "== Deploying to Cloud Run =="
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --allow-unauthenticated \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "$SECRETS" \
  "${NO_TRAFFIC_ARGS[@]}"

if [[ -n "$DEPLOY_TAG" ]]; then
  TAG_URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --format="value(status.traffic.url)" --filter="status.traffic.tag=${DEPLOY_TAG}" 2>/dev/null | head -1)
  echo ""
  echo "Preview revision deployed (no traffic). Tagged URL: ${TAG_URL:-<see console>}"
  echo "NEXT: add ${TAG_URL:-<tag-url>}/oauth2callback as an authorized redirect URI on the"
  echo "GOOGLE_OAUTH client, set WORKSPACE_EXTERNAL_URL on the dev revision, then verify at the tagged URL."
  exit 0
fi

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format="value(status.url)")
echo "== Setting WORKSPACE_EXTERNAL_URL=$URL =="
gcloud run services update "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --update-env-vars "WORKSPACE_EXTERNAL_URL=${URL}" >/dev/null

echo ""
echo "Deployed: $URL"
echo "MCP endpoint: ${URL}/mcp"
echo "Health: curl ${URL}/health"
echo ""
echo "NEXT: add ${URL}/oauth2callback as an authorized redirect URI on the GOOGLE_OAUTH client."
