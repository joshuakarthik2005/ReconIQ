"""
Common data schemas for the AI Finance Controller.

Defines the canonical types used across every pipeline stage:
  - InternalTransaction / ExternalTransaction: the two sides being reconciled
  - MatchResult / AuditEntry / ExceptionRecord: pipeline outputs
  - MatchPath / GLCategory: enumerations for resolution path and GL buckets
  - ParseError: ingestion-time failures (persisted for Part 5 consumption)

Amount fields use Decimal (not float) to eliminate floating-point comparison
bugs in tolerance-band matching.  All amounts are normalised to 2 dp with
ROUND_HALF_UP at parse time.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict, Any, List
from enum import Enum


# ── Enums ─────────────────────────────────────────────────────

class MatchPath(Enum):
    """How a record was ultimately resolved."""
    RULE = "rule"
    LLM = "llm"
    EXCEPTION = "exception"
    UNPROCESSED = "unprocessed"


class GLCategory(Enum):
    """Chart-of-accounts style GL classification buckets."""
    SETTLEMENT = "Settlement Income"
    GATEWAY_FEE = "Gateway Processing Fee"
    REFUND = "Customer Refund"
    TAX_ADJUSTMENT = "Tax Adjustment (GST/TDS)"
    CHARGEBACK = "Chargeback"
    BANK_CHARGE = "Bank Service Charge"
    INTEREST_INCOME = "Interest Income"
    MISCELLANEOUS = "Miscellaneous"
    UNCLASSIFIED = "Unclassified"


# ── Transaction Schemas ──────────────────────────────────────

@dataclass
class InternalTransaction:
    """A transaction record from our internal payment/settlement system."""
    txn_id: str
    reference_id: str
    amount: Decimal
    currency: str
    txn_type: str               # payment | settlement | refund
    date: str                   # YYYY-MM-DD
    merchant_name: str
    merchant_category: str
    payment_method: str         # UPI | credit_card | debit_card | netbanking | wallet
    status: str                 # captured | settled | processed
    description: str = ""


@dataclass
class ExternalTransaction:
    """Normalized representation of a bank-statement / ledger entry."""
    ext_id: str
    reference_id: Optional[str]
    amount: Decimal
    date: str                   # YYYY-MM-DD (normalized from source format)
    description: str
    source_format: str          # A | B | C
    raw_description: str = ""   # Always retains original narrative/description text
    original_data: Dict[str, Any] = field(default_factory=dict)


# ── Pipeline Output Schemas ──────────────────────────────────

@dataclass
class MatchResult:
    """Outcome of matching a single internal transaction."""
    internal_id: str
    external_id: Optional[str] = None
    match_path: MatchPath = MatchPath.UNPROCESSED
    confidence: float = 0.0
    rule_name: Optional[str] = None
    reasoning: str = ""
    gl_category: Optional[GLCategory] = None
    is_partial_refund: bool = False
    timestamp: str = ""


@dataclass
class AuditEntry:
    """Immutable audit-trail entry for one reconciliation decision."""
    record_id: str
    record_type: str            # internal | external
    resolution_path: str        # rule | llm | exception
    detail: str                 # which rule fired, or LLM reasoning text
    matched_to: Optional[str] = None
    confidence: float = 0.0
    gl_category: str = ""
    rule_name: str = ""
    timestamp: str = ""


@dataclass
class ExceptionRecord:
    """A record that could not be confidently resolved."""
    record_id: str
    record_type: str            # internal | external
    reason: str                 # specific, human-readable (never generic)
    attempted_matches: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: str = ""


@dataclass
class ParseError:
    """A row / entry that failed to parse during ingestion.

    Persisted as JSON so Part 5 exception handling can consume them
    alongside matching exceptions.
    """
    source_file: str
    row_number: int             # 1-based (header = row 1 for CSV)
    raw_data: Any               # original row dict or JSON entry
    error_message: str
    timestamp: str = ""
