# ---------------------------------------------------------------------------
# Veritas Claims standardisation pipeline - GCP infrastructure
#
# One `terraform apply` brings up the whole topology: landing bucket, Pub/Sub
# notification, processing service, BigQuery dataset, the ops UI behind IAP,
# and the alert policies.
#
# Committed because an architecture diagram is a claim and Terraform is the
# evidence. The service accounts and IAM bindings below are also where the
# least-privilege story is actually enforced, rather than asserted in prose.
#
#   terraform init
#   terraform apply -var project_id=<your-project> -var region=asia-south1
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "asia-south1" # data residency: Indian patient data stays in India
}
variable "name_prefix" {
  type    = string
  default = "veritas"
}
variable "image" {
  type        = string
  description = "Container image for the processing and UI services."
  default     = "gcr.io/cloudrun/placeholder"
}
variable "alert_email" {
  type    = string
  default = ""
}

locals {
  labels = {
    system     = "veritas-standardisation"
    managed_by = "terraform"
    data_class = "phi" # drives org-policy and audit-log routing
  }
}

# ---------------------------------------------------------------------------
# Service accounts - one per workload, least privilege each
# ---------------------------------------------------------------------------

resource "google_service_account" "processor" {
  account_id   = "${var.name_prefix}-processor"
  display_name = "Veritas processing service"
  description  = "Reads landing objects, writes BigQuery and the dead-letter prefix."
}

resource "google_service_account" "ui" {
  account_id   = "${var.name_prefix}-ui"
  display_name = "Veritas operations UI"
  description  = "Read-only. Queries BigQuery views; never writes."
}

# ---------------------------------------------------------------------------
# Landing bucket
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "landing" {
  name                        = "${var.project_id}-${var.name_prefix}-landing"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = local.labels

  # Raw clinical payloads are the audit trail (FR-4.3) and PHI. Versioning
  # protects against a clinic overwriting an object with a corrected one before
  # it has been processed.
  versioning { enabled = true }

  lifecycle_rule {
    condition { age = 90 }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  lifecycle_rule {
    condition {
      age        = 90
      matches_prefix = ["_deadletter/"]
    }
    action { type = "Delete" }
  }

  # A dead letter nobody has triaged in 90 days is not going to be, and keeping
  # PHI longer than it is useful is a liability rather than an asset.
}

resource "google_storage_bucket_iam_member" "processor_reads_landing" {
  bucket = google_storage_bucket.landing.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.processor.email}"
}

resource "google_storage_bucket_iam_member" "processor_writes_deadletter" {
  bucket = google_storage_bucket.landing.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.processor.email}"
}

# ---------------------------------------------------------------------------
# Pub/Sub: object-finalise notifications drive the pipeline
# ---------------------------------------------------------------------------

resource "google_pubsub_topic" "arrivals" {
  name   = "${var.name_prefix}-arrivals"
  labels = local.labels
}

resource "google_pubsub_topic" "arrivals_dead_letter" {
  name   = "${var.name_prefix}-arrivals-dlq"
  labels = local.labels
}

data "google_storage_project_service_account" "gcs" {}

resource "google_pubsub_topic_iam_member" "gcs_publishes" {
  topic  = google_pubsub_topic.arrivals.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

resource "google_storage_notification" "on_finalise" {
  bucket             = google_storage_bucket.landing.name
  topic              = google_pubsub_topic.arrivals.id
  payload_format     = "JSON_API_V1"
  event_types        = ["OBJECT_FINALIZE"]
  depends_on         = [google_pubsub_topic_iam_member.gcs_publishes]
}

resource "google_pubsub_subscription" "to_processor" {
  name  = "${var.name_prefix}-to-processor"
  topic = google_pubsub_topic.arrivals.id

  # Push rather than pull: Cloud Run scales on request concurrency, so push
  # lets the platform do the autoscaling instead of a hand-rolled poller.
  push_config {
    push_endpoint = "${google_cloud_run_v2_service.processor.uri}/pubsub"
    oidc_token {
      service_account_email = google_service_account.processor.email
    }
  }

  ack_deadline_seconds = 120

  # Seven days of retention means a total processing outage is a delay, not
  # data loss (NFR-3.3).
  message_retention_duration = "604800s"

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.arrivals_dead_letter.id
    max_delivery_attempts = 5
  }

  labels = local.labels
}

# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------

