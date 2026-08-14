"""Tests for the complete post-migration application initializer.

The deployment contract depends on one command owning every code-backed data
concern required by a fresh installation.  These tests prevent future setup
work from being split back into operator-only follow-up commands, and verify
that routine deployments can safely skip the first-install sequence.
"""

from unittest.mock import ANY
from unittest.mock import call
from unittest.mock import patch

import pytest
from django.core.management import call_command

from validibot.core.management.commands.initialize_validibot import (
    INITIALIZATION_STATE_KEY,
)
from validibot.core.management.commands.initialize_validibot import (
    INITIALIZATION_VERSION,
)
from validibot.core.management.commands.initialize_validibot import Command
from validibot.core.site_settings import get_site_settings


def test_initializer_runs_every_required_data_command():
    """A fresh deployment must receive all required data without manual seeds."""
    with (
        patch.object(Command, "_is_initialized", return_value=False),
        patch.object(Command, "_mark_initialized") as mark_initialized,
        patch(
            "validibot.core.management.commands.initialize_validibot.call_command"
        ) as managed_call,
    ):
        call_command("initialize_validibot", "--if-needed")

    assert managed_call.call_args_list == [
        call("setup_validibot", "--noinput", stdout=ANY),
        call("sync_help", stdout=ANY),
        call("seed_weather_files", "--strict", stdout=ANY),
    ]
    mark_initialized.assert_called_once_with()


def test_initializer_skips_completed_first_install():
    """Routine deployments must not rewrite initialization data unnecessarily."""
    with (
        patch.object(Command, "_is_initialized", return_value=True),
        patch.object(Command, "_mark_initialized") as mark_initialized,
        patch(
            "validibot.core.management.commands.initialize_validibot.call_command"
        ) as managed_call,
    ):
        call_command("initialize_validibot", "--if-needed")

    managed_call.assert_not_called()
    mark_initialized.assert_not_called()


def test_manual_initializer_refreshes_all_data_when_already_initialized():
    """The recovery command must refresh all data when the guard is omitted."""
    with (
        patch.object(Command, "_is_initialized", return_value=True) as initialized,
        patch.object(Command, "_mark_initialized") as mark_initialized,
        patch(
            "validibot.core.management.commands.initialize_validibot.call_command"
        ) as managed_call,
    ):
        call_command("initialize_validibot")

    initialized.assert_not_called()
    assert [entry.args[0] for entry in managed_call.call_args_list] == [
        "setup_validibot",
        "sync_help",
        "seed_weather_files",
    ]
    mark_initialized.assert_called_once_with()


def test_initializer_does_not_mark_partial_failure_as_complete():
    """A failed seed must leave the next deployment able to retry everything."""
    with (
        patch.object(Command, "_is_initialized", return_value=False),
        patch.object(Command, "_mark_initialized") as mark_initialized,
        patch(
            "validibot.core.management.commands.initialize_validibot.call_command",
            side_effect=[None, None, RuntimeError("weather upload failed")],
        ) as managed_call,
        pytest.raises(RuntimeError, match="weather upload failed"),
    ):
        call_command("initialize_validibot", "--if-needed")

    assert [entry.args[0] for entry in managed_call.call_args_list] == [
        "setup_validibot",
        "sync_help",
        "seed_weather_files",
    ]
    mark_initialized.assert_not_called()


@pytest.mark.django_db
def test_completion_marker_is_versioned_and_preserves_other_site_settings():
    """Completion must persist durably without clobbering unrelated settings."""
    site_settings = get_site_settings()
    site_settings.data = {"operator_setting": "keep-me"}
    site_settings.save(update_fields=["data", "modified"])

    Command()._mark_initialized()

    site_settings.refresh_from_db()
    assert site_settings.data == {
        "operator_setting": "keep-me",
        INITIALIZATION_STATE_KEY: INITIALIZATION_VERSION,
    }
