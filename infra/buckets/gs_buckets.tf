resource google_storage_bucket dss_gs_bucket {
  count = "${length(var.DSS_GS_BUCKET) > 0 ? 1 : 0}"
  name = "${var.DSS_GS_BUCKET}"
  provider = "google"
  location = "${length(var.DSS_GS_BUCKET_REGION) > 0 ?
    "${var.DSS_GS_BUCKET_REGION}" : "${var.GCP_DEFAULT_REGION}"}"
  storage_class = "REGIONAL"
}

resource google_storage_bucket dss_gs_bucket_test {
  count = "${length(var.DSS_GS_BUCKET_TEST) > 0 ? 1 : 0}"
  name = "${var.DSS_GS_BUCKET_TEST}"
  provider = "google"
  location = "${length(var.DSS_GS_BUCKET_TEST_REGION) > 0 ?
    "${var.DSS_GS_BUCKET_TEST_REGION}" : "${var.GCP_DEFAULT_REGION}"}"
  storage_class = "REGIONAL"
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 7
      is_live = true
    }
  }
}

resource google_storage_bucket dss_gs_bucket_test_fixtures {
  count = "${length(var.DSS_GS_BUCKET_TEST_FIXTURES) > 0 ? 1 : 0}"
  name = "${var.DSS_GS_BUCKET_TEST_FIXTURES}"
  provider = "google"
  location = "${length(var.DSS_GS_BUCKET_TEST_FIXTURES_REGION) > 0 ?
    "${var.DSS_GS_BUCKET_TEST_FIXTURES_REGION}" : "${var.GCP_DEFAULT_REGION}"}"
  storage_class = "REGIONAL"
}

resource google_storage_bucket dss_gs_checkout_bucket {
  count = "${length(var.DSS_GS_CHECKOUT_BUCKET) > 0 ? 1 : 0}"
  name = "${var.DSS_GS_CHECKOUT_BUCKET}"
  provider = "google"
  location = "${var.GCP_DEFAULT_REGION}"
  storage_class = "REGIONAL"
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 30
      is_live = true
    }
  }
}

resource google_storage_bucket dss_gs_checkout_bucket_test {
  count = "${length(var.DSS_GS_CHECKOUT_BUCKET_TEST) > 0 ? 1 : 0}"
  name = "${var.DSS_GS_CHECKOUT_BUCKET_TEST}"
  provider = "google"
  location = "${var.GCP_DEFAULT_REGION}"
  storage_class = "REGIONAL"
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 7
      is_live = true
    }
  }
}
