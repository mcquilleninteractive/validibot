"""Replace validator-wide type hints with explicit typed input ports."""

from django.db import migrations
from django.db import models

PORT_CONTRACTS = {
    "BASIC": ("document", "Document", ["json", "xml"], ["json", "xml"]),
    "JSON_SCHEMA": ("json_document", "JSON document", ["json"], ["json"]),
    "XML_SCHEMA": ("xml_document", "XML document", ["xml"], ["xml"]),
    "SCHEMATRON": ("xml_document", "XML document", ["xml"], ["xml"]),
    "SHACL": (
        "data_graph",
        "RDF data graph",
        ["text", "json", "xml"],
        ["ttl", "rdf", "jsonld", "nt", "nq"],
    ),
    "TABULAR": ("table_document", "Table document", ["text"], ["csv", "tsv"]),
    "AI_ASSIST": ("document", "Document", ["json", "text"], ["json", "txt"]),
    "ENERGYPLUS": (
        "primary_model",
        "Primary Model",
        ["text", "json"],
        ["idf", "epjson", "json"],
    ),
    "THERM": ("therm_model", "THERM model", ["xml", "binary"], ["thmx", "thmz"]),
    "PDF": ("pdf_document", "PDF document", ["pdf"], ["pdf"]),
    "PORTFOLIO_MANAGER": (
        "benchmark_report",
        "ENERGY STAR Portfolio Manager report",
        ["binary", "xml"],
        ["xls", "xlsx", "xml", "zip"],
    ),
}


def _custom_contract(validator):
    formats = list(validator.supported_data_formats or [])
    if "yaml" in formats:
        return "document", "Document", ["yaml"], ["yaml", "yml"]
    return "document", "Document", ["json"], ["json"]


def populate_typed_contracts(apps, schema_editor):
    """Give every validator and existing step an explicit source contract."""

    Validator = apps.get_model("validations", "Validator")
    StepIODefinition = apps.get_model("validations", "StepIODefinition")
    StepInputBinding = apps.get_model("validations", "StepInputBinding")
    WorkflowStep = apps.get_model("workflows", "WorkflowStep")

    for validator in Validator.objects.all().iterator():
        if validator.validation_type == "FMU":
            port_key = "fmu_model"
            label = "FMU Model"
            file_types = []
            extensions = ["fmu"]
            allowed_scopes = ["workflow_resource", "system"]
            data_format = "fmu"
            media_type = "application/vnd.fmi.fmu"
            resource_type = "fmu_model"
        else:
            contract = PORT_CONTRACTS.get(validator.validation_type)
            if validator.validation_type == "CUSTOM_VALIDATOR":
                contract = _custom_contract(validator)
            if contract is None:
                raise RuntimeError(
                    "Validator has no typed input contract: "
                    f"{validator.slug} v{validator.version}"
                )
            port_key, label, file_types, extensions = contract
            allowed_scopes = ["submission_file", "upstream_artifact"]
            if validator.validation_type == "JSON_SCHEMA":
                allowed_scopes.append("submission_metadata")
            data_format = {
                "JSON_SCHEMA": "json",
                "XML_SCHEMA": "xml",
                "SCHEMATRON": "xml",
                "PDF": "pdf",
                "TABULAR": "csv",
                "CUSTOM_VALIDATOR": (
                    "yaml"
                    if "yaml" in (validator.supported_data_formats or [])
                    else "json"
                ),
            }.get(validator.validation_type, "")
            media_type = {
                "JSON_SCHEMA": "application/json",
                "XML_SCHEMA": "application/xml",
                "SCHEMATRON": "application/xml",
                "PDF": "application/pdf",
                "TABULAR": "text/csv",
                "CUSTOM_VALIDATOR": (
                    "application/yaml" if data_format == "yaml" else "application/json"
                ),
            }.get(validator.validation_type, "")
            resource_type = ""

        port, _created = StepIODefinition.objects.get_or_create(
            validator=validator,
            contract_key=port_key,
            direction="input",
            defaults={
                "native_name": port_key,
                "label": label,
                "description": f"Typed document input for {validator.name}.",
                "data_type": "artifact_ref",
                "io_medium": "artifact",
                "artifact_kind": "file",
                "media_type": media_type,
                "data_format": data_format,
                "accepted_data_formats": [data_format] if data_format else [],
                "accepted_media_types": [media_type] if media_type else [],
                "accepted_file_types": file_types,
                "accepted_extensions": extensions,
                "allowed_source_scopes": allowed_scopes,
                "default_source_strategy": (
                    "workflow_resource_default"
                    if validator.validation_type == "FMU"
                    else "submitted_file_first"
                ),
                "envelope_channel": "input_files",
                "resource_type": resource_type,
                "role": port_key.replace("_", "-"),
                "min_items": 1,
                "max_items": 1,
                "origin_kind": "catalog",
                "source_kind": "payload_path",
                "is_path_editable": False,
                "on_missing": "error",
                "order": 1,
            },
        )
        metadata = dict(port.metadata or {})
        metadata_extensions = metadata.pop("accepted_extensions", [])
        port.accepted_file_types = file_types
        port.accepted_extensions = extensions or metadata_extensions
        port.allowed_source_scopes = allowed_scopes
        port.metadata = metadata
        port.save(
            update_fields=[
                "accepted_extensions",
                "accepted_file_types",
                "allowed_source_scopes",
                "metadata",
            ]
        )

        for step in WorkflowStep.objects.filter(validator=validator).iterator():
            if StepInputBinding.objects.filter(
                workflow_step=step,
                io_definition=port,
            ).exists():
                continue
            source_scope = allowed_scopes[0]
            source_data_path = (
                "primary" if source_scope == "submission_file" else resource_type
            )
            StepInputBinding.objects.create(
                workflow_step=step,
                io_definition=port,
                source_scope=source_scope,
                source_data_path=source_data_path,
                is_required=True,
            )


def restore_validator_type_summaries(apps, schema_editor):
    """Reconstruct removed summary fields if the schema migration is reversed."""

    Validator = apps.get_model("validations", "Validator")
    StepIODefinition = apps.get_model("validations", "StepIODefinition")
    for validator in Validator.objects.all().iterator():
        ports = StepIODefinition.objects.filter(
            validator=validator,
            direction="input",
            io_medium="artifact",
        )
        file_types = []
        data_formats = []
        for port in ports:
            file_types.extend(port.accepted_file_types or [])
            data_formats.extend(port.accepted_data_formats or [])
        validator.supported_file_types = list(dict.fromkeys(file_types))
        validator.supported_data_formats = list(dict.fromkeys(data_formats))
        validator.save(
            update_fields=["supported_file_types", "supported_data_formats"]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0041_alter_validator_supported_file_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="stepiodefinition",
            name="accepted_extensions",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Accepted lowercase filename extensions without leading dots."
                ),
            ),
        ),
        migrations.AddField(
            model_name="stepiodefinition",
            name="accepted_file_types",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Accepted primary-submission carrier types for artifact input "
                    "ports."
                ),
            ),
        ),
        migrations.RunPython(
            populate_typed_contracts,
            restore_validator_type_summaries,
        ),
        migrations.RemoveField(
            model_name="validator",
            name="supported_data_formats",
        ),
        migrations.RemoveField(
            model_name="validator",
            name="supported_file_types",
        ),
    ]
