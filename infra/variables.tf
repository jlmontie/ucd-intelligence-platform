variable "project" {
  description = "GCP project ID"
  type        = string
  default     = "uc-and-d"
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "db_tier" {
  description = "Cloud SQL machine tier"
  type        = string
  default     = "db-g1-small"  # upgrade to db-n1-standard-1 before launch
}

variable "db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "ucd_db"
}

variable "db_user" {
  description = "PostgreSQL user"
  type        = string
  default     = "ucd_user"
}

variable "api_image" {
  description = "Container image URI for the API Cloud Run service"
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"  # placeholder until first build
}

variable "frontend_image" {
  description = "Container image URI for the frontend Cloud Run service"
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"  # placeholder until first build
}

variable "vertex_location" {
  description = "Vertex AI region for Claude Model Garden — must support Claude models"
  type        = string
  default     = "us-east5"  # primary US region for Claude on Vertex AI
}

variable "secret_key" {
  description = "Random secret for signing JWTs — generate with: openssl rand -base64 32"
  type        = string
  sensitive   = true
}
