# =============================================================================
# Validibot - AWS Deployment (STUB)
# =============================================================================
#
# Commands for deploying and managing Validibot on Amazon Web Services.
#
# STATUS: NOT YET IMPLEMENTED
#
# This module is a placeholder for future AWS deployment support. The commands
# below are stubs that mirror the GCP module structure but are not functional.
#
# =============================================================================
# IMPLEMENTATION NOTES
# =============================================================================
#
# When implementing AWS support, consider these architectural choices:
#
# COMPUTE OPTIONS:
#   1. AWS App Runner (similar to Cloud Run)
#      - Fully managed, auto-scaling containers
#      - Simplest option, good for web services
#      - Limitations: max 10 min timeout, less control
#
#   2. ECS Fargate (similar to Cloud Run Jobs)
#      - Serverless container orchestration
#      - Good for long-running tasks
#      - More complex but more flexible
#
#   3. ECS on EC2
#      - Full control over instances
#      - Cost-effective for steady workloads
#      - More operational overhead
#
#   4. AWS Lambda (for specific tasks)
#      - 15 min max timeout
#      - Not suitable for web servers
#      - Could be used for scheduled tasks
#
# DATABASE OPTIONS:
#   1. Amazon RDS for PostgreSQL
#      - Managed PostgreSQL, most similar to Cloud SQL
#      - Multi-AZ for high availability
#      - Supports IAM authentication
#
#   2. Amazon Aurora PostgreSQL
#      - Higher performance, higher cost
#      - Serverless v2 option for auto-scaling
#
# STORAGE OPTIONS:
#   1. Amazon S3
#      - Direct equivalent to GCS
#      - Use for media and file storage
#      - Configure CORS for frontend uploads
#
# SECRETS MANAGEMENT:
#   1. AWS Secrets Manager
#      - Automatic rotation support
#      - Similar to GCP Secret Manager
#      - Higher cost than Parameter Store
#
#   2. AWS Systems Manager Parameter Store
#      - Free tier available
#      - Good for non-rotating secrets
#      - Simpler than Secrets Manager
#
# TASK QUEUE OPTIONS:
#   1. Amazon SQS (Simple Queue Service)
#      - Similar to Cloud Tasks
#      - Use with Lambda or ECS for processing
#
#   2. AWS Step Functions
#      - For complex workflows
#      - Visual workflow designer
#
# SCHEDULING OPTIONS:
#   1. Amazon EventBridge Scheduler
#      - Similar to Cloud Scheduler
#      - Cron or rate expressions
#      - Can invoke Lambda, ECS, or HTTP endpoints
#
# KEY MANAGEMENT:
#   1. AWS KMS (Key Management Service)
#      - Similar to Cloud KMS
#      - For credential signing
#      - HSM-backed keys available
#
# NETWORKING:
#   1. Application Load Balancer (ALB)
#      - SSL termination
#      - Path-based routing
#      - AWS Certificate Manager for free certs
#
#   2. CloudFront (optional)
#      - CDN for static assets
#      - Edge locations worldwide
#
# AUTHENTICATION:
#   - IAM roles for service-to-service auth
#   - IAM user credentials for local dev
#   - Consider AWS IAM Identity Center for team access
#
# RECOMMENDED ARCHITECTURE:
#
#   ┌─────────────────┐     ┌─────────────────┐
#   │  Route 53 DNS   │────►│  ALB + ACM SSL  │
#   └─────────────────┘     └────────┬────────┘
#                                    │
#                          ┌─────────┴─────────┐
#                          │                   │
#                    ┌─────▼─────┐       ┌─────▼─────┐
#                    │ ECS/Fargate │       │ ECS/Fargate │
#                    │   (Web)    │       │  (Worker)  │
#                    └─────┬─────┘       └─────┬─────┘
#                          │                   │
#     ┌────────────────────┼───────────────────┤
#     │                    │                   │
#   ┌─▼────┐         ┌─────▼─────┐       ┌────▼────┐
#   │  S3  │         │  RDS/Aurora │       │  SQS   │
#   └──────┘         └───────────┘       └─────────┘
#
# =============================================================================

