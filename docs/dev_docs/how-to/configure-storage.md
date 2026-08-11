# Configuring Storage

Validibot uses a **single storage location** with prefix-based separation for public and private files:

```
storage/                        # Local: ./storage/  |  GCS: gs://bucket-name/
├── public/                     # Publicly accessible files
│   ├── avatars/                # User profile pictures
│   └── workflow_images/        # Workflow featured images
└── private/                    # Private files (authenticated access only)
    └── runs/                   # Validation run data
        └── {run_id}/           # Each run gets its own directory
            ├── input/          # Written by web app before validation
            │   ├── envelope.json
            │   └── submission.idf
            └── output/         # Written by validator container
                ├── envelope.json
                ├── findings.json
                └── artifacts/
                    └── report.html
```

This guide explains how to configure storage for different deployment scenarios.

## Storage Systems Overview

### Private Files (Django STORAGES "default")

The default Django storage is private. Any model `FileField` or `ImageField`
that does not explicitly select another storage backend writes under the
`private/` prefix.

This includes customer-owned uploads such as:

- Submissions that spill to files
- Uploaded rulesets, FMUs, validator resources, and step resources
- Validation artifacts and evidence manifests

These objects must not be made public. If a view needs to expose a private
object to a user, use an authenticated view or a short-lived signed URL.

### Public Files (Django STORAGES "public")

Only fields that explicitly opt in to `STORAGES["public"]` handle publicly
accessible files:

- User profile pictures (avatars)
- Workflow featured images
- Organization logos

These files are served directly via URL. In production, GCS serves them through the `public/` prefix which has public read access via IAM conditions.

### Private Files (Data Storage)

The `validibot.core.storage` module handles private validation pipeline files:

- User-submitted files for validation (IDF, FMU, etc.)
- Input envelopes (JSON configuration for validators)
- Output envelopes (JSON results from validators)
- Generated artifacts (reports, transformed files)

These files are stored under the `private/` prefix and require authenticated access. Users download their files via time-limited signed URLs.

## Validation Run Storage Structure

Each validation run gets its own directory under `private/runs/{run_id}/`. This structure is **standardized across all deployment platforms** (Docker, Kubernetes, GCS, etc.):

```
private/runs/{run_id}/
├── input/                      # Written by web app
│   ├── envelope.json           # Validation configuration
│   └── {submission_files}      # User-uploaded files (e.g., model.idf)
└── output/                     # Written by validator container
    ├── envelope.json           # Validation results
    ├── findings.json           # Detailed findings
    └── artifacts/              # Generated files
        ├── report.html
        └── transformed.idf
```

### Ownership and Access

| Directory | Written By          | Read By             |
| --------- | ------------------- | ------------------- |
| `input/`  | Web app             | Validator container |
| `output/` | Validator container | Web app (worker)    |

### Container Access

Validator containers receive their run path via environment variable:

```bash
# Container receives:
RUN_PATH=runs/{run_id}

# Container reads from:
${STORAGE_ROOT}/${RUN_PATH}/input/

# Container writes to:
${STORAGE_ROOT}/${RUN_PATH}/output/
```

This standardized structure allows containers to work identically across platforms:

| Platform                       | How Container Accesses Storage          |
| ------------------------------ | --------------------------------------- |
| **Docker Compose** | Shared volume mount at `STORAGE_ROOT`   |
| **Kubernetes**                 | Shared PVC or object storage (MinIO/S3) |
| **Cloud Run Services/Jobs (GCP)** | Attempt-scoped GCS capability token  |

## Configuration by Environment

### Local Development (Default)

When running without cloud storage, files are stored locally:

```
./storage/
├── public/      # MEDIA_ROOT - avatars, workflow images
└── private/     # DATA_STORAGE_ROOT - validation files
```

No configuration needed - this is the default behavior.

### Docker Compose Deployments

For Docker deployments, use a shared volume so both web app and validator containers can access the same storage:

