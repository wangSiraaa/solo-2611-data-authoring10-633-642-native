"""Self-contained fictional teaching toolkit: quiz gradebook + exam seating."""

from .gradebook import (
    Gradebook,
    GradebookError,
    IdempotencyConflict,
    IngestRejected,
    RuleError,
    compute_product,
    open_gradebook,
)
from .rational import Rat
from .seater import ConfirmRejected, Seater, SeatingProtocolError, open_seater
from .solver import SeatingError, greedy_first_available, solve
from .store import connect
from .verify import verify_database

__all__ = [
    "Gradebook", "GradebookError", "IdempotencyConflict", "IngestRejected",
    "RuleError", "compute_product", "open_gradebook", "Rat",
    "ConfirmRejected", "Seater", "SeatingProtocolError", "open_seater",
    "SeatingError", "greedy_first_available", "solve", "connect",
    "verify_database",
]