# Use bash for shell commands
set shell := ["bash", "-cu"]

# =============================================================================
# AWS Configuration (PLACEHOLDER)
# =============================================================================
#
# These variables will need to be configured for your AWS account.
# Update these when implementing AWS support.
#
# =============================================================================

# AWS region for deployment
# Consider: ap-southeast-2 (Sydney) for Australian users
aws_region := "ap-southeast-2"

# AWS account ID (replace with your account)
aws_account_id := "123456789012"

# ECR repository for container images
# aws_image := aws_account_id + ".dkr.ecr." + aws_region + ".amazonaws.com/validibot"

# Git SHA for image tagging (inherited from common)
git_sha := `git rev-parse --short HEAD`

# =============================================================================
# Not Yet Implemented Notice
# =============================================================================

[private]
_not-implemented:
    @echo ""
    @echo "═══════════════════════════════════════════════════════════════════"
    @echo "  AWS deployment is not yet implemented"
    @echo "═══════════════════════════════════════════════════════════════════"
    @echo ""
    @echo "  Validibot currently supports deployment to:"
    @echo "    - Google Cloud Platform (GCP): just gcp <command>"
    @echo "    - Docker Compose:  just docker-compose <command>"
    @echo ""
    @echo "  AWS support is planned for a future release."
    @echo ""
    @echo "  For now, you can:"
    @echo "    1. Use GCP for cloud deployment"
    @echo "    2. Use Docker Compose for on-premises or cloud VMs"
    @echo "    3. Contribute AWS support (see just/aws/mod.just)"
    @echo ""
    @echo "═══════════════════════════════════════════════════════════════════"
    @echo ""

# =============================================================================
# Build & Push (STUB)
# =============================================================================
#
# A future AWS web target should serve ``config.asgi:application`` so the Pro
# feature gate can mount embedded MCP without an extra image or service.

# Build Docker image for AWS (linux/amd64 platform)
build: _not-implemented
    @echo "Would build: validibot:{{git_sha}}"
    @echo ""
    @echo "Implementation needed:"
    @echo "  - Build for linux/amd64"
    @echo "  - Tag for ECR registry"
    @echo "  - Consider multi-arch builds"

# Push Docker image to Amazon ECR
push: _not-implemented
    @echo "Would push to ECR"
    @echo ""
    @echo "Implementation needed:"
    @echo "  - Authenticate with ECR: aws ecr get-login-password"
    @echo "  - Push to ECR repository"
    @echo "  - Consider lifecycle policies for old images"

# =============================================================================
# Deploy Services (STUB)
# =============================================================================

# Deploy web service to AWS
# Usage: just aws deploy dev | just aws deploy prod
deploy stage: _not-implemented
    @echo "Would deploy web service to {{stage}}"
    @echo ""
    @echo "Implementation options:"
    @echo "  1. AWS App Runner - simplest, similar to Cloud Run"
    @echo "  2. ECS Fargate - more control, better for production"
    @echo "  3. ECS on EC2 - cost-effective for steady load"
    @echo ""
    @echo "Key considerations:"
    @echo "  - VPC configuration for database access"
    @echo "  - IAM roles for service permissions"
    @echo "  - Secrets Manager integration"
    @echo "  - Auto-scaling policies"

# Deploy worker service to AWS
# Usage: just aws deploy-worker dev | just aws deploy-worker prod
deploy-worker stage: _not-implemented
    @echo "Would deploy worker service to {{stage}}"
    @echo ""
    @echo "Implementation needed:"
    @echo "  - ECS Task Definition for worker"
    @echo "  - SQS queue for task distribution"
    @echo "  - IAM role with queue permissions"

# Deploy both web and worker
deploy-all stage: _not-implemented
    @echo "Would deploy all services to {{stage}}"

# =============================================================================
# Secrets Management (STUB)
# =============================================================================

