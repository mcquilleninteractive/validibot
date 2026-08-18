"""Record confirmed deletion of one exact validator provider resource."""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.deployments import (
    ExecutionDeploymentResolutionError,
)
from validibot.validations.services.execution.deployments import (
    record_execution_deployment_provider_deleted,
)


class Command(BaseCommand):
    """Persist one resumable cleanup checkpoint after provider absence."""

    help = "Record provider_deleted_at for every row naming one exact resource."

    def add_arguments(self, parser):
        """Require the complete canonical resource name."""
        parser.add_argument("--resource", required=True)
        parser.add_argument(
            "--deactivate-superseded",
            action="store_true",
            help=(
                "Atomically deactivate stale routes for a superseded provider. "
                "Reserved for guarded latest-only reconciliation after provider "
                "absence has been confirmed."
            ),
        )

    def handle(self, *args, **options):
        """Update matching semantic rows without deleting historical identity."""
        deployments = list(
            ValidatorExecutionDeployment.objects.filter(
                provider_resource_name=options["resource"]
            ).order_by("pk")
        )
        if not deployments:
            raise CommandError("No deployment row names that provider resource.")
        try:
            deleted_at = timezone.now()
            with transaction.atomic():
                for deployment in deployments:
                    record_execution_deployment_provider_deleted(
                        deployment,
                        deleted_at=deleted_at,
                        deactivate_superseded=options["deactivate_superseded"],
                    )
        except ExecutionDeploymentResolutionError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Recorded provider deletion on {len(deployments)} row(s)."
            )
        )
