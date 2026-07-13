"""
Back-compat re-exports. Prefer app.core.platform_legal (env-driven source of truth).
"""
from app.core.platform_legal import (  # noqa: F401
    get_waro_legal_entity,
    waro_platform_footer_lines,
    waro_platform_footer_text,
)

# Deprecated: tests may still reference a dict name.
def _entity_dict():
    return get_waro_legal_entity()


# Lazy property-like for old WARO_LEGAL_ENTITY usage in tests
class _WaroLegalProxy(dict):
    def __getitem__(self, key):
        return get_waro_legal_entity()[key]

    def get(self, key, default=None):
        return get_waro_legal_entity().get(key, default)

    def __iter__(self):
        return iter(get_waro_legal_entity())

    def keys(self):
        return get_waro_legal_entity().keys()


WARO_LEGAL_ENTITY = _WaroLegalProxy()
