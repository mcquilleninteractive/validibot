"""Expose MCP tool invocation as a first-class audit action."""

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    """Keep persisted audit choices aligned with MCP evidence records."""

    dependencies = [
        ("audit", "0005_add_validator_deployment_deactivated_action"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auditlogentry",
            name="action",
            field=models.CharField(
                choices=[
                    ("workflow_created", "Workflow Created"),
                    ("workflow_updated", "Workflow Updated"),
                    ("workflow_deleted", "Workflow Deleted"),
                    ("ruleset_created", "Ruleset Created"),
                    ("ruleset_updated", "Ruleset Updated"),
                    ("ruleset_deleted", "Ruleset Deleted"),
                    ("validator_added", "Validator Added"),
                    ("validator_updated", "Validator Updated"),
                    ("validator_removed", "Validator Removed"),
                    (
                        "validator_deployment_activated",
                        "Validator Deployment Activated",
                    ),
                    (
                        "validator_deployment_deactivated",
                        "Validator Deployment Deactivated",
                    ),
                    (
                        "validator_deployment_registered",
                        "Validator Deployment Registered",
                    ),
                    (
                        "validator_deployment_verified",
                        "Validator Deployment Verified",
                    ),
                    (
                        "validator_deployment_capacity_updated",
                        "Validator Deployment Capacity Updated",
                    ),
                    (
                        "validator_deployment_blocked",
                        "Validator Deployment Blocked",
                    ),
                    (
                        "validator_deployment_unblocked",
                        "Validator Deployment Unblocked",
                    ),
                    (
                        "validator_deployment_retired",
                        "Validator Deployment Retired",
                    ),
                    ("org_updated", "Organization Updated"),
                    ("org_deleted", "Organization Deleted"),
                    ("member_invited", "Member Invited"),
                    ("member_role_changed", "Member Role Changed"),
                    ("member_removed", "Member Removed"),
                    ("guest_granted", "Guest Access Granted"),
                    ("guest_revoked", "Guest Access Revoked"),
                    ("api_key_created", "API Key Created"),
                    ("api_key_revoked", "API Key Revoked"),
                    ("mcp_tool_called", "MCP Tool Called"),
                    ("login_succeeded", "Login Succeeded"),
                    ("login_failed", "Login Failed"),
                    ("mfa_enabled", "MFA Enabled"),
                    ("mfa_disabled", "MFA Disabled"),
                    ("mfa_challenge_failed", "MFA Challenge Failed"),
                    ("password_changed", "Password Changed"),
                    ("password_reset_requested", "Password Reset Requested"),
                    ("session_revoked", "Session Revoked"),
                    ("email_added", "Email Address Added"),
                    ("email_changed", "Email Address Changed"),
                    ("email_verified", "Email Address Verified"),
                    ("email_removed", "Email Address Removed"),
                    ("admin_object_changed", "Admin Object Changed"),
                    ("user_promoted_to_basic", "User Promoted to Basic"),
                    ("user_demoted_to_guest", "User Demoted to Guest"),
                    ("user_groups_changed", "User Groups Changed"),
                    ("user_erasure_requested", "User Erasure Requested"),
                    ("user_erasure_completed", "User Erasure Completed"),
                    ("audit_entry_sanitised", "Audit Entry Sanitised"),
                ],
                help_text="Machine-readable action code; see AuditAction.",
                max_length=64,
            ),
        ),
    ]
