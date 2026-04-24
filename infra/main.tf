terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  # Remote state in GCS — the state bucket must be created once before `terraform init`:
  #   gcloud storage buckets create gs://uc-and-d-tf-state --location=us-central1 --project=uc-and-d
  backend "gcs" {
    bucket = "uc-and-d-tf-state"
    prefix = "terraform/state"
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

# Enable required GCP APIs
resource "google_project_service" "apis" {
  for_each = toset([
    "sqladmin.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "servicenetworking.googleapis.com",
    "aiplatform.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}
