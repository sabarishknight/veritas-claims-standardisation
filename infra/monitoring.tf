# ---------------------------------------------------------------------------
# Observability (NFR-5.1)
#
# Metrics are derived from the structured logs the pipeline already emits, so
# there is no separate instrumentation path to keep in sync with the code.
#
# The ordering below is deliberate. An error-rate alert fires when something is
# obviously broken. The misalignment alert fires when everything looks fine and
# the data is quietly wrong - which is the failure this system exists to catch.
# ---------------------------------------------------------------------------

resource "google_logging_metric" "files_failed" {
  name   = "${var.name_prefix}/files_failed"
  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${google_cloud_run_v2_service.processor.name}"
    jsonPayload.message="run complete"
  EOT
  metric_descriptor {
    metric_kind = "GAUGE"
    value_type  = "INT64"
    unit        = "1"
  }
  value_extractor = "EXTRACT(jsonPayload.files_failed)"
}

resource "google_logging_metric" "error_rate" {
  name   = "${var.name_prefix}/error_rate"
  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${google_cloud_run_v2_service.processor.name}"
    jsonPayload.message="run complete"
  EOT
  metric_descriptor {
    metric_kind = "GAUGE"
    value_type  = "DOUBLE"
    unit        = "1"
  }
  value_extractor = "EXTRACT(jsonPayload.error_rate)"
}

resource "google_logging_metric" "misalignment_detected" {
  name   = "${var.name_prefix}/misalignment_detected"
  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${google_cloud_run_v2_service.processor.name}"
    jsonPayload.message="report misalignment suspected"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  label_extractors = {
    source_system = "EXTRACT(jsonPayload.source_system)"
  }
}

resource "google_logging_metric" "dead_letters" {
  name   = "${var.name_prefix}/dead_letters"
  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${google_cloud_run_v2_service.processor.name}"
    jsonPayload.message="payload written to dead-letter store"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  label_extractors = {
    reason = "EXTRACT(jsonPayload.reason)"
  }
}

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  display_name = "Veritas on-call"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

locals {
  channels = var.alert_email == "" ? [] : [google_monitoring_notification_channel.email[0].id]
}

# NFR-5.1: alert when the error rate exceeds 1%.
resource "google_monitoring_alert_policy" "error_rate" {
  display_name = "Veritas: file error rate above 1%"
  combiner     = "OR"
  notification_channels = local.channels

  conditions {
    display_name = "error_rate > 0.01 over 5 minutes"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.error_rate.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.01
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  documentation {
    content = <<-EOT
      More than 1% of files failed to process.

      Triage:
        1. Ops UI -> Dead letters, group by reason.
        2. A single dominant reason means one clinic's feed changed.
           Spread across reasons means a platform problem.
        3. Replay after the fix:
             veritas run gs://BUCKET/_deadletter/<reason>/<date>/
           Replay is idempotent - identity is the content hash.
    EOT
  }
}

# NFR-1.2: alert when processing lag threatens the 15-minute SLO.
resource "google_monitoring_alert_policy" "processing_lag" {
  display_name = "Veritas: processing lag above the 15-minute SLO"
  combiner     = "OR"
  notification_channels = local.channels

  conditions {
    display_name = "request latency p95 > 900s"
    condition_threshold {
      filter          = <<-EOT
        metric.type="run.googleapis.com/request_latencies"
        resource.type="cloud_run_revision"
        resource.label.service_name="${google_cloud_run_v2_service.processor.name}"
      EOT
      comparison      = "COMPARISON_GT"
      threshold_value = 900000
      duration        = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
      }
    }
  }
}

# The alert that matters most: data that looks fine and is wrong.
resource "google_monitoring_alert_policy" "misalignment" {
  display_name = "Veritas: OCR column misalignment detected"
  combiner     = "OR"
  notification_channels = local.channels

  conditions {
    display_name = "any report quarantined for suspected misalignment"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.misalignment_detected.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
        group_by_fields    = ["metric.label.source_system"]
      }
    }
  }

  documentation {
    content = <<-EOT
      A report's test names do not describe the values printed beside them -
      the signature of an OCR row drop upstream.

      This does NOT mean data was corrupted: the affected rows are quarantined
      and withheld from analytics. It means a clinic's document pipeline is
      producing unusable reports and will keep doing so until someone tells
      them.

      Triage:
        1. Ops UI -> Dashboard, read the proposed offset and the evidence.
        2. Confirm the reading with a clinical reviewer.
        3. Accept the offset in the review queue, or raise it with the clinic.

      Never apply a realignment without human confirmation: a wrong
      realignment writes a plausible-looking wrong value into a claim
      decision, and it is undetectable downstream.
    EOT
  }
}

resource "google_monitoring_alert_policy" "dead_letter_spike" {
  display_name = "Veritas: dead-letter spike"
  combiner     = "OR"
  notification_channels = local.channels

  conditions {
    display_name = "dead letters above baseline"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.dead_letters.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 100
      duration        = "600s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
        group_by_fields    = ["metric.label.reason"]
      }
    }
  }
}
