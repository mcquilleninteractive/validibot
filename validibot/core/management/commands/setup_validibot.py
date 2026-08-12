"""
Set up Validibot for first use.

This command initializes a fresh Validibot installation with all the
configuration and data it needs to run. Run it once after applying
migrations to get your instance ready.

Usage:
    python manage.py migrate
    python manage.py setup_validibot --domain myvalidibot.example.com
    python manage.py check_validibot
    python manage.py createsuperuser

What this command does:
    1. Configures your site domain (used in emails and links)
    2. Sets up background job schedules (cleanup tasks, session management)
    3. Creates the permission system for role-based access control
    4. Initializes default user roles (Owner, Admin, Author, etc.)
    5. Creates default validators (JSON Schema, XML Schema, Basic, etc.)
    6. Sets up user workspaces and default projects
    7. Creates default actions

The command is idempotent - you can safely run it multiple times.
To update your domain later, just run the command again with the new value.

For more information, see: https://docs.validibot.com/installation
"""

from __future__ import annotations

import logging
import os
import sys
from uuid import uuid4

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils.text import slugify

logger = logging.getLogger(__name__)

# Permissions that Validibot needs for its role-based access control system.
# Format: (codename, display_name, app_label, model)
DEFAULT_PERMISSIONS = [
    ("workflow_launch", "Can launch workflows", "workflows", "workflow"),
    ("workflow_view", "Can view workflows", "workflows", "workflow"),
    ("workflow_edit", "Can edit workflows", "workflows", "workflow"),
    (
        "admin_manage_org",
        "Can manage organizations and members",
        "users",
        "organization",
    ),
    (
        "validation_results_view_all",
        "Can view all validation results",
        "validations",
        "validationrun",
    ),
    (
        "validation_results_view_own",
        "Can view own validation results",
        "validations",
        "validationrun",
    ),
    ("validator_view", "Can view validators", "validations", "validator"),
    ("validator_edit", "Can create or edit validators", "validations", "validator"),
    ("analytics_view", "Can view analytics", "validations", "validationrun"),
    ("analytics_review", "Can review analytics", "validations", "validationrun"),
]


def _manager_for(model):
    """Get the appropriate manager for a model, preferring all_objects if available."""
    return getattr(model, "all_objects", model._default_manager)


