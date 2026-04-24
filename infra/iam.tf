# Service account used by both Cloud Run services
resource "google_service_account" "app" {
  account_id   = "ucd-app"
  display_name = "UCD Application Service Account"
}

# Cloud SQL access
resource "google_project_iam_member" "app_sql" {
  project = var.project
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# GCS read/write for page images
resource "google_storage_bucket_iam_member" "app_storage" {
  bucket = google_storage_bucket.assets.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app.email}"
}

# Secret Manager access
resource "google_project_iam_member" "app_secrets" {
  project = var.project
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# Vertex AI — allows the service account to call Claude via the Model Garden
resource "google_project_iam_member" "app_vertex" {
  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# ── App secrets ───────────────────────────────────────────────────────────────

resource "google_secret_manager_secret" "secret_key" {
  secret_id = "SECRET_KEY"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "secret_key" {
  secret      = google_secret_manager_secret.secret_key.id
  secret_data = var.secret_key
}
