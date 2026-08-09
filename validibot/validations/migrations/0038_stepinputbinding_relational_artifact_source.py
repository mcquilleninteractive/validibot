"""Store upstream artifact bindings as protected relational graph edges.

Portable ``step_key.output_key`` references remain part of the workflow export
contract, but the live graph is relational.  Existing resolvable references are
backfilled before the database constraint is added.  An ambiguous or stale
reference stops the migration so operators can inspect it without losing data.
"""

import django.db.models.deletion
from django.db import migrations
from django.db import models

REFERENCE_PART_COUNT = 2


def backfill_artifact_relations(apps, schema_editor):
    """Resolve legacy portable artifact references into protected foreign keys."""
    StepInputBinding = apps.get_model("validations", "StepInputBinding")
    StepIODefinition = apps.get_model("validations", "StepIODefinition")
    WorkflowStep = apps.get_model("workflows", "WorkflowStep")

    bindings = StepInputBinding.objects.filter(
        source_scope="upstream_artifact",
    ).select_related("workflow_step")
    for binding in bindings.iterator():
        parts = str(binding.source_data_path or "").split(".", 1)
        if len(parts) != REFERENCE_PART_COUNT or not all(parts):
            raise RuntimeError(
                "Cannot migrate upstream artifact binding "
                f"{binding.pk}: expected 'step_key.output_key', got "
                f"{binding.source_data_path!r}."
            )
        source_step = WorkflowStep.objects.filter(
            workflow_id=binding.workflow_step.workflow_id,
            step_key=parts[0],
        ).first()
        if source_step is None:
            raise RuntimeError(
                "Cannot migrate upstream artifact binding "
                f"{binding.pk}: source step {parts[0]!r} was not found."
            )
        owner_filter = models.Q(workflow_step_id=source_step.pk)
        if source_step.validator_id is not None:
            owner_filter |= models.Q(validator_id=source_step.validator_id)
        output_matches = StepIODefinition.objects.filter(
            contract_key=parts[1],
            direction="output",
            io_medium="artifact",
        ).filter(owner_filter)
        output_ids = list(output_matches.values_list("pk", flat=True)[:2])
        if len(output_ids) != 1:
            raise RuntimeError(
                "Cannot migrate upstream artifact binding "
                f"{binding.pk}: {binding.source_data_path!r} resolved to "
                f"{len(output_ids)} output contracts."
            )
        StepInputBinding.objects.filter(pk=binding.pk).update(
            source_step_id=source_step.pk,
            source_output_io_definition_id=output_ids[0],
        )


def clear_artifact_relations(apps, schema_editor):
    """Clear derived relations while preserving portable references on rollback."""
    StepInputBinding = apps.get_model("validations", "StepInputBinding")
    StepInputBinding.objects.filter(source_scope="upstream_artifact").update(
        source_step_id=None,
        source_output_io_definition_id=None,
    )


class Migration(migrations.Migration):
    """Add producer-step and producer-output relations to input bindings."""

    dependencies = [
        ("validations", "0037_validationrun_definition_released_at"),
        ("workflows", "0009_alter_workflow_output_retention"),
    ]

    operations = [
        migrations.AddField(
            model_name="stepinputbinding",
            name="source_output_io_definition",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Artifact output port on source_step. Set only when "
                    "source_scope is upstream_artifact."
                ),
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="artifact_output_bindings",
                to="validations.stepiodefinition",
            ),
        ),
        migrations.AddField(
            model_name="stepinputbinding",
            name="source_step",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Earlier workflow step that produces this artifact-backed "
                    "input. Set only when source_scope is upstream_artifact."
                ),
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="artifact_output_consumers",
                to="workflows.workflowstep",
            ),
        ),
        migrations.RunPython(
            backfill_artifact_relations,
            clear_artifact_relations,
        ),
        migrations.AddConstraint(
            model_name="stepinputbinding",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        source_scope="upstream_artifact",
                        source_step__isnull=False,
                        source_output_io_definition__isnull=False,
                    )
                    | (
                        ~models.Q(source_scope="upstream_artifact")
                        & models.Q(source_step__isnull=True)
                        & models.Q(source_output_io_definition__isnull=True)
                    )
                ),
                name="ck_step_input_binding_artifact_source_fields",
            ),
        ),
    ]