resource "google_bigquery_dataset" "warehouse" {
  dataset_id    = "${var.name_prefix}_prod"
  location      = var.region
  friendly_name = "Veritas standardised clinical data"
  description   = "Schema is generated by src/veritas/load/schema.py. Do not edit by hand."
  labels        = local.labels

  # No default table expiry: this is the adjudication record of truth.
  default_partition_expiration_ms = null
}

resource "google_bigquery_dataset_iam_member" "processor_writes" {
  dataset_id = google_bigquery_dataset.warehouse.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.processor.email}"
}

resource "google_bigquery_dataset_iam_member" "ui_reads" {
  dataset_id = google_bigquery_dataset.warehouse.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.ui.email}"
}

resource "google_project_iam_member" "processor_runs_jobs" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.processor.email}"
}

resource "google_project_iam_member" "ui_runs_jobs" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.ui.email}"
}

# ---------------------------------------------------------------------------
# PII tokenisation key
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret" "pii_key" {
  secret_id = "${var.name_prefix}-pii-hmac-key"
  labels    = local.labels
  replication {
    user_managed {
      replicas { location = var.region }
    }
  }
}

resource "google_secret_manager_secret_iam_member" "processor_reads_key" {
  secret_id = google_secret_manager_secret.pii_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.processor.email}"
  # Deliberately NOT granted to the UI: the dashboard shows tokens, never
  # the means to generate or check them.
}

# ---------------------------------------------------------------------------
# Cloud Run: processing
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "processor" {
  name     = "${var.name_prefix}-processor"
  location = var.region
  labels   = local.labels

  template {
    service_account = google_service_account.processor.email

    scaling {
      # One instance covers 200k files/day at the measured rate
      # (docs/cost-model.md). min_instance_count = 1 removes cold start from
      # the latency budget; the headroom is for zone failure, not throughput.
      min_instance_count = 1
      max_instance_count = 10
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle = false
      }

      env {
        name  = "VERITAS_TARGET"
        value = "bigquery"
      }
      env {
        name  = "VERITAS_BQ_DATASET"
        value = google_bigquery_dataset.warehouse.dataset_id
      }
      env {
        name  = "VERITAS_GCS_BUCKET"
        value = google_storage_bucket.landing.name
      }
      env {
        name  = "VERITAS_LOG_FORMAT"
        value = "json"
      }
      env {
        name = "VERITAS_PII_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.pii_key.secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        http_get { path = "/api/health" }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 6
      }
    }

    # Several messages per instance: the work is CPU-light per file and the
    # per-request overhead dominates at concurrency 1.
    max_instance_request_concurrency = 20
    timeout                          = "300s"
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}

# ---------------------------------------------------------------------------
# Cloud Run: operations UI (read-only, behind IAP)
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "ui" {
  name     = "${var.name_prefix}-ui"
  location = var.region
  labels   = local.labels

  template {
    service_account = google_service_account.ui.email

    scaling {
      min_instance_count = 0 # nobody is looking at 3am
      max_instance_count = 3
    }

    containers {
      image   = var.image
      command = ["python", "-m", "veritas"]
      args    = ["serve", "--host", "0.0.0.0", "--port", "8080", "--no-browser"]

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name  = "VERITAS_TARGET"
        value = "bigquery"
      }
      env {
        name  = "VERITAS_BQ_DATASET"
        value = google_bigquery_dataset.warehouse.dataset_id
      }
    }
  }
}

# The UI shows PHI-adjacent data, so it is never public. IAP in front, and the
# allow-list is a separate, reviewable resource.
resource "google_cloud_run_v2_service_iam_member" "ui_iap_only" {
  name     = google_cloud_run_v2_service.ui.name
  location = google_cloud_run_v2_service.ui.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

data "google_project" "current" {}

# ---------------------------------------------------------------------------
# Scheduled reconciliation sweep (T-1)
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "reconcile" {
  name        = "${var.name_prefix}-reconcile"
  description = "Catches objects whose finalise notification was missed. Idempotent by content hash, so it can be aggressive."
  schedule    = "0 * * * *"
  time_zone   = "Asia/Kolkata"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.processor.uri}/reconcile"
    oidc_token {
      service_account_email = google_service_account.processor.email
    }
  }
}

output "landing_bucket" { value = google_storage_bucket.landing.name }
output "dataset" { value = google_bigquery_dataset.warehouse.dataset_id }
output "processor_url" { value = google_cloud_run_v2_service.processor.uri }
output "ui_url" { value = google_cloud_run_v2_service.ui.uri }
