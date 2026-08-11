# Cloud Scheduler - Scheduled Jobs (GCP)

> **Note**: This document covers GCP Cloud Scheduler for production deployments.
> For Docker Compose deployments, see [configure-scheduled-tasks.md](../how-to/configure-scheduled-tasks.md) which uses Celery + Celery Beat instead.

Cloud Scheduler runs periodic tasks in GCP deployments. Each job sends an HTTP POST request to an endpoint on the worker service, authenticated via OIDC.

## Architecture

```
┌──────────────────┐     OIDC Token      ┌──────────────────┐
│ Cloud Scheduler  │ ──────────────────► │ Worker Service   │
│ (cron triggers)  │     POST request    │ (Cloud Run)      │
└──────────────────┘                     └──────────────────┘
                                                  │
                                                  ▼
                                         ┌──────────────────┐
                                         │ Django Command   │
                                         │ (cleanup, etc)   │
                                         └──────────────────┘
```

**Authentication**: Cloud Scheduler uses OIDC tokens signed by a service account. Cloud Run automatically verifies these tokens - no application-level auth is needed.

## Scheduled Jobs

Each stage has its own set of scheduler jobs with stage-specific names:

| Job Name (prod) | Schedule (Sydney) | Endpoint | Purpose |
|-----------------|-------------------|----------|---------|
| `$GCP_APP_NAME-clear-sessions` | Daily at 2:00 AM | `/api/v1/scheduled/clear-sessions/` | Remove expired Django sessions from the database |
| `$GCP_APP_NAME-cleanup-idempotency-keys` | Daily at 3:00 AM | `/api/v1/scheduled/cleanup-idempotency-keys/` | Delete expired API idempotency keys (24h TTL) |
| `$GCP_APP_NAME-cleanup-callback-receipts` | Weekly Sunday 4:00 AM | `/api/v1/scheduled/cleanup-callback-receipts/` | Delete old validator callback receipts (30 day retention) |
| `$GCP_APP_NAME-purge-expired-submissions` | Hourly at :00 | `/api/v1/scheduled/purge-expired-submissions/` | Purge submission content past retention period |
| `$GCP_APP_NAME-purge-expired-outputs` | Hourly at :00 | `/api/v1/scheduled/purge-expired-outputs/` | Purge validation outputs past retention period |
| `$GCP_APP_NAME-process-purge-retries` | Every 5 minutes | `/api/v1/scheduled/process-purge-retries/` | Retry failed submission purges |
| `$GCP_APP_NAME-cleanup-stuck-runs` | Every 10 minutes | `/api/v1/scheduled/cleanup-stuck-runs/` | Mark validation runs stuck in RUNNING state as FAILED (30min timeout) |

For dev/staging, job names include the stage suffix (e.g., `$GCP_APP_NAME-clear-sessions-dev`).

## Setup

### Prerequisites

1. **Worker service deployed**: The worker service must be running and accessible.
2. **Cloud Scheduler API enabled**:
   ```bash
   gcloud services enable cloudscheduler.googleapis.com --project=PROJECT_ID
   ```
3. **Service account with invoker role**: The scheduler service account needs permission to invoke the worker service.

### Creating Jobs

Use the justfile command to create all scheduled jobs for a stage:

```bash
# Set up all jobs for dev (creates or updates)
just gcp scheduler-setup dev

# Set up all jobs for production
just gcp scheduler-setup prod
```

This command:
- Detects the worker service URL automatically for the given stage
- Creates jobs if they don't exist, or updates them if they do
- Uses OIDC authentication with the stage's service account

### Managing Jobs

```bash
# List all scheduler jobs (all stages)
just gcp scheduler-list

# Run a job manually (for testing)
just gcp scheduler-run $GCP_APP_NAME-cleanup-idempotency-keys-dev

# Pause a job
just gcp scheduler-pause $GCP_APP_NAME-clear-sessions-dev

# Resume a paused job
just gcp scheduler-resume $GCP_APP_NAME-clear-sessions-dev

# Delete all scheduler jobs for a stage (use with caution)
just gcp scheduler-delete-all dev
```

## Environment-Specific Setup

When setting up a new environment, deploy the services and then set up the scheduler:

```bash
# Deploy web and worker services
just gcp deploy-all dev

# Set up scheduler jobs
just gcp scheduler-setup dev
```

For subsequent deployments (code updates only), use `just gcp deploy dev` or `just gcp deploy-all dev` as needed—the scheduler jobs don't need to be recreated unless their configuration changes.

## Adding New Scheduled Jobs

To add a new scheduled task:

1. **Create the management command** in `validibot/<app>/management/commands/`:
   ```python
   # validibot/myapp/management/commands/my_cleanup.py
   from django.core.management.base import BaseCommand


   class Command(BaseCommand):
       help = "Description of what this command does"

       def handle(self, *args, **options):
           # Your cleanup logic here
           self.stdout.write(self.style.SUCCESS("Done!"))
   ```

2. **Add the API endpoint** in `validibot/core/api/scheduled_tasks.py`:
   ```python
   class MyCleanupView(ScheduledTaskBaseView):
       """Description and recommended schedule."""

       def post(self, request):
           self.check_worker_mode()
           out = StringIO()
           call_command("my_cleanup", stdout=out)
           return Response({"status": "completed", "output": out.getvalue().strip()})
   ```

3. **Register the URL** in `config/api_internal_router.py`:
   ```python
   (
       path(
           "scheduled/my-cleanup/",
           MyCleanupView.as_view(),
           name="scheduled-my-cleanup",
       ),
   )
   ```

4. **Add to the scheduler setup** in `justfile` under `gcp-scheduler-setup`:
   ```bash
   create_or_update_job \
       "$GCP_APP_NAME-my-cleanup" \
       "0 5 * * *" \
       "/api/v1/scheduled/my-cleanup/" \
       "Description of the cleanup job"
   ```

5. **Update this documentation** with the new job.

## Monitoring

Cloud Scheduler provides built-in monitoring:

- **Cloud Console**: View job history, success/failure rates
- **Cloud Logging**: Each job execution is logged
- **Alerting**: Set up alerts for job failures

View job history in the console:
```bash
just gcp console  # Opens Cloud Run console
# Then navigate to Cloud Scheduler in the sidebar
```

## Troubleshooting

### Job returns 404

The endpoint only works on worker instances (`APP_IS_WORKER=True`). Ensure:
- You're calling the worker service URL, not the web service
- The worker service is deployed and running

### Job returns 403

OIDC authentication failed. Check:
- Service account has `roles/run.invoker` on the worker service
- Service account email is correct in the scheduler job

### Job times out

Default Cloud Scheduler timeout is 10 minutes. For long-running jobs:
- Increase the timeout in the scheduler job configuration
- Or break the job into smaller chunks

## Cron Schedule Reference

Schedules use standard cron syntax (minute hour day-of-month month day-of-week):

| Pattern | Meaning |
|---------|---------|
| `0 2 * * *` | Daily at 2:00 AM |
| `0 3 * * *` | Daily at 3:00 AM |
| `0 4 * * 0` | Weekly on Sunday at 4:00 AM |
| `0 */6 * * *` | Every 6 hours |
| `*/10 * * * *` | Every 10 minutes |
| `*/15 * * * *` | Every 15 minutes |

All times are in Australia/Sydney timezone (configured in justfile).
