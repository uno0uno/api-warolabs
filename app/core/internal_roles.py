from typing import Optional


LEGACY_INTERNAL_TEAM_ROLES = ("superuser", "admin", "employee", "member", "promotor")


def is_legacy_internal_team_role(role: Optional[str]) -> bool:
    return role in LEGACY_INTERNAL_TEAM_ROLES