```yaml
# docker-compose.yml
services:
  web:
    volumes:
      - storage_data:/app/storage
    environment:
      - STORAGE_ROOT=/app/storage

  validator-backend-energyplus:
    volumes:
      - storage_data:/app/storage # Same volume!
    environment:
      - STORAGE_ROOT=/app/storage
      - RUN_PATH=runs/${RUN_ID} # Set per-invocation

volumes:
  storage_data:
```

### Local Development with GCS

To test GCS integration locally:

1. Authenticate with Google Cloud:

   ```bash
   gcloud auth application-default login
   ```

2. Set the environment variable:

   ```bash
   export STORAGE_BUCKET=your-dev-bucket
   ```

3. The storage will use GCS with:
   - `public/` prefix for media files
   - `private/` prefix for validation files

### Production (Google Cloud Storage)

Production uses a single GCS bucket with prefix-based access control:

```python
# config/settings/production.py

STORAGE_BUCKET = env("STORAGE_BUCKET")  # e.g., "myapp-storage"

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": STORAGE_BUCKET,
            "location": "private",
            "querystring_auth": True,
        },
    },
    "public": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": STORAGE_BUCKET,
            "location": "public",
            "querystring_auth": False,
        },
    },
}

DATA_STORAGE_BACKEND = "gcs"
DATA_STORAGE_BUCKET = STORAGE_BUCKET
DATA_STORAGE_PREFIX = "private"
```

## GCS Bucket Setup

### Migrating Older Public-Default Deployments

If a deployment previously used `STORAGES["default"]` with `location="public"`,
move every customer-data object out of `public/` before accepting cloud users.
Only these prefixes should remain public:

- `public/avatars/`
- `public/workflow_images/`
- Other documented marketing/public media prefixes

Submission files, rulesets, FMUs, validator resources, step resources,
artifacts, and evidence manifests belong under `private/`. After migration,
test with an unauthenticated `curl` or `gsutil` request and confirm private
objects return access denied.

### Creating the Bucket

```bash
# Create bucket (choose your region)
gcloud storage buckets create gs://your-bucket-name \
    --location=us-west1 \
    --uniform-bucket-level-access
```

### Configuring IAM for Public/Private Separation

The key to this architecture is using **IAM Conditions** to make only the `public/` prefix readable while keeping `private/` restricted:

```bash
# Make public/ prefix publicly readable
gcloud storage buckets add-iam-policy-binding gs://your-bucket-name \
    --member="allUsers" \
    --role="roles/storage.objectViewer" \
    --condition='expression=resource.name.startsWith("projects/_/buckets/your-bucket-name/objects/public/"),title=public-prefix-only'
```

This grants `allUsers` read access **only** to objects under the `public/` prefix. Objects under `private/` remain accessible only to authenticated service accounts.

### Service Account Permissions

Your Cloud Run service account needs full access to the bucket:

```bash
gcloud storage buckets add-iam-policy-binding gs://your-bucket-name \
    --member="serviceAccount:YOUR_SERVICE_ACCOUNT@PROJECT.iam.gserviceaccount.com" \
    --role="roles/storage.objectAdmin"
```

### Verifying Access Control

Test that the configuration is correct:

```bash
# This should work (public file)
curl -I https://storage.googleapis.com/your-bucket-name/public/test.txt

# This should return 403 Forbidden (private file)
curl -I https://storage.googleapis.com/your-bucket-name/private/test.txt
```

## Environment Variables Reference

| Variable               | Default     | Description                              |
| ---------------------- | ----------- | ---------------------------------------- |
| `STORAGE_BUCKET`       | _(none)_    | GCS bucket name (required in production) |
| `STORAGE_ROOT`         | `./storage` | Local filesystem root for storage        |
| `DATA_STORAGE_BACKEND` | `local`     | Backend type: `local` or `gcs`           |
| `DATA_STORAGE_PREFIX`  | `private`   | Prefix for private files in bucket       |

