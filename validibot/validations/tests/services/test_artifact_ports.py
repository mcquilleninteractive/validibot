"""
Tests for artifact-port contract validation.

Artifact ports are the workflow-facing file/data contracts used during launch.
These tests keep the reusable contract service honest without coupling every
case to Cloud Run envelope assembly or trace persistence.
"""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from validibot_shared.validations.envelopes import InputFileItem
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import SupportedMimeType

from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import ResourceFileType
from validibot.validations.services import artifact_ports


@dataclass
class ArtifactPort:
    """Small in-memory port shape for pure contract tests."""

    contract_key: str
    role: str | None = None
    resource_type: str | None = None
    data_format: str | None = None
    media_type: str | None = None
    accepted_data_formats: list[str] | None = None
    accepted_extensions: list[str] | None = None
    accepted_file_types: list[str] | None = None
    accepted_media_types: list[str] | None = None
    allowed_source_scopes: list[str] | None = None
    min_items: int | None = None
    max_items: int | None = None


def _primary_model_port(**overrides) -> ArtifactPort:
    """Return an EnergyPlus model file port with production-like defaults."""

    values = {
        "contract_key": "primary_model",
        "role": "primary-model",
        "data_format": SubmissionDataFormat.ENERGYPLUS_IDF,
        "media_type": SupportedMimeType.ENERGYPLUS_IDF.value,
        "accepted_data_formats": [
            SubmissionDataFormat.ENERGYPLUS_IDF,
            SubmissionDataFormat.ENERGYPLUS_EPJSON,
        ],
        "accepted_media_types": [
            SupportedMimeType.ENERGYPLUS_IDF.value,
            SupportedMimeType.ENERGYPLUS_EPJSON.value,
        ],
        "allowed_source_scopes": [
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        "accepted_extensions": ["idf", "epjson", "json"],
        "min_items": 1,
        "max_items": 1,
    }
    values.update(overrides)
    return ArtifactPort(**values)


def _weather_file_port(**overrides) -> ArtifactPort:
    """Return an EnergyPlus weather file port with production-like defaults."""

    values = {
        "contract_key": "weather_file",
        "role": "weather",
        "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
        "data_format": ResourceFileType.ENERGYPLUS_WEATHER,
        "media_type": SupportedMimeType.ENERGYPLUS_EPW.value,
        "accepted_data_formats": [ResourceFileType.ENERGYPLUS_WEATHER],
        "accepted_media_types": [SupportedMimeType.ENERGYPLUS_EPW.value],
        "allowed_source_scopes": [
            BindingSourceScope.WORKFLOW_RESOURCE,
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        "accepted_extensions": ["epw"],
        "min_items": 1,
        "max_items": 1,
    }
    values.update(overrides)
    return ArtifactPort(**values)


def _shacl_data_graph_port(**overrides) -> ArtifactPort:
    """Return a SHACL data-graph file port with production-like defaults."""

    values = {
        "contract_key": "data_graph",
        "role": "data-graph",
        "data_format": SubmissionDataFormat.TEXT,
        "media_type": SupportedMimeType.RDF_TURTLE.value,
        "accepted_data_formats": [
            SubmissionDataFormat.TEXT,
            SubmissionDataFormat.JSON,
            SubmissionDataFormat.XML,
        ],
        "accepted_media_types": [
            SupportedMimeType.RDF_TURTLE.value,
            SupportedMimeType.RDF_XML.value,
            SupportedMimeType.RDF_JSON_LD.value,
            SupportedMimeType.RDF_N_TRIPLES.value,
            SupportedMimeType.RDF_N_QUADS.value,
        ],
        "allowed_source_scopes": [
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        "accepted_extensions": ["ttl", "rdf", "jsonld", "nt", "nq"],
        "min_items": 1,
        "max_items": 1,
    }
    values.update(overrides)
    return ArtifactPort(**values)


def _schematron_xml_document_port(**overrides) -> ArtifactPort:
    """Return a Schematron XML document file port with production-like defaults."""

    values = {
        "contract_key": "xml_document",
        "role": "xml-document",
        "data_format": SubmissionDataFormat.XML,
        "media_type": SupportedMimeType.APPLICATION_XML.value,
        "accepted_data_formats": [SubmissionDataFormat.XML],
        "accepted_media_types": [
            SupportedMimeType.APPLICATION_XML.value,
            SupportedMimeType.TEXT_XML.value,
        ],
        "allowed_source_scopes": [
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        "accepted_extensions": ["xml"],
        "min_items": 1,
        "max_items": 1,
    }
    values.update(overrides)
    return ArtifactPort(**values)


def _energyplus_sql_output_port(**overrides) -> ArtifactPort:
    """Return an EnergyPlus SQL output artifact port with production defaults."""

    values = {
        "contract_key": "eplusout_sql",
        "role": "simulation-db",
        "data_format": "sqlite",
        "media_type": "application/x-sqlite3",
        "accepted_data_formats": ["sqlite"],
        "accepted_media_types": ["application/x-sqlite3", "application/vnd.sqlite3"],
        "accepted_extensions": ["sql"],
        "min_items": 0,
        "max_items": 1,
    }
    values.update(overrides)
    return ArtifactPort(**values)


def _schematron_svrl_report_port(**overrides) -> ArtifactPort:
    """Return a Schematron SVRL report port with production-like defaults."""

    values = {
        "contract_key": "svrl_report",
        "role": "svrl-report",
        "data_format": SubmissionDataFormat.XML,
        "media_type": SupportedMimeType.APPLICATION_XML.value,
        "accepted_data_formats": [SubmissionDataFormat.XML],
        "accepted_media_types": [
            SupportedMimeType.APPLICATION_XML.value,
            SupportedMimeType.TEXT_XML.value,
        ],
        "accepted_extensions": ["svrl"],
        "min_items": 0,
        "max_items": 1,
    }
    values.update(overrides)
    return ArtifactPort(**values)


def _portfolio_manager_property_results_port(**overrides) -> ArtifactPort:
    """Return the carrier-neutral Portfolio Manager results output port."""

    values = {
        "contract_key": "property_results",
        "role": "portfolio-manager-property-results",
        "data_format": "portfolio_manager_property_results_v1",
        "media_type": "application/json",
        "accepted_data_formats": ["portfolio_manager_property_results_v1"],
        "accepted_media_types": ["application/json"],
        "accepted_extensions": ["json"],
        "min_items": 0,
        "max_items": 1,
    }
    values.update(overrides)
    return ArtifactPort(**values)


class TestArtifactPortContractValidation:
    """Coverage for the reusable artifact-port validation service."""

    def test_input_file_item_accepts_declared_epjson_contract(self):
        """epJSON should pass when the model port declares the format and MIME."""

        artifact_ports.validate_input_file_item(
            port=_primary_model_port(),
            item=InputFileItem(
                name="model.epjson",
                mime_type=SupportedMimeType.ENERGYPLUS_EPJSON,
                role="primary-model",
                port_key="primary_model",
                uri="file:///validibot/input/model.epjson",
                size_bytes=17,
                sha256="a" * 64,
                storage_version="sha256:" + "a" * 64,
            ),
        )

    def test_input_file_item_rejects_unaccepted_media_type(self):
        """MIME validation must be separate from extension/data-format checks."""

        port = _primary_model_port(
            accepted_media_types=[SupportedMimeType.ENERGYPLUS_IDF.value],
        )

        with pytest.raises(ValueError, match="does not accept media type"):
            artifact_ports.validate_input_file_item(
                port=port,
                item=InputFileItem(
                    name="model.epjson",
                    mime_type=SupportedMimeType.ENERGYPLUS_EPJSON,
                    role="primary-model",
                    port_key="primary_model",
                    uri="file:///validibot/input/model.epjson",
                    size_bytes=17,
                    sha256="a" * 64,
                    storage_version="sha256:" + "a" * 64,
                ),
            )

    def test_file_uri_rejects_extensions_outside_the_port_allowlist(self):
        """Extension allowlists should fail before an invalid file reaches a runner."""

        with pytest.raises(ValueError, match=r"expected one of \.epw"):
            artifact_ports.validate_file_uri(
                port=_weather_file_port(),
                uri="file:///validibot/input/weather.txt",
            )

    def test_artifact_ref_accepts_generic_json_for_epjson_uri(self):
        """Generic JSON MIME remains valid for declared epJSON artifacts."""

        artifact_ports.validate_artifact_ref(
            port=_primary_model_port(),
            artifact_ref={
                "uri": "gs://validibot/runs/run-1/model.epjson",
                "data_format": SubmissionDataFormat.ENERGYPLUS_EPJSON,
                "media_type": "application/json",
            },
        )

    def test_artifact_ref_rejects_wrong_explicit_media_type(self):
        """A wrong explicit artifact MIME should not be hidden by the filename."""

        with pytest.raises(ValueError, match="application/pdf"):
            artifact_ports.validate_artifact_ref(
                port=_primary_model_port(),
                artifact_ref={
                    "uri": "gs://validibot/runs/run-1/model.epjson",
                    "data_format": SubmissionDataFormat.ENERGYPLUS_EPJSON,
                    "media_type": "application/pdf",
                },
            )

    def test_artifact_ref_accepts_generic_json_for_jsonld_uri(self):
        """Generic JSON MIME remains valid for declared JSON-LD artifacts."""

        artifact_ports.validate_artifact_ref(
            port=_shacl_data_graph_port(),
            artifact_ref={
                "uri": "gs://validibot/runs/run-1/graph.jsonld",
                "data_format": SubmissionDataFormat.JSON,
                "media_type": "application/json",
            },
        )

    def test_input_file_item_accepts_declared_xml_contract(self):
        """Generic XML artifact ports should infer XML data format and MIME."""

        artifact_ports.validate_input_file_item(
            port=_schematron_xml_document_port(),
            item=InputFileItem(
                name="invoice.xml",
                mime_type=SupportedMimeType.APPLICATION_XML,
                role="xml-document",
                port_key="xml_document",
                uri="file:///validibot/input/invoice.xml",
                size_bytes=17,
                sha256="a" * 64,
                storage_version="sha256:" + "a" * 64,
            ),
        )

    def test_resource_item_enforces_resource_type_and_file_contract(self):
        """Workflow resources must match both domain type and file constraints."""

        artifact_ports.validate_resource_file_item(
            port=_weather_file_port(),
            item=ResourceFileItem(
                id="resource-weather-123",
                name="weather.epw",
                type=ResourceFileType.ENERGYPLUS_WEATHER,
                port_key="weather_file",
                uri="gs://validibot/resources/weather.epw",
                size_bytes=17,
                sha256="a" * 64,
                storage_version="1",
            ),
        )

        with pytest.raises(ValueError, match="expected resource type"):
            artifact_ports.validate_resource_file_item(
                port=_weather_file_port(),
                item=ResourceFileItem(
                    id="resource-library-123",
                    name="library.epw",
                    type="library",
                    port_key="weather_file",
                    uri="gs://validibot/resources/library.epw",
                    size_bytes=17,
                    sha256="a" * 64,
                    storage_version="1",
                ),
            )

    def test_output_artifact_accepts_declared_energyplus_sql_contract(self):
        """Declared output ports validate backend artifact role, MIME, and URI."""

        artifact_ports.validate_output_artifact(
            port=_energyplus_sql_output_port(),
            artifact=SimpleNamespace(
                name="eplusout.sql",
                type="simulation-db",
                mime_type="application/x-sqlite3",
                uri="gs://validibot/runs/run-1/outputs/eplusout.sql",
            ),
        )

    def test_output_artifact_accepts_declared_svrl_report_contract(self):
        """Schematron SVRL reports should validate as XML report artifacts."""

        artifact_ports.validate_output_artifact(
            port=_schematron_svrl_report_port(),
            artifact=SimpleNamespace(
                name="report.svrl",
                type="svrl-report",
                mime_type=SupportedMimeType.APPLICATION_XML.value,
                uri="gs://validibot/runs/run-1/outputs/report.svrl",
            ),
        )

    def test_output_artifact_uses_declared_role_for_domain_json_contract(self):
        """Generic JSON carriers must not be mistaken for EnergyPlus epJSON."""

        artifact_ports.validate_output_artifact(
            port=_portfolio_manager_property_results_port(),
            artifact=SimpleNamespace(
                name="portfolio-manager-property-results.json",
                type="portfolio-manager-property-results",
                mime_type="application/json",
                uri=(
                    "gs://validibot/runs/run-1/outputs/"
                    "portfolio-manager-property-results.json"
                ),
            ),
        )

    def test_output_artifact_rejects_wrong_role(self):
        """Output ports should reject a backend artifact with the wrong role."""

        with pytest.raises(ValueError, match="expected role 'simulation-db'"):
            artifact_ports.validate_output_artifact(
                port=_energyplus_sql_output_port(),
                artifact=SimpleNamespace(
                    name="eplusout.sql",
                    type="timeseries-csv",
                    mime_type="application/x-sqlite3",
                    uri="gs://validibot/runs/run-1/outputs/eplusout.sql",
                ),
            )

    def test_source_scope_and_cardinality_fail_closed(self):
        """Binding scope and artifact count are part of the same launch contract."""

        port = _primary_model_port(
            allowed_source_scopes=[BindingSourceScope.UPSTREAM_ARTIFACT],
        )

        with pytest.raises(ValueError, match="does not allow source scope"):
            artifact_ports.validate_source_scope(
                port,
                BindingSourceScope.SUBMISSION_FILE,
            )

        with pytest.raises(ValueError, match="expected at least 1"):
            artifact_ports.validate_cardinality(
                port=port,
                count=0,
                source_description="submitted files",
            )
