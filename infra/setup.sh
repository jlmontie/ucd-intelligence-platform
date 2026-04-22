#!/bin/bash
# One-time GCP project setup for the UCD Research Platform.
# Run once from a machine authenticated with gcloud.
#
# Usage: bash infra/setup.sh

set -euo pipefail

PROJECT="uc-and-d"
REGION="us-central1"
DB_INSTANCE="ucd-db"
DB_NAME="ucd_db"
DB_USER="ucd_user"
BUCKET="uc-and-d-assets"

echo "==> Setting project"
gcloud config set project "$PROJECT"

echo "==> Enabling APIs"
gcloud services enable \
  sqladmin.googleapis.com \
  run.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

echo "==> Creating GCS bucket"
gcloud storage buckets create "gs://$BUCKET" \
  --location="$REGION" \
  --uniform-bucket-level-access 2>/dev/null || echo "Bucket already exists"

echo "==> Creating Cloud SQL instance (PostgreSQL 16)"
gcloud sql instances create "$DB_INSTANCE" \
  --database-version=POSTGRES_16 \
  --tier=db-g1-small \
  --region="$REGION" \
  --storage-size=20GB \
  --storage-type=SSD \
  --backup-start-time=03:00 \
  --availability-type=zonal 2>/dev/null || echo "Instance already exists"

echo "==> Creating database and user"
gcloud sql databases create "$DB_NAME" --instance="$DB_INSTANCE" 2>/dev/null || true
DB_PASS=$(openssl rand -base64 24)
gcloud sql users create "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASS"

echo "==> Storing secrets in Secret Manager"
echo -n "postgresql://$DB_USER:$DB_PASS@localhost/$DB_NAME" | \
  gcloud secrets create DATABASE_URL --data-file=- 2>/dev/null || \
  echo -n "postgresql://$DB_USER:$DB_PASS@localhost/$DB_NAME" | \
  gcloud secrets versions add DATABASE_URL --data-file=-

echo ""
echo "==> Done. Next steps:"
echo "  1. Add ANTHROPIC_API_KEY to Secret Manager:"
echo "     echo -n 'sk-ant-...' | gcloud secrets create ANTHROPIC_API_KEY --data-file=-"
echo "  2. Run the Auth Proxy locally and apply the schema:"
echo "     cloud-sql-proxy $PROJECT:$REGION:$DB_INSTANCE &"
echo "     psql postgresql://$DB_USER:PASSWORD@localhost:5432/$DB_NAME -f db/schema.sql"
echo "  3. Run infra/deploy.sh to deploy Cloud Run services"
