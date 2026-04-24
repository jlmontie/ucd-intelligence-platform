locals {
  cloud_run_image_repo = "${var.region}-docker.pkg.dev/${var.project}/ucd"

  # Secrets injected as environment variables into both services
  shared_secrets = [
    {
      name   = "DATABASE_URL"
      secret = "DATABASE_URL"
    },
    {
      name   = "SECRET_KEY"
      secret = "SECRET_KEY"
    },
  ]
}

resource "google_cloud_run_v2_service" "api" {
  name     = "ucd-api"
  location = var.region

  template {
    service_account = google_service_account.app.email

    containers {
      image = var.api_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      dynamic "env" {
        for_each = local.shared_secrets
        content {
          name = env.value.name
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = "latest"
            }
          }
        }
      }

      env {
        name  = "GCS_BUCKET"
        value = google_storage_bucket.assets.name
      }

      # Vertex AI — litellm reads these to route Claude calls through the Model Garden
      env {
        name  = "VERTEXAI_PROJECT"
        value = var.project
      }

      env {
        name  = "VERTEXAI_LOCATION"
        value = var.vertex_location
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }

    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }
  }

  depends_on = [
    google_project_service.apis,
    google_service_account.app,
    google_project_iam_member.app_vertex,
    google_secret_manager_secret_version.database_url,
    google_secret_manager_secret_version.secret_key,
  ]
}

resource "google_cloud_run_v2_service" "frontend" {
  name     = "ucd-frontend"
  location = var.region

  template {
    service_account = google_service_account.app.email

    containers {
      image = var.frontend_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name  = "NEXT_PUBLIC_API_URL"
        value = google_cloud_run_v2_service.api.uri
      }

      dynamic "env" {
        for_each = [{ name = "SECRET_KEY", secret = "SECRET_KEY" }]
        content {
          name = env.value.name
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = "latest"
            }
          }
        }
      }
    }

    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_service.api,
  ]
}

# Allow unauthenticated public access to both services
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "frontend_public" {
  name     = google_cloud_run_v2_service.frontend.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
