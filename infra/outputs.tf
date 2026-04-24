output "api_url" {
  description = "Public URL of the API Cloud Run service"
  value       = google_cloud_run_v2_service.api.uri
}

output "frontend_url" {
  description = "Public URL of the frontend Cloud Run service"
  value       = google_cloud_run_v2_service.frontend.uri
}

output "cloud_sql_connection_name" {
  description = "Used in Cloud SQL Auth Proxy: --instances=<this value>"
  value       = google_sql_database_instance.main.connection_name
}

output "artifact_registry_repo" {
  description = "Docker image prefix for builds"
  value       = "${var.region}-docker.pkg.dev/${var.project}/ucd"
}

output "assets_bucket" {
  description = "GCS bucket for PDFs and page images"
  value       = google_storage_bucket.assets.name
}
