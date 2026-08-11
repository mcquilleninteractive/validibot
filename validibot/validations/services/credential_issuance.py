"""Community-owned extension point for signed credential issuance.

The workflow engine decides when a credential should be issued, while an
installed commercial package owns the private signing implementation. This
registry keeps that dependency one-way: providers import and register with
community; community never imports a provider package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Protocol

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from validibot.validations.models import ValidationStepRun


class CredentialIssuer(Protocol):
    """Issue one credential and return its stable public identifier."""

    def __call__(self, step_run: ValidationStepRun) -> str:
        """Issue or recover the credential for ``step_run``."""


class CredentialIssuanceError(RuntimeError):
    """Report an expected provider failure without exposing provider types."""


class CredentialIssuerUnavailableError(CredentialIssuanceError):
    """Report that no installed package registered an issuer."""


@dataclass
class _CredentialIssuerState:
    """Mutable process state kept explicit for registration and test resets."""

    issuer: CredentialIssuer | None = None
    provider_name: str = ""


_STATE = _CredentialIssuerState()


def register_credential_issuer(
    issuer: CredentialIssuer,
    *,
    provider_name: str,
) -> None:
    """Register the process-wide credential issuer.

    Registration is idempotent for the same callable and name so Django app
    startup remains safe under test reloads. A second distinct provider is a
    configuration error because provider order must not decide which signing
    implementation handles a run.
    """
    if _STATE.issuer is issuer and _STATE.provider_name == provider_name:
        return
    if _STATE.issuer is not None:
        raise ImproperlyConfigured(
            "Only one credential issuer may be registered. "
            f"Already registered: {_STATE.provider_name}; "
            f"attempted: {provider_name}.",
        )
    _STATE.issuer = issuer
    _STATE.provider_name = provider_name


def get_credential_issuer() -> CredentialIssuer | None:
    """Return the installed credential issuer, if one registered at startup."""

    return _STATE.issuer


def issue_registered_credential(step_run: ValidationStepRun) -> str:
    """Issue through the installed provider without knowing its package."""

    issuer = get_credential_issuer()
    if issuer is None:
        raise CredentialIssuerUnavailableError(
            "Signed credential support is not installed on this instance.",
        )
    return issuer(step_run)


def reset_credential_issuer() -> None:
    """Clear process-global registration for isolated registry tests."""
    _STATE.issuer = None
    _STATE.provider_name = ""
