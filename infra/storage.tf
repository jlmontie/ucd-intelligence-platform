resource "google_storage_bucket" "assets" {
  name                        = "uc-and-d-assets"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  lifecycle_rule {
    condition {
      age = 365
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  depends_on = [google_project_service.apis]
}

# Page images are public-readable so the frontend can serve them as citation previews
resource "google_storage_bucket_iam_member" "public_images" {
  bucket = google_storage_bucket.assets.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

resource "google_artifact_registry_repository" "ucd" {
  repository_id = "ucd"
  location      = var.region
  format        = "DOCKER"
  description   = "UCD platform container images"

  depends_on = [google_project_service.apis]
}
