"""Authoritative WARO mapping between DIAN responsibilities and sale taxes."""
from typing import Any, Dict, Optional


SALES_TAX_PROFILE_SETTINGS: Dict[str, Dict[str, Any]] = {
    'iva_responsible': {
        'tax_regime_id': 1,
        'iva_applicable': True,
        'inc_applicable': False,
    },
    'inc_responsible': {
        'tax_regime_id': 2,
        'iva_applicable': False,
        'inc_applicable': True,
    },
    'non_responsible_iva_inc': {
        'tax_regime_id': 2,
        'iva_applicable': False,
        'inc_applicable': False,
    },
    'non_responsible_iva': {
        'tax_regime_id': 2,
        'iva_applicable': False,
        'inc_applicable': False,
    },
}
ALLOWED_SALES_TAX_PROFILES = {'unconfigured', *SALES_TAX_PROFILE_SETTINGS.keys()}


def settings_for_sales_tax_profile(profile: str) -> Optional[Dict[str, Any]]:
    return SALES_TAX_PROFILE_SETTINGS.get(profile)


def sales_tax_profile_is_aligned(
    profile: str,
    type_organization_id: int,
    tax_regime_id: int,
    inc_applicable: bool,
    iva_applicable: bool,
) -> bool:
    settings = settings_for_sales_tax_profile(profile)
    if settings is None:
        return False
    if profile == 'non_responsible_iva_inc' and type_organization_id != 2:
        return False
    return (
        tax_regime_id == settings['tax_regime_id']
        and bool(inc_applicable) is settings['inc_applicable']
        and bool(iva_applicable) is settings['iva_applicable']
    )
