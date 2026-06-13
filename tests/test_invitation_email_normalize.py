"""Pydantic normalizes invitation email on model construction."""
from app.models.invitation import InvitationRole, SendInvitationRequest


def test_send_invitation_request_normalizes_email():
    payload = SendInvitationRequest(
        email="Foo@Bar.com",
        phone="3001234567",
        name="Test",
        role=InvitationRole.ADMIN,
    )
    assert payload.email == "foo@bar.com"