# Upload secrets to AWS Secrets Manager
# Usage: just aws secrets dev | just aws secrets prod
secrets stage: _not-implemented
    @echo "Would upload secrets for {{stage}}"
    @echo ""
    @echo "Implementation options:"
    @echo "  1. AWS Secrets Manager"
    @echo "     - aws secretsmanager create-secret"
    @echo "     - Supports automatic rotation"
    @echo "     - Higher cost"
    @echo ""
    @echo "  2. AWS Parameter Store"
    @echo "     - aws ssm put-parameter --type SecureString"
    @echo "     - Free tier available"
    @echo "     - Manual rotation"

# Create environment file template
secrets-init stage: _not-implemented
    @echo "Would create env template for {{stage}}"

# =============================================================================
# Operations (STUB)
# =============================================================================

# View service logs
# Usage: just aws logs dev | just aws logs prod
logs stage: _not-implemented
    @echo "Would show logs for {{stage}}"
    @echo ""
    @echo "Implementation:"
    @echo "  - aws logs tail /ecs/validibot-web-{{stage}}"
    @echo "  - Or: aws logs filter-log-events"

# Follow logs in real-time
logs-follow stage: _not-implemented
    @echo "Would follow logs for {{stage}}"
    @echo ""
    @echo "Implementation:"
    @echo "  - aws logs tail --follow /ecs/validibot-web-{{stage}}"

# Show service status
# Usage: just aws status dev | just aws status prod
status stage: _not-implemented
    @echo "Would show status for {{stage}}"
    @echo ""
    @echo "Implementation:"
    @echo "  - aws ecs describe-services"
    @echo "  - aws ecs describe-tasks"
    @echo "  - Show ALB health checks"

# Show status of all stages
status-all: _not-implemented
    @echo "Would show status for all stages"

# Quick health check
health-check stage: _not-implemented
    @echo "Would check health of {{stage}}"
    @echo ""
    @echo "Implementation:"
    @echo "  - Curl the ALB endpoint"
    @echo "  - Check ECS task status"

# =============================================================================
# Management Commands (STUB)
# =============================================================================

# Run database migrations
# Usage: just aws migrate dev | just aws migrate prod
migrate stage: _not-implemented
    @echo "Would run migrations for {{stage}}"
    @echo ""
    @echo "Implementation options:"
    @echo "  1. ECS RunTask with migration command"
    @echo "  2. CodeBuild project for migrations"
    @echo "  3. Lambda function (if quick enough)"

# Run setup_validibot to initialize site and default data
setup-data stage: _not-implemented
    @echo "Would run setup_validibot for {{stage}}"

# Run any Django management command
# Usage: just aws management-cmd prod "shell"
management-cmd stage command: _not-implemented
    @echo "Would run: python manage.py {{command}}"
    @echo "On stage: {{stage}}"

# =============================================================================
# Infrastructure Setup (STUB)
# =============================================================================

# Initialize AWS infrastructure for a stage
# Usage: just aws init-stage dev
init-stage stage: _not-implemented
    @echo "Would initialize infrastructure for {{stage}}"
    @echo ""
    @echo "Resources to create:"
    @echo "  1. VPC with public/private subnets"
    @echo "  2. RDS PostgreSQL instance"
    @echo "  3. S3 buckets for media/files"
    @echo "  4. ECR repository"
    @echo "  5. ECS cluster"
    @echo "  6. IAM roles and policies"
    @echo "  7. Security groups"
    @echo "  8. ALB with ACM certificate"
    @echo "  9. SQS queue for tasks"
    @echo "  10. Secrets in Secrets Manager"
    @echo ""
    @echo "Consider using:"
    @echo "  - CloudFormation templates"
    @echo "  - Terraform modules"
    @echo "  - AWS CDK"

# List resources for a stage
list-resources stage: _not-implemented
    @echo "Would list resources for {{stage}}"

# =============================================================================
# Scheduler (STUB)
# =============================================================================

