"""
Tests for workflow enrichment functions.

The MCP server enriches raw API workflow responses with computed fields
that help agents make informed decisions. These unit tests verify the
enrichment functions in isolation, without any network mocking needed.

Covered:
- File type → extension mapping (_build_accepted_extensions)
- Pricing summary generation (_build_pricing)
- Validation step summary generation (_build_validation_summary)
- Full enrichment pipeline (_enrich_workflow_for_agent)
"""

from __future__ import annotations

from validibot_mcp.tools.workflows import (
    _build_accepted_extensions,
    _build_pricing,
    _build_validation_summary,
    _enrich_workflow_for_agent,
)

# ── File type to extension mapping ─────────────────────────────────────
# Verifies that logical file type codes from the API are correctly mapped
# to the concrete file extensions that agents can submit.


class TestBuildAcceptedExtensions:
    """Verify the file type → extension mapping."""

    def test_json_maps_to_json_and_epjson(self):
        """JSON file type should include .json and .epjson (EnergyPlus JSON)."""
        result = _build_accepted_extensions(["json"])
        assert ".json" in result
        assert ".epjson" in result

    def test_text_maps_to_idf_txt_csv_yaml(self):
        """TEXT file type covers plain text formats including IDF files."""
        result = _build_accepted_extensions(["text"])
        assert ".idf" in result
        assert ".txt" in result
        assert ".csv" in result

    def test_multiple_types_are_deduplicated(self):
        """When multiple types share extensions, results are deduplicated."""
        result = _build_accepted_extensions(["yaml", "text"])
        # Both yaml and text include .yaml/.yml — should appear only once
        assert result.count(".yaml") == 1

    def test_unknown_type_returns_empty(self):
        """Unknown file type codes should produce no extensions."""
        result = _build_accepted_extensions(["unknown_format"])
        assert result == []

    def test_case_insensitive(self):
        """File type codes should be matched case-insensitively."""
        result = _build_accepted_extensions(["JSON"])
        assert ".json" in result

    def test_empty_input(self):
        """Empty allowed_file_types should return empty list."""
        assert _build_accepted_extensions([]) == []

    def test_results_are_sorted(self):
        """Extensions should be sorted alphabetically for consistency."""
        result = _build_accepted_extensions(["json", "text", "xml"])
        assert result == sorted(result)


# ── Pricing summary ───────────────────────────────────────────────────
# Verifies that billing mode and price are translated into a structured
# summary that agents can use for decision-making.


class TestBuildPricing:
    """Verify pricing summary generation."""

    def test_author_pays(self):
        """Author-pays workflows should show no payment required."""
        result = _build_pricing({"agent_billing_mode": "AUTHOR_PAYS"})
        assert result["mode"] == "AUTHOR_PAYS"
        assert result["payment_required"] is False
        assert "price_cents" not in result

    def test_agent_pays_x402(self):
        """Agent-pays-x402 should show the price and require payment."""
        result = _build_pricing(
            {
                "agent_billing_mode": "AGENT_PAYS_X402",
                "agent_price_cents": 250,
            }
        )
        assert result["mode"] == "AGENT_PAYS_X402"
        assert result["payment_required"] is True
        assert result["price_cents"] == 250
        assert result["price_display"] == "$2.50 USD"
        assert result["currency"] == "usd"

    def test_defaults_to_author_pays(self):
        """Missing billing mode should default to AUTHOR_PAYS."""
        result = _build_pricing({})
        assert result["mode"] == "AUTHOR_PAYS"
        assert result["payment_required"] is False


# ── Validation summary ────────────────────────────────────────────────
# Verifies that step information is compiled into a human-readable string.


class TestBuildValidationSummary:
    """Verify validation step summary generation."""

    def test_no_steps(self):
        """Workflows with no steps should say so."""
        result = _build_validation_summary({"steps": []})
        assert "no validation steps" in result

    def test_single_step(self):
        """Single step should use singular 'step' word."""
        workflow = {
            "steps": [
                {
                    "name": "Schema Check",
                    "validator": {
                        "name": "JSON Schema",
                        "validation_type": "JSON_SCHEMA",
                    },
                },
            ],
        }
        result = _build_validation_summary(workflow)
        assert "1 validation step" in result
        assert "Schema Check" in result

    def test_multiple_steps(self):
        """Multiple steps should use plural 'steps' word."""
        workflow = {
            "steps": [
                {
                    "name": "Schema Check",
                    "validator": {
                        "name": "JSON Schema",
                        "validation_type": "JSON_SCHEMA",
                    },
                },
                {
                    "name": "Simulation",
                    "validator": {
                        "name": "EnergyPlus",
                        "validation_type": "ENERGYPLUS",
                    },
                },
            ],
        }
        result = _build_validation_summary(workflow)
        assert "2 validation steps" in result

    def test_action_steps(self):
        """Action steps (no validator) should show the action type."""
        workflow = {
            "steps": [
                {
                    "name": "Send Notification",
                    "validator": None,
                    "action_type": "SLACK_MESSAGE",
                },
            ],
        }
        result = _build_validation_summary(workflow)
        assert "SLACK_MESSAGE" in result


# ── Full enrichment pipeline ──────────────────────────────────────────
# Verifies that _enrich_workflow_for_agent adds all computed fields
# without mutating the original workflow dict.


class TestEnrichWorkflowForAgent:
    """Verify the complete enrichment pipeline."""

    def test_adds_all_computed_fields(self):
        """Enrichment should add accepted_extensions, pricing, and summary."""
        workflow = {
            "slug": "test",
            "allowed_file_types": ["json"],
            "agent_billing_mode": "AUTHOR_PAYS",
            "steps": [],
        }
        result = _enrich_workflow_for_agent(workflow)
        assert "accepted_extensions" in result
        assert "pricing" in result
        assert "validation_summary" in result

    def test_does_not_mutate_original(self):
        """Enrichment should return a new dict, not modify the input."""
        workflow = {
            "slug": "test",
            "allowed_file_types": ["json"],
            "agent_billing_mode": "AUTHOR_PAYS",
            "steps": [],
        }
        original_keys = set(workflow.keys())
        _enrich_workflow_for_agent(workflow)
        assert set(workflow.keys()) == original_keys

    def test_preserves_existing_fields(self):
        """All original workflow fields should be preserved in the output."""
        workflow = {
            "slug": "test",
            "name": "Test Workflow",
            "allowed_file_types": ["json"],
            "agent_billing_mode": "AUTHOR_PAYS",
            "steps": [],
            "custom_field": "should_be_preserved",
        }
        result = _enrich_workflow_for_agent(workflow)
        assert result["slug"] == "test"
        assert result["name"] == "Test Workflow"
        assert result["custom_field"] == "should_be_preserved"
