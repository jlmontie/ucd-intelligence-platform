#!/bin/bash
# Deploy API and frontend Cloud Run services.
#
# Usage:
#   bash infra/deploy.sh          # deploy both
#   bash infra/deploy.sh api      # deploy API only
#   bash infra/deploy.sh frontend # deploy frontend only

set -euo pipefail

PROJECT="uc-and-d"
REGION="us-central1"
DB_INSTANCE="ucd-db"
REPO="us-central1-docker.pkg.dev/$PROJECT/ucd"

TARGET="${1:-all}"

build_and_push() {
  local name=$1
  local dir=$2
  echo "==> Building $name"
  gcloud builds submit "$dir" \
    --tag "$REPO/$name:latest" \
    --project "$PROJECT"
}

deploy_service() {
  local name=$1
  local image="$REPO/$name:latest"
  echo "==> Deploying $name to Cloud Run"
  gcloud run deploy "$name" \
    --image "$image" \
    --region "$REGION" \
    --platform managed \
    --allow-unauthenticated \
    --add-cloudsql-instances "$PROJECT:$REGION:$DB_INSTANCE" \
    --set-secrets "DATABASE_URL=DATABASE_URL:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,SECRET_KEY=SECRET_KEY:latest" \
    --min-instances 0 \
    --max-instances 10 \
    --memory 512Mi \
    --project "$PROJECT"
}

# Ensure Artifact Registry repo exists
gcloud artifacts repositories create ucd \
  --repository-format=docker \
  --location="$REGION" \
  --project="$PROJECT" 2>/dev/null || true

if [[ "$TARGET" == "all" || "$TARGET" == "api" ]]; then
  build_and_push "ucd-api" "api"
  deploy_service "ucd-api"
fi

if [[ "$TARGET" == "all" || "$TARGET" == "frontend" ]]; then
  build_and_push "ucd-frontend" "frontend"
  deploy_service "ucd-frontend"
fi

echo ""
echo "==> Deployed. Service URLs:"
gcloud run services list --region "$REGION" --project "$PROJECT" \
  --filter="metadata.name:ucd-" --format="table(metadata.name, status.url)"