# Set up scheduled tasks
# Usage: just aws scheduler-setup dev | just aws scheduler-setup prod
scheduler-setup stage: _not-implemented
    @echo "Would set up schedulers for {{stage}}"
    @echo ""
    @echo "Implementation:"
    @echo "  - EventBridge Scheduler rules"
    @echo "  - Target: ECS RunTask or Lambda"
    @echo ""
    @echo "Scheduled tasks needed:"
    @echo "  - Clear expired sessions (daily)"
    @echo "  - Cleanup idempotency keys (daily)"
    @echo "  - Cleanup callback receipts (weekly)"
    @echo "  - Purge expired submissions (hourly)"
    @echo "  - Process purge retries (every 5 min)"
    @echo "  - Cleanup stuck runs (every 10 min)"

# List scheduler jobs
scheduler-list: _not-implemented
    @echo "Would list EventBridge rules"

# =============================================================================
# Validator Containers (STUB)
# =============================================================================

# Build a validator container
validator-build name: _not-implemented
    @echo "Would build validator: {{name}}"

# Deploy one signed validator release as an ECS task
validator-deploy name stage release_tag: _not-implemented
    @echo "Would deploy validator {{name}} {{release_tag}} to {{stage}}"
    @echo ""
    @echo "Implementation:"
    @echo "  - ECS Task Definition for validator"
    @echo "  - IAM role with S3 access"
    @echo "  - Triggered via SQS or direct invocation"

# Deploy one signed release of all validators
validator-deploy-all stage release_tag: _not-implemented
    @echo "Would deploy all validators at {{release_tag}} to {{stage}}"

# =============================================================================
# Maintenance Mode (STUB)
# =============================================================================

# Put stage into maintenance mode
maintenance-on stage: _not-implemented
    @echo "Would enable maintenance for {{stage}}"
    @echo ""
    @echo "Actions:"
    @echo "  - Set ECS desired count to 0"
    @echo "  - Or: Update ALB rules to return 503"
    @echo "  - Pause EventBridge rules"
    @echo "  - Optionally stop RDS instance"

# Bring stage out of maintenance mode
maintenance-off stage: _not-implemented
    @echo "Would disable maintenance for {{stage}}"

# Check maintenance status
maintenance-status stage: _not-implemented
    @echo "Would check maintenance status for {{stage}}"

# =============================================================================
# Helpers (STUB)
# =============================================================================

# Authenticate with ECR
auth: _not-implemented
    @echo "Would authenticate with ECR"
    @echo ""
    @echo "Command:"
    @echo "  aws ecr get-login-password --region {{aws_region}} | \\"
    @echo "    docker login --username AWS --password-stdin \\"
    @echo "    {{aws_account_id}}.dkr.ecr.{{aws_region}}.amazonaws.com"

# Open AWS Console for the service
console stage: _not-implemented
    @echo "Would open AWS Console for {{stage}}"
    @echo ""
    @echo "URL: https://{{aws_region}}.console.aws.amazon.com/ecs/"

# =============================================================================
# Contributing AWS Support
# =============================================================================
#
# If you'd like to contribute AWS deployment support:
#
# 1. Fork the repository
#
# 2. Study the GCP implementation in just/gcp/mod.just
#    - It's the reference implementation for the command structure
#    - AWS commands should mirror the same interface where possible
#
# 3. Start with these priorities:
#    a. init-stage - create basic infrastructure
#    b. build/push - container registry
#    c. deploy/deploy-worker - ECS services
#    d. secrets - Secrets Manager
#    e. migrate/management-cmd - one-off tasks
#    f. scheduler-setup - cron jobs
#
# 4. Consider using Infrastructure as Code:
#    - CloudFormation: Native AWS, YAML/JSON
#    - Terraform: Multi-cloud, widely used
#    - AWS CDK: TypeScript/Python, more flexible
#    - Pulumi: Code-first, multi-language
#
# 5. Document your architecture choices in this file
#
# 6. Test thoroughly before submitting a PR
#
# =============================================================================
