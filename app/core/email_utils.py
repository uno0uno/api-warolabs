"""Email normalization for profile lookups and persistence."""


def normalize_email(email: str) -> str:
    """Strip whitespace and lowercase — canonical form for storage and lookup."""
    return email.strip().lower()
