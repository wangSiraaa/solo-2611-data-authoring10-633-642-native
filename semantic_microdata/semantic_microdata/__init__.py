"""Public API of the purpose-authorised data service and anonymous publisher.

Typical use::

    from semantic_microdata import (
        DataService, MicrodataPublisher, load_material,
    )

    material = load_material("case.json")
    service = DataService("state.db", material.domains)
    publisher = MicrodataPublisher(service, material.domains, "releases/")

    draft = publisher.draft("req-1", k=8, purpose="research", K=2, D=2,
                            weights=[1, 0, 0])
    publisher.review(draft["draft_id"], "approve", "checked")
    release = publisher.release(draft["draft_id"], "rel-1")
"""

from .data_service import DataService, SliceRow, last_seq_at, view_at
from .domains import (
    QUASI_COLUMNS,
    SENSITIVE_COLUMN,
    Domains,
    Hierarchy,
    domains_from_material,
    material_rows,
)
from .errors import (
    AuthorizationConflict,
    ConsentMissing,
    DomainViolation,
    DraftConflict,
    DraftNotApproved,
    IdenticalRecordConflict,
    Infeasible,
    InvalidArgument,
    RecordIdConflict,
    ReleaseConflict,
    ServiceError,
    StaleDraft,
    StaleRevision,
    SubjectConflict,
    UnknownDraft,
    UnknownRelease,
)
from .material import Material, load_material
from .optimizer import (
    GroupStat,
    Selection,
    group_stats,
    infeasibility_report,
    is_feasible,
    level_vectors,
    loss_of,
    normalised_loss,
    optimal_vectors,
    select_best,
    validate_params,
)
from .plan import PlanExecutor, load_plan
from .publisher import MicrodataPublisher
from .verify_hook import independent_check

__all__ = [
    "AuthorizationConflict",
    "ConsentMissing",
    "DataService",
    "DomainViolation",
    "Domains",
    "DraftConflict",
    "DraftNotApproved",
    "GroupStat",
    "Hierarchy",
    "IdenticalRecordConflict",
    "Infeasible",
    "InvalidArgument",
    "Material",
    "MicrodataPublisher",
    "PlanExecutor",
    "QUASI_COLUMNS",
    "RecordIdConflict",
    "ReleaseConflict",
    "SENSITIVE_COLUMN",
    "Selection",
    "ServiceError",
    "SliceRow",
    "StaleDraft",
    "StaleRevision",
    "SubjectConflict",
    "UnknownDraft",
    "UnknownRelease",
    "domains_from_material",
    "group_stats",
    "independent_check",
    "infeasibility_report",
    "is_feasible",
    "last_seq_at",
    "level_vectors",
    "load_material",
    "load_plan",
    "loss_of",
    "material_rows",
    "normalised_loss",
    "optimal_vectors",
    "select_best",
    "validate_params",
    "view_at",
]

__version__ = "1.0.0"
