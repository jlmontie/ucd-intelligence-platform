#!/bin/bash
# Build and push container images to Artifact Registry.
# Terraform manages all infrastructure; this script handles image builds only.
#
# Usage:
#   bash infra/build.sh          # build both
#   bash infra/build.sh api      # build API only
#   bash infra/build.sh frontend # build frontend only

set -euo pipefail

PROJECT="uc-and-d"
REGION="us-central1"
REPO="$REGION-docker.pkg.dev/$PROJECT/ucd"
TARGET="${1:-all}"

gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet

if [[ "$TARGET" == "all" || "$TARGET" == "api" ]]; then
  echo "==> Building ucd-api"
  gcloud builds submit api/ --tag "$REPO/ucd-api:latest" --project "$PROJECT"
  echo "    Image: $REPO/ucd-api:latest"
fi

if [[ "$TARGET" == "all" || "$TARGET" == "frontend" ]]; then
  echo "==> Building ucd-frontend"
  gcloud builds submit frontend/ --tag "$REPO/ucd-frontend:latest" --project "$PROJECT"
  echo "    Image: $REPO/ucd-frontend:latest"
fi

echo ""
echo "==> Update infra/terraform.tfvars with the new image tags, then run:"
echo "    cd infra && terraform apply"
