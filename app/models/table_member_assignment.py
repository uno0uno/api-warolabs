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
