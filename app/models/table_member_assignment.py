"""Pydantic models for the waiter-attribution family (warocol.com#573).

Three shapes:
- AssignMemberRequest: PATCH body for assigning a member to a table.
- AssignmentHistoryEntry: one row from the history endpoint.
- MemberSummary: lightweight member entry embedded in the aggregator
  responses so cashiers/supervisors don't need access to the EQUIPO-gated
  /tenants/members endpoint.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class AssignMemberRequest(BaseModel):
    """PATCH /api/operaciones/tables/{id}/assigned-member"""
    member_id: Optional[UUID] = Field(
        None,
        description="Member UUID to assign as default waiter. NULL clears the assignment.",
    )


class MemberSummary(BaseModel):
    """Lightweight member entry embedded in /api/{pos,operaciones}/restaurant-context."""
    id: UUID
    name: str
    role: str


class AssignmentHistoryEntry(BaseModel):
    """One row in the GET /api/operaciones/tables/{id}/assignment-history response.

    Snapshots (member_name, member_role) are taken at assignment time and
    survive member deletion. `member_id` may be NULL if the member was
    removed since (FK is ON DELETE SET NULL).
    """
    id: UUID
    member_id: Optional[UUID] = None
    member_name: Optional[str] = None
    member_role: Optional[str] = None
    assigned_at: datetime
    unassigned_at: Optional[datetime] = None
    assigned_by: Optional[UUID] = None
    assigned_by_name: Optional[str] = None


class SetSessionWaiterRequest(BaseModel):
    """PATCH /api/pos/tables/{id}/session-waiter (warocol.com#574)."""
    member_id: Optional[UUID] = Field(
        None,
        description="Member to attribute as serving this session. "
                    "NULL clears (session falls back to the table default via the resolver).",
    )


class OpenTableRequest(BaseModel):
    """POST /tables/{id}/open body (warocol.com#574 extension — optional).

    The endpoint accepted NO body before this. Body is fully optional;
    when omitted, behaviour is identical to today.
    """
    attended_by_member_id: Optional[UUID] = Field(
        None,
        description="Optional initial waiter override for the session being opened. "
                    "If absent, the session inherits the table's assigned_member_id via the resolver. "
                    "Ignored silently when waiter_attribution_enabled is off.",
    )


class SetOrderServedByRequest(BaseModel):
    """PATCH /api/pos/orders/{id}/served-by (warocol.com#575).

    Per-order waiter attribution. Auto-handoff guard enforced server-side:
    only the current `served_by_member_id` or supervisor+ can change it.
    Use the body of POST /pos-cart/{id}/complete to set this at creation
    time (no auto-handoff check there — order didn't exist yet).
    """
    member_id: Optional[UUID] = Field(
        None,
        description="Member to attribute as the server of this order. "
                    "NULL clears (resolver falls back to session/table).",
    )
