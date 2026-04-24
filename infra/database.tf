resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_database_instance" "main" {
  name             = "ucd-db"
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    tier = var.db_tier

    backup_configuration {
      enabled    = true
      start_time = "03:00"
    }

    ip_configuration {
      # Cloud Run connects via the Cloud SQL Auth Proxy sidecar — no public IP needed.
      # For local developer access, enable and add your IP to authorized_networks,
      # or use the Cloud SQL Auth Proxy locally.
      ipv4_enabled = true
    }

    insights_config {
      query_insights_enabled = true
    }
  }

  deletion_protection = true

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "ucd" {
  name     = var.db_name
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "app" {
  name     = var.db_user
  instance = google_sql_database_instance.main.name
  password = random_password.db.result
}

# Store the full connection string in Secret Manager so Cloud Run can consume it
resource "google_secret_manager_secret" "database_url" {
  secret_id = "DATABASE_URL"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "database_url" {
  secret = google_secret_manager_secret.database_url.id
  # Cloud Run connects via the Auth Proxy unix socket
  secret_data = "postgresql://${var.db_user}:${random_password.db.result}@/${var.db_name}?host=/cloudsql/${var.project}:${var.region}:${google_sql_database_instance.main.name}"
}
