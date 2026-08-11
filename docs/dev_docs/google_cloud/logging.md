# Cloud Logging

Validibot uses Google Cloud Logging for centralized log management in production. Cloud Run automatically captures stdout/stderr and sends it to Cloud Logging, where logs are indexed, searchable, and retained for 30 days by default.

## How It Works

When Django runs on Cloud Run, all output to stdout is captured by Cloud Logging. We use structured JSON logging so that individual fields (severity, module, message, etc.) become queryable in the Cloud Console.

The flow:

```
Django logger → JSON formatter → stdout → Cloud Run → Cloud Logging
```

Without JSON formatting, logs appear as plain text strings. With JSON formatting, Cloud Logging parses the JSON and indexes each field, enabling powerful filtering.

## What We Log

### Automatic Logs

Cloud Run automatically logs:

- **Request logs**: Every HTTP request with status code, latency, URL
- **Container lifecycle**: Startup, shutdown, cold starts
- **System errors**: OOM, crashes, timeouts

### Application Logs

Our Django app logs:

- **User actions**: Login, logout, organization changes
- **Validation runs**: Creation, status changes, completion
- **API requests**: Authenticated API calls
- **Errors**: Exceptions with stack traces (also sent to Sentry)

## Viewing Logs

### Cloud Console

The easiest way to view logs is the Cloud Console:

1. Go to [Cloud Logging](https://console.cloud.google.com/logs)
2. Select your project
3. Use the query editor to filter

### CLI Quick Commands

```bash
# Recent logs from web service
just gcp logs

# Or directly with gcloud
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$GCP_APP_NAME" --limit=50

# Logs from worker service
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$GCP_APP_NAME-worker" --limit=50
```

## Searching Logs

Cloud Logging uses a query language to filter logs. Here are common queries:

### By Severity

```
severity>=ERROR
severity=WARNING
```

### By Service

```
resource.labels.service_name="$GCP_APP_NAME"
resource.labels.service_name="$GCP_APP_NAME-worker"
```

### By Module/Logger

```
jsonPayload.name="validibot.validations"
jsonPayload.module="views"
```

### By Message Content

```
jsonPayload.message:"ValidationRun"
jsonPayload.message:"failed"
```

### By Time Range

```
timestamp>="2025-12-01T00:00:00Z"
timestamp>="2025-12-01T00:00:00Z" timestamp<="2025-12-02T00:00:00Z"
```

### Combined Queries

Find validation errors in the last hour:

```
resource.labels.service_name="$GCP_APP_NAME-worker"
severity>=ERROR
jsonPayload.name:"validations"
timestamp>="-1h"
```

Find requests for a specific validation run:

```
jsonPayload.message:"run_id=abc123"
```

## Structured Logging in Code

When logging in application code, include structured data for better searchability:

```python
import logging

logger = logging.getLogger(__name__)

# Basic logging
logger.info("Validation started")

# With extra fields (become queryable in Cloud Logging)
logger.info(
    "Validation started",
    extra={
        "run_id": str(run.id),
        "workflow_id": str(run.workflow_id),
        "user_id": str(run.user_id),
    },
)
```

The `extra` fields are merged into the JSON output and become searchable:

```
jsonPayload.run_id="abc-123"
```

## Log Levels

| Level | Usage |
|-------|-------|
| `DEBUG` | Detailed diagnostic info (disabled in production) |
| `INFO` | Normal operations, milestones |
| `WARNING` | Unexpected but handled conditions |
| `ERROR` | Errors that need attention |
| `CRITICAL` | System failures |

In production, the root logger is set to `INFO`. Individual loggers can be adjusted in `production.py`.

## Retention and Costs

- **Default retention**: 30 days
- **Cost**: Cloud Logging has a generous free tier (50 GB/month)
- **Export**: Logs can be exported to Cloud Storage or BigQuery for longer retention

## Debugging a Failed Validation

When a validation fails, here's how to investigate:

1. **Find the error in Cloud Logging**:
   ```
   resource.labels.service_name="$GCP_APP_NAME-worker"
   severity>=ERROR
   timestamp>="-1h"
   ```

2. **Get the run_id from the error**

3. **Find all logs for that run**:
   ```
   jsonPayload.message:"run_id=<the-run-id>"
   ```

4. **Check the pinned validator runtime logs**. Request-shaped attempts use a
   release-specific Service; over-budget/rollback attempts use retained Jobs:
   ```
   resource.type="cloud_run_revision"
   resource.labels.service_name="validibot-validator-service-energyplus-v0-15-0"

   # Retained Job route:
   resource.type="cloud_run_job"
   resource.labels.job_name="$GCP_APP_NAME-validator-backend-energyplus"
   ```

   The job name follows the `$GCP_APP_NAME-validator-backend-<slug>`
   convention (May 2026 standardisation; see
   `ValidatorConfig.cloud_run_job_name`). For FMU substitute
   `validator-backend-fmu`; for any new validator, use the slug
   from its `validators/<slug>/config.py`.

## Related

- [Deployment Guide](deployment.md)
- [Google Cloud Overview](index.md)