class Command(BaseCommand):
    help = "Set up Validibot for first use (site, validators, roles, schedules)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--domain",
            type=str,
            help=(
                "Your Validibot domain (e.g., validibot.example.com). "
                "Can also use VALIDIBOT_SITE_DOMAIN environment variable."
            ),
        )
        parser.add_argument(
            "--site-name",
            type=str,
            help=(
                "Display name for your site (e.g., 'My Company Validibot'). "
                "Defaults to 'Validibot'. Can also use VALIDIBOT_SITE_NAME env var."
            ),
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_true",
            dest="no_input",
            help="Run without prompts. Uses localhost:8000 if no domain provided.",
        )

    def handle(self, *args, **options):
        self.stdout.write("")
        self.stdout.write(self.style.HTTP_INFO("=" * 60))
        self.stdout.write(self.style.HTTP_INFO("  Welcome to Validibot Setup"))
        self.stdout.write(self.style.HTTP_INFO("=" * 60))
        self.stdout.write("")
        self.stdout.write("This command will configure your Validibot installation.")
        self.stdout.write(
            "It's safe to run multiple times - existing data will be updated."
        )
        self.stdout.write("")

        # Step 1: Site configuration
        self.stdout.write(self.style.MIGRATE_HEADING("Step 1/7: Site Configuration"))
        self.stdout.write("-" * 40)
        domain = self._resolve_domain(options)
        site_name = self._resolve_site_name(options)
        self._setup_site_domain(domain, site_name)
        self.stdout.write("")

        # Step 2: Background job schedules
        self.stdout.write(
            self.style.MIGRATE_HEADING("Step 2/7: Background Job Schedules")
        )
        self.stdout.write("-" * 40)
        self._setup_celery_beat_schedules()
        self.stdout.write("")

        # Step 3: Permissions
        self.stdout.write(self.style.MIGRATE_HEADING("Step 3/7: Permission System"))
        self.stdout.write("-" * 40)
        self._setup_permissions()
        self.stdout.write("")

        # Step 4: Roles
        self.stdout.write(self.style.MIGRATE_HEADING("Step 4/7: User Roles"))
        self.stdout.write("-" * 40)
        self._setup_roles()
        self.stdout.write("")

        # Step 5: Default validators
        self.stdout.write(self.style.MIGRATE_HEADING("Step 5/7: Default Validators"))
        self.stdout.write("-" * 40)
        self._setup_default_validators()
        self.stdout.write("")

        # Step 6: User workspaces and projects
        self.stdout.write(self.style.MIGRATE_HEADING("Step 6/7: Workspaces & Projects"))
        self.stdout.write("-" * 40)
        self._setup_workspaces_and_projects()
        self.stdout.write("")

        # Step 7: Default actions and superuser
        self.stdout.write(self.style.MIGRATE_HEADING("Step 7/7: Actions & Admin User"))
        self.stdout.write("-" * 40)
        self._setup_default_actions()
        self._setup_local_superuser()
        self.stdout.write("")

        # Success message
        self.stdout.write(self.style.HTTP_INFO("=" * 60))
        self.stdout.write(self.style.SUCCESS("  Setup Complete!"))
        self.stdout.write(self.style.HTTP_INFO("=" * 60))
        self.stdout.write("")
        self.stdout.write("Your Validibot instance is configured and ready to use.")
        self.stdout.write("")
        self.stdout.write("Next steps:")
        self.stdout.write("  1. Verify setup:   python manage.py check_validibot")
        self.stdout.write("  2. Create admin:   python manage.py createsuperuser")
        self.stdout.write("  3. Start server:   python manage.py runserver")
        self.stdout.write(f"  4. Visit site:     http://{domain}/")
        self.stdout.write("")

    # =========================================================================
    # Step 1: Site Configuration
    # =========================================================================

    def _resolve_domain(self, options) -> str:
        """
        Figure out what domain to use for this Validibot instance.

        We check in this order:
        1. --domain command line argument
        2. VALIDIBOT_SITE_DOMAIN environment variable
        3. Interactive prompt (if running in a terminal)
        4. Default to localhost:8000 (for development)
        """
        logger.debug("Resolving site domain configuration...")

        # Check CLI argument first
        if options.get("domain"):
            domain = options["domain"]
            logger.debug(f"Using domain from --domain argument: {domain}")
            self.stdout.write(f"  Using domain from command line: {domain}")
            return domain

        # Check environment variable
        env_domain = os.environ.get("VALIDIBOT_SITE_DOMAIN")
        if env_domain:
            logger.debug(
                "Using domain from VALIDIBOT_SITE_DOMAIN env var: %s", env_domain
            )
            self.stdout.write(f"  Using domain from environment variable: {env_domain}")
            return env_domain

        # Interactive prompt if we have a terminal
        if not options.get("no_input") and sys.stdin.isatty():
            self.stdout.write("")
            self.stdout.write("  What domain will users use to access Validibot?")
            self.stdout.write("  Examples: validibot.mycompany.com, localhost:8000")
            self.stdout.write("")

            while True:
                domain = input("  Site domain: ").strip()
                if domain:
                    logger.debug(f"Using domain from interactive input: {domain}")
                    return domain
                self.stdout.write(
                    self.style.WARNING(
                        "  Please enter a domain, or press Ctrl+C to cancel."
                    )
                )

        # Default fallback for non-interactive mode
        default_domain = "localhost:8000"
        logger.debug(f"No domain provided, using default: {default_domain}")
        self.stdout.write(
            self.style.WARNING(f"  No domain provided. Using default: {default_domain}")
        )
        self.stdout.write("  You can update this later by running:")
        self.stdout.write(
            "    python manage.py setup_validibot --domain yourdomain.com"
        )
        return default_domain

    def _resolve_site_name(self, options) -> str:
        """
        Figure out the display name for this Validibot instance.

        We check in this order:
        1. --site-name command line argument
        2. VALIDIBOT_SITE_NAME environment variable
        3. Default to "Validibot"
        """
        logger.debug("Resolving site display name...")

        if options.get("site_name"):
            name = options["site_name"]
            logger.debug(f"Using site name from --site-name argument: {name}")
            return name

        env_name = os.environ.get("VALIDIBOT_SITE_NAME")
        if env_name:
            logger.debug(
                "Using site name from VALIDIBOT_SITE_NAME env var: %s", env_name
            )
            return env_name

        logger.debug("Using default site name: Validibot")
        return "Validibot"

    def _setup_site_domain(self, domain: str, site_name: str):
        """
        Configure the Django Sites framework with the user's domain.

        Django's Sites framework is used to generate absolute URLs in emails
        and other contexts where we need to know the full site URL.
        """
        logger.debug("Configuring site: domain=%s, name=%s", domain, site_name)
        self.stdout.write("  Configuring Django Sites framework...")

        site, created = Site.objects.update_or_create(
            id=settings.SITE_ID,
            defaults={
                "domain": domain,
                "name": site_name,
            },
        )

        if created:
            logger.debug("Created new Site record, fixing PostgreSQL sequence...")
            # When we explicitly provide an ID, PostgreSQL's auto-increment
            # sequence doesn't update. We need to fix it manually to avoid
            # duplicate key errors on future Site creates.
            max_id = Site.objects.order_by("-id").first().id
            with connection.cursor() as cursor:
                cursor.execute("SELECT last_value from django_site_id_seq")
                (current_id,) = cursor.fetchone()
                if current_id <= max_id:
                    cursor.execute(
                        "alter sequence django_site_id_seq restart with %s",
                        [max_id + 1],
                    )
                    logger.debug(f"Updated django_site_id_seq to {max_id + 1}")
            action = "Created"
        else:
            action = "Updated"
            logger.debug("Updated existing Site record")

        self.stdout.write(self.style.SUCCESS(f"  {action} site configuration:"))
        self.stdout.write(f"    Domain: {site.domain}")
        self.stdout.write(f"    Name:   {site.name}")

    # =========================================================================
    # Step 2: Background Job Schedules
    # =========================================================================

    def _setup_celery_beat_schedules(self):
        """
        Set up the background job schedules that keep Validibot healthy.

        These scheduled tasks handle cleanup and maintenance:
        - Purging expired submissions and validation outputs
        - Cleaning up stuck validation runs
        - Removing expired temporary data
        - Clearing old Django sessions
        - Cleaning up orphaned Docker containers

        Task definitions are stored in validibot.core.tasks.registry - the
        single source of truth for all scheduled tasks across backends.

        On GCP, periodic tasks are managed by Cloud Scheduler (via
        ``just gcp scheduler-setup``), so Celery Beat sync is skipped.
        """
        logger.debug("Setting up Celery Beat periodic task schedules...")
        self.stdout.write("  Setting up background job schedules...")
        self.stdout.write("  These tasks run automatically to keep Validibot healthy.")
        self.stdout.write("")

        # On GCP, Cloud Scheduler handles periodic tasks instead of Celery Beat.
        # The schedules are created by `just gcp scheduler-setup`, not here.
        deployment_target = getattr(settings, "DEPLOYMENT_TARGET", None)
        if deployment_target == "gcp":
            self.stdout.write(
                self.style.SUCCESS(
                    "  Skipped: GCP uses Cloud Scheduler for periodic tasks."
                )
            )
            self.stdout.write(
                "  Run `just gcp scheduler-setup <stage>` to configure schedules."
            )
            return

        try:
            # Check if django_celery_beat is available
            import django_celery_beat  # noqa: F401
        except ImportError:
            logger.warning("django_celery_beat not installed, skipping schedule setup")
            self.stdout.write(
                self.style.WARNING("  Skipped: django_celery_beat is not installed.")
            )
            self.stdout.write("  Background job schedules require Celery Beat.")
            self.stdout.write(
                "  If you're using GCP Cloud Scheduler, this is expected."
            )
            return

        # Use the sync_schedules command which reads from the task registry
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("sync_schedules", backend="celery", stdout=out)

        # Parse the output to show summary
        output = out.getvalue()
        logger.debug("sync_schedules output: %s", output)

        # Show task list from registry
        from validibot.core.tasks.registry import Backend
        from validibot.core.tasks.registry import get_admin_tasks_for_backend

        tasks = get_admin_tasks_for_backend(Backend.CELERY)
        for task in tasks:
            self.stdout.write(f"    • {task.name}: {task.schedule_cron}")

        self.stdout.write("")
        task_count = len(tasks)
        msg = f"  Configured {task_count} background jobs from registry"
        self.stdout.write(self.style.SUCCESS(msg))

    # =========================================================================
    # Step 3: Permissions
    # =========================================================================

    def _setup_permissions(self):
        """
        Create the custom permissions that power Validibot's access control.

        These permissions are assigned to roles, which are then granted to
        organization members. This allows fine-grained control over who can
        view, edit, or launch workflows.
        """
        logger.debug("Setting up custom permissions for RBAC...")
        self.stdout.write(
            "  Creating custom permissions for role-based access control..."
        )

        from django.contrib.auth.models import Permission

        created_count = 0
        for codename, name, app_label, model in DEFAULT_PERMISSIONS:
            logger.debug(f"Processing permission: {codename}")

            content_type, ct_created = ContentType.objects.get_or_create(
                app_label=app_label,
                model=model,
            )
            if ct_created:
                logger.debug(f"Created content type: {app_label}.{model}")

            _, created = Permission.objects.update_or_create(
                codename=codename,
                content_type=content_type,
                defaults={"name": name},
            )
            if created:
                created_count += 1
                logger.debug(f"Created permission: {codename}")

        self.stdout.write(
            self.style.SUCCESS(
                f"  Configured {len(DEFAULT_PERMISSIONS)} permissions "
                f"({created_count} new)"
            )
        )

    # =========================================================================
    # Step 4: Roles
    # =========================================================================

    def _setup_roles(self):
        """
        Initialize the default roles that users can be assigned.

        Roles bundle permissions together into meaningful groups:
        - Owner: Full access to everything in the organization
        - Admin: Can manage workflows and view all results
        - Author: Can create and edit workflows and validators
        - Executor: Can run workflows and view their own results
        - Viewer roles: Read-only access to specific features

        When someone creates an organization, they automatically get the
        Owner role with all permissions.
        """
        logger.debug("Setting up default user roles...")
        self.stdout.write("  Creating default user roles...")

        from validibot.users.constants import RoleCode
        from validibot.users.models import Membership
        from validibot.users.models import MembershipRole
        from validibot.users.models import Role

        # Create all roles defined in the RoleCode enum
        roles_by_code = {}
        created_count = 0
        for role_code in RoleCode:
            db_role, created = Role.objects.get_or_create(
                code=role_code.value,
                defaults={"name": role_code.label},
            )
            roles_by_code[role_code.value] = db_role
            if created:
                created_count += 1
                logger.debug(f"Created role: {role_code.value} ({role_code.label})")

        self.stdout.write(
            self.style.SUCCESS(
                f"  Configured {len(roles_by_code)} roles ({created_count} new)"
            )
        )

        # Ensure owners have all roles (for existing organizations)
        logger.debug("Checking owner memberships for complete role grants...")
        owner_memberships = Membership.objects.filter(
            membership_roles__role__code=RoleCode.OWNER,
            is_active=True,
        ).distinct()

        grants_created = 0
        for membership in owner_memberships:
            for role in roles_by_code.values():
                _, created = MembershipRole.objects.get_or_create(
                    membership=membership,
                    role=role,
                )
                if created:
                    grants_created += 1
                    logger.debug(f"Granted {role.code} to membership {membership.id}")

        if owner_memberships.exists() and grants_created > 0:
            self.stdout.write(f"  Updated {grants_created} role grants for owners")

    # =========================================================================
    # Step 5: Default Validators
    # =========================================================================

    def _setup_default_validators(self):
        """
        Create the built-in validators that ship with Validibot.

        These include JSON Schema, XML Schema, Basic (field validation),
        and AI validators. Also syncs advanced validators (EnergyPlus, FMU)
        with their step I/O definitions for input/output values.
        """
        logger.debug("Setting up default validators...")
        self.stdout.write("  Creating default validators...")

        from validibot.validations.utils import create_default_validators

        created, updated = create_default_validators()
        self.stdout.write(
            self.style.SUCCESS(
                f"  Configured built-in validators ({created} new, {updated} updated)"
            )
        )

        # Sync system validators (EnergyPlus, FMU, THERM) with their
        # step I/O definitions.
        # This populates the input/output values needed for the step editor UI.
        self.stdout.write("  Syncing system validators and step I/O definitions...")

        from io import StringIO

        from django.core.management import call_command

        # ``setup_validibot`` is fundamentally a "reset the DB to match the
        # current code state" command — operators run it precisely because
        # they want the DB updated. ``--allow-drift`` matches that intent:
        # without it, every dev iteration that touches a semantic config
        # field in the catalog port contract blocks
        # setup_validibot until the operator manually runs
        # ``sync_validators --allow-drift`` to clear the digest mismatch.
        #
        # The drift detector still does its job in two places where it
        # actually matters: the production container start script (which
        # keeps the strict check) and audit_workflow_versions (the
        # tamper-detection report). Both run *outside* setup_validibot.
        out = StringIO()
        call_command("sync_validators", "--allow-drift", stdout=out)

        # Log the output for debugging
        output = out.getvalue()
        logger.debug("sync_validators output: %s", output)

        self.stdout.write(
            self.style.SUCCESS("  System validators and step I/O definitions synced")
        )

    # =========================================================================
    # Step 6: Workspaces and Projects
    # =========================================================================

    def _setup_workspaces_and_projects(self):
        """
        Ensure all users have personal workspaces and default projects.

        This handles:
        - Creating personal workspaces for users who don't have one
        - Creating default projects for personal organizations
        - Assigning default projects to workflows that don't have one
        """
        logger.debug("Setting up workspaces and projects...")
        self.stdout.write("  Ensuring user workspaces and default projects...")

        from validibot.projects.models import Project
        from validibot.users.constants import RoleCode
        from validibot.users.models import Membership
        from validibot.users.models import Organization
        from validibot.users.models import Role
        from validibot.users.models import User
        from validibot.workflows.models import Workflow

        # Get roles needed for personal workspaces
        roles_needed = {
            code: Role.objects.filter(code=code).first()
            for code in (RoleCode.ADMIN, RoleCode.OWNER, RoleCode.EXECUTOR)
        }
        missing = [code for code, role in roles_needed.items() if role is None]
        if missing:
            logger.warning("Cannot assign roles %s - they do not exist.", missing)

        workspaces_created = 0
        projects_created = 0

        # Process each user
        for user in User.objects.all():
            membership = (
                Membership.objects.filter(user=user, org__is_personal=True)
                .select_related("org")
                .first()
            )

            if membership:
                # User has a personal workspace, ensure they have roles
                roles_to_add = [role for role in roles_needed.values() if role]
                if roles_to_add:
                    membership.roles.add(*roles_to_add)
                # Ensure default project exists
                project, created = self._ensure_default_project(membership.org)
                if created:
                    projects_created += 1
                # Set current org if not set
                if user.current_org_id != membership.org_id:
                    user.current_org = membership.org
                    user.save(update_fields=["current_org"])
            else:
                # Create personal workspace
                workspace_name = self._workspace_name(user)
                slug = self._unique_slug(
                    Organization,
                    workspace_name,
                    prefix="workspace-",
                )
                org = Organization.objects.create(
                    name=workspace_name,
                    slug=slug,
                    is_personal=True,
                )
                membership = Membership.objects.create(
                    user=user,
                    org=org,
                    is_active=True,
                )
                roles_to_add = [role for role in roles_needed.values() if role]
                if roles_to_add:
                    membership.roles.add(*roles_to_add)
                project, _ = self._ensure_default_project(org)
                projects_created += 1
                user.current_org = org
                user.save(update_fields=["current_org"])
                workspaces_created += 1
                logger.debug(f"Created workspace '{org.slug}' for user {user.username}")

        # Ensure default projects for all personal orgs
        for org in Organization.objects.filter(is_personal=True):
            _, created = self._ensure_default_project(org)
            if created:
                projects_created += 1

        # Assign default projects to workflows without one
        workflows_updated = 0
        for workflow in Workflow.objects.filter(project__isnull=True).select_related(
            "org"
        ):
            if not workflow.org:
                continue
            project = (
                Project.objects.filter(org=workflow.org, is_default=True).first()
                or _manager_for(Project)
                .filter(org=workflow.org, is_default=True)
                .first()
            )
            if not project:
                project, _ = self._ensure_default_project(workflow.org)
            if project:
                Workflow.objects.filter(pk=workflow.pk).update(project=project)
                workflows_updated += 1

        results = []
        if workspaces_created:
            results.append(f"{workspaces_created} workspaces")
        if projects_created:
            results.append(f"{projects_created} projects")
        if workflows_updated:
            results.append(f"{workflows_updated} workflows updated")

        if results:
            self.stdout.write(self.style.SUCCESS(f"  Created {', '.join(results)}"))
        else:
            self.stdout.write(self.style.SUCCESS("  All workspaces up to date"))

    def _workspace_name(self, user) -> str:
        """Generate a workspace name for a user."""
        name = (user.name or "").strip() or (user.username or "").strip() or "Workspace"
        if name.endswith("s"):
            return f"{name}' Workspace"
        return f"{name}'s Workspace"

    def _unique_slug(
        self,
        model,
        base: str,
        *,
        prefix: str = "",
        filter_kwargs=None,
    ) -> str:
        """Generate a unique slug for a model."""
        base_slug = slugify(base) or uuid4().hex[:8]
        if prefix:
            base_slug = f"{prefix}{base_slug}"
        slug = base_slug
        counter = 2
        manager = _manager_for(model)
        lookup = dict(filter_kwargs or {})
        while manager.filter(slug=slug, **lookup).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        return slug

    def _ensure_default_project(self, org):
        """Ensure an organization has a default project."""
        from validibot.projects.models import Project

        manager = _manager_for(Project)
        project = manager.filter(org=org, is_default=True).order_by("id").first()
        if project:
            if not project.is_active:
                project.is_active = True
                project.deleted_at = None
                project.save(update_fields=["is_active", "deleted_at"])
            return project, False

        name = "Default Project"
        slug = self._unique_slug(
            Project,
            name,
            prefix="default-",
            filter_kwargs={"org": org},
        )
        project = Project.objects.create(
            org=org,
            name=name,
            description="",
            slug=slug,
            is_default=True,
            is_active=True,
        )
        return project, True

    # =========================================================================
    # Step 7: Actions and Superuser
    # =========================================================================

    def _setup_default_actions(self):
        """Sync workflow action definitions from the registered action plugins."""
        logger.debug("Setting up default actions...")
        self.stdout.write("  Syncing action definitions...")

        from validibot.actions.utils import create_default_actions

        create_default_actions()
        self.stdout.write(self.style.SUCCESS("  Action definitions configured"))

    def _setup_local_superuser(self):
        """
        Set up a local superuser for development/production if env vars are present.

        Reads SUPERUSER_USERNAME, SUPERUSER_PASSWORD, SUPERUSER_EMAIL, and
        SUPERUSER_NAME from Django settings. If username is configured, creates
        the user if they don't exist, or updates their password if they do.
        """
        from validibot.users.models import User

        username = getattr(settings, "SUPERUSER_USERNAME", None)
        password = getattr(settings, "SUPERUSER_PASSWORD", None)
        email = getattr(settings, "SUPERUSER_EMAIL", None)
        name = getattr(settings, "SUPERUSER_NAME", None)

        if not username:
            logger.debug("No SUPERUSER_USERNAME configured, skipping superuser setup")
            self.stdout.write("  No SUPERUSER_USERNAME configured (skipped)")
            return

        self.stdout.write(f"  Setting up superuser '{username}'...")

        user = User.objects.filter(username=username).first()

        if not user:
            logger.info("Creating user '%s'", username)
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
            )
            action = "Created"
        else:
            logger.info("Updating existing user '%s'", username)
            if password:
                user.set_password(password)
                user.save(update_fields=["password"])
            action = "Updated"

        user.name = name
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=["name", "is_staff", "is_superuser"])

        if email:
            user.emailaddress_set.update_or_create(
                email=email,
                defaults={"primary": True, "verified": True},
            )

        self.stdout.write(self.style.SUCCESS(f"  {action} superuser '{username}'"))
