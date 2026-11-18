"""Explicit failure types shared by the data service and the publisher.

Every rejection carries a stable machine code *and* a human readable reason so a
caller can report why an operation was refused without having to parse prose.
"""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """Base class for every refusal raised by this package."""

    code = "SERVICE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = {} if details is None else details
        if code is not None:
            self.code = code

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


class InvalidArgument(ServiceError):
    code = "INVALID_ARGUMENT"


class DomainViolation(InvalidArgument):
    """A value is not in the finite domain fixed by the supplied material."""

    code = "DOMAIN_VIOLATION"


# --------------------------------------------------------------------------- #
# Data service
# --------------------------------------------------------------------------- #
class StaleRevision(ServiceError):
    code = "STALE_REVISION"


class SubjectConflict(ServiceError):
    """The same subject_id is already bound to a different record_id."""

    code = "SUBJECT_CONFLICT"


class RecordIdConflict(ServiceError):
    """The same record_id already carries a different subject_id."""

    code = "RECORD_ID_CONFLICT"


class IdenticalRecordConflict(ServiceError):
    """Same record_id / same revision accepted twice with different content."""

    code = "IDENTICAL_RECORD_CONFLICT"


class RetractionAlreadyReplaced(ServiceError):
    code = "RETRACTION_ALREADY_REPLACED"


class AuthorizationConflict(ServiceError):
    """Same authorization operation received twice with different content."""

    code = "AUTHORIZATION_CONFLICT"


# --------------------------------------------------------------------------- #
# Publisher
# --------------------------------------------------------------------------- #
class Infeasible(ServiceError):
    """No level vector can satisfy K / D for the chosen slice."""

    code = "INFEASIBLE"


class DraftConflict(ServiceError):
    code = "DRAFT_CONFLICT"


class ReleaseConflict(ServiceError):
    code = "RELEASE_CONFLICT"


class DraftNotApproved(ServiceError):
    code = "DRAFT_NOT_APPROVED"


class StaleDraft(ServiceError):
    """The live slice changed since the draft was created; re-draft required."""

    code = "STALE_DRAFT"


class ConsentMissing(ServiceError):
    """At release time at least one source subject is no longer authorised."""

    code = "CONSENT_MISSING"


class UnknownDraft(ServiceError):
    code = "UNKNOWN_DRAFT"


class UnknownRelease(ServiceError):
    code = "UNKNOWN_RELEASE"