## Using the Data Storage API

```python
from validibot.core.storage import get_data_storage

storage = get_data_storage()

# Write input files (before validation)
storage.write("runs/run-123/input/envelope.json", json_content)
storage.write_file("runs/run-123/input/model.idf", local_path)

# Read output files (after validation)
content = storage.read("runs/run-123/output/envelope.json")

# Write/read Pydantic envelopes
from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope

storage.write_envelope("runs/run-123/input/envelope.json", envelope)

output = storage.read_envelope(
    "runs/run-123/output/envelope.json",
    EnergyPlusOutputEnvelope,
)

# Generate signed download URL (for user downloads)
url = storage.get_download_url(
    "runs/run-123/output/artifacts/report.pdf",
    expires_in=3600,  # 1 hour
    filename="validation-report.pdf",
)
```

## Platform-Specific Implementations

The standardized run directory structure (`runs/{run_id}/input/` and `output/`) works across platforms, but each platform may require specific implementation details:

### Docker/Kubernetes (Shared Filesystem)

- Uses shared volume mounts
- Both web app and containers access the same filesystem path
- Simple and reliable for Docker Compose deployments

### Google Cloud Storage

- The web/worker identity stages attempt data in the GCS bucket
- Validator Services and Jobs receive a short-lived token restricted to one
  attempt prefix; their runtime service account has no ambient object role
- Requires IAM configuration for public/private separation

### Future Platforms (S3, Azure Blob, etc.)

When implementing new storage backends:

1. **Follow the standard structure** - Use `runs/{run_id}/` with `input/` and `output/` subdirectories.
2. **Implement the `DataStorage` interface** - Inherit from `validibot.core.storage.base.DataStorage`
3. **Handle platform-specific auth** - Each platform has its own credential mechanism
4. **Document IAM/access setup** - Public/private separation varies by platform

See `validibot/core/storage/gcs.py` for an example implementation.

## Security Model

### How It Works

Validibot separates public and private files using prefix-based access control:

**For GCS deployments:**

1. Bucket uses **uniform bucket-level access** (no per-object ACLs)
2. An **IAM Condition** grants `allUsers` read access only to the `public/` prefix
3. The `private/` prefix is only accessible to authenticated service accounts
4. Users download their files via **time-limited signed URLs**

**For local/Docker deployments:**

1. Public files are served directly via Django's media handling
2. Private files are served through authenticated Django views
3. Signed URLs use HMAC signatures with Django's `SECRET_KEY`

### Security Checklist (GCS)

- [ ] Bucket has uniform bucket-level access enabled
- [ ] IAM condition restricts `allUsers` to `public/` prefix only
- [ ] Service account has `storage.objectAdmin` role
- [ ] Application never stores sensitive data under `public/` prefix
- [ ] Signed URLs have reasonable expiration times (1 hour default)

### Security Checklist (Local/Docker)

- [ ] Storage volume is not exposed outside the Docker network
- [ ] `SECRET_KEY` is set consistently across app instances
- [ ] Download endpoints require authentication

## Troubleshooting

### "Permission denied" when uploading

Check that:

1. Service account has `storage.objectAdmin` role on the bucket
2. Cloud Run is using the correct service account
3. For local development, you've run `gcloud auth application-default login`

### Public files returning 403

Check that:

1. IAM condition is correctly configured (verify the bucket name in the condition)
2. Files are being stored under the `public/` prefix
3. Run `gcloud storage buckets get-iam-policy gs://bucket` to verify

### Signed URL errors

Check that:

1. The file exists in storage
2. Service account can sign blobs (needs `iam.serviceAccounts.signBlob` permission)
3. For local storage, `SECRET_KEY` is set consistently

### Container can't access files

Check that:

1. Volume is mounted at the correct path in both containers
2. `STORAGE_ROOT` environment variable matches the mount path
3. `RUN_PATH` is set correctly for the validation run
