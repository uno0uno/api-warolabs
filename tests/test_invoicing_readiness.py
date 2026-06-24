"""
Tests for invoicing readiness service and gate (issue #130).

Covers:
  - readiness service builds the correct payload from each row shape
  - readiness gate raises 403 when not ready, lets through when ready
  - emit endpoints reject without ever calling api-facturacion when not ready
"""
import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from app.services import invoicing_readiness_service


_TENANT_ID = UUID('93b3e582-34fa-44a6-8d0f-bf82a3608727')


def _row(
    dev_flag_enabled: bool = True,
    tenant_slug: str = 'acme-sas',
    nit: str = '900123456',
    business_name: str = 'ACME SAS',
    phone: str = '3001234567',
    email: str = 'contacto@acme.co',
    matias_company_id: str = '8d4f2f79-4a4e-4d7d-bf07-7bd61c9e4f37',
    active_resolution: bool = True,
    type_organization_id: int = 1,
    tax_regime_id: int = 2,
    tax_level_id: int = 5,
    inc_applicable: bool = False,
    iva_applicable: bool = True,
):
    return {
        'dev_flag_enabled':  dev_flag_enabled,
        'tenant_slug':       tenant_slug,
        'nit':               nit,
        'business_name':     business_name,
        'phone':             phone,
        'email':             email,
        'matias_company_id':  matias_company_id,
        'type_organization_id': type_organization_id,
        'tax_regime_id':     tax_regime_id,
        'tax_level_id':      tax_level_id,
        'active_resolution': active_resolution,
        'inc_applicable':    inc_applicable,
        'iva_applicable':    iva_applicable,
    }


class _FakeConnCtx:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=self._row)
        return conn

    async def __aexit__(self, *_):
        return False


def _patch_db(row):
    return patch(
        'app.services.invoicing_readiness_service.get_db_connection',
        lambda *args, **kwargs: _FakeConnCtx(row),
    )


class TestReadinessService:
    """Unit tests for `invoicing_readiness_service.get_readiness`."""

    @pytest.mark.asyncio
    async def test_happy_path_all_five_checks_true(self):
        with _patch_db(_row()):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload is not None
        assert payload['ready'] is True
        assert payload['checks'] == {
            'dev_flag_enabled':     True,
            'fiscal_data_complete': True,
            'active_resolution':    True,
            'taxes_configured':     True,
            'tax_requirement_satisfied': True,
            'matias_company_id_configured': True,
        }
        assert payload['missing'] == []

    @pytest.mark.asyncio
    async def test_taxes_configured_via_inc_alone(self):
        # Restaurant under INC (8%) without IVA → still considered configured
        with _patch_db(_row(inc_applicable=True, iva_applicable=False)):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['checks']['taxes_configured'] is True
        assert payload['checks']['tax_requirement_satisfied'] is True
        assert payload['ready'] is True

    @pytest.mark.asyncio
    async def test_responsable_without_taxes_blocks_ready(self):
        with _patch_db(_row(
            type_organization_id=1,
            tax_regime_id=1,
            tax_level_id=48,
            inc_applicable=False,
            iva_applicable=False,
        )):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['checks']['taxes_configured'] is False
        assert payload['checks']['tax_requirement_satisfied'] is False
        assert payload['ready'] is False
        assert any('requiere INC/IVA activo o un escenario sin impuesto válido' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_no_responsable_persona_natural_without_taxes_can_be_ready(self):
        with _patch_db(_row(
            type_organization_id=2,
            tax_regime_id=2,
            tax_level_id=5,
            inc_applicable=False,
            iva_applicable=False,
        )):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['checks']['taxes_configured'] is False
        assert payload['checks']['tax_requirement_satisfied'] is True
        assert payload['ready'] is True
        assert payload['missing'] == []

    @pytest.mark.asyncio
    async def test_incomplete_no_responsable_persona_natural_still_blocks_ready(self):
        with _patch_db(_row(
            nit=None,
            type_organization_id=2,
            tax_regime_id=2,
            tax_level_id=5,
            inc_applicable=False,
            iva_applicable=False,
        )):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['checks']['fiscal_data_complete'] is False
        assert payload['checks']['tax_requirement_satisfied'] is True
        assert payload['ready'] is False
        assert any('NIT' in m for m in payload['missing'])
        assert not any('requiere INC/IVA activo o un escenario sin impuesto válido' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_production_missing_matias_company_id_blocks_ready(self, monkeypatch):
        from app import config as config_module

        monkeypatch.setattr(config_module.settings, 'matias_environment_id', 1)
        monkeypatch.setattr(config_module.settings, 'matias_habilitacion_tenant_ids', '')
        monkeypatch.setattr(config_module.settings, 'matias_sandbox_tenant_ids', '')

        with _patch_db(_row(matias_company_id='  ')):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['ready'] is False
        assert payload['checks']['matias_company_id_configured'] is False
        assert any('client_uuid' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_habilitacion_missing_matias_company_id_blocks_ready(self, monkeypatch):
        from app import config as config_module

        monkeypatch.setattr(config_module.settings, 'matias_environment_id', 1)
        monkeypatch.setattr(config_module.settings, 'matias_habilitacion_tenant_ids', str(_TENANT_ID))
        monkeypatch.setattr(config_module.settings, 'matias_sandbox_tenant_ids', '')

        with _patch_db(_row(matias_company_id=None)):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['ready'] is False
        assert payload['checks']['matias_company_id_configured'] is False
        assert any('client_uuid' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_sandbox_missing_matias_company_id_blocks_ready(self, monkeypatch):
        from app import config as config_module

        monkeypatch.setattr(config_module.settings, 'matias_environment_id', 1)
        monkeypatch.setattr(config_module.settings, 'matias_habilitacion_tenant_ids', '')
        monkeypatch.setattr(config_module.settings, 'matias_sandbox_tenant_ids', 'acme-sas')

        with _patch_db(_row(matias_company_id=None)):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['ready'] is False
        assert payload['checks']['matias_company_id_configured'] is False
        assert any('client_uuid' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_dev_flag_off_blocks(self):
        with _patch_db(_row(dev_flag_enabled=False)):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['ready'] is False
        assert payload['checks']['dev_flag_enabled'] is False
        assert any('deshabilitada por el equipo' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_missing_nit_marks_fiscal_incomplete(self):
        with _patch_db(_row(nit=None)):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['ready'] is False
        assert payload['checks']['fiscal_data_complete'] is False
        assert any('NIT' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_missing_business_name(self):
        with _patch_db(_row(business_name=None)):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['checks']['fiscal_data_complete'] is False
        assert any('razón social' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_missing_phone(self):
        with _patch_db(_row(phone=None)):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['checks']['fiscal_data_complete'] is False
        assert any('teléfono' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_missing_email(self):
        with _patch_db(_row(email=None)):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['checks']['fiscal_data_complete'] is False
        assert any('email' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_no_active_resolution(self):
        with _patch_db(_row(active_resolution=False)):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['ready'] is False
        assert payload['checks']['active_resolution'] is False
        assert any('resolución DIAN' in m for m in payload['missing'])

    @pytest.mark.asyncio
    async def test_all_four_failing_lists_four_reasons(self):
        with _patch_db(_row(
            dev_flag_enabled=False,
            nit=None,
            active_resolution=False,
            inc_applicable=False,
            iva_applicable=False,
        )):
            payload = await invoicing_readiness_service.get_readiness(_TENANT_ID)

        assert payload['ready'] is False
        assert len(payload['missing']) == 4

    @pytest.mark.asyncio
    async def test_unknown_tenant_returns_none(self):
        with _patch_db(None):
            payload = await invoicing_readiness_service.get_readiness(uuid4())

        assert payload is None


class TestEmitGate:
    """The emission endpoints must 403 when readiness fails — and never call
    api-facturacion / Matías. The gate is a `Depends`, not deep in the
    service, so we don't need a full test client to assert this contract;
    we exercise the dependency directly."""

    @pytest.mark.asyncio
    async def test_dependency_raises_403_when_not_ready(self):
        from app.core.dependencies import require_invoicing_ready
        from fastapi import HTTPException

        request = MagicMock()
        request.state.session_context = MagicMock()
        request.state.session_context.is_valid = True
        request.state.session_context.tenant_id = _TENANT_ID

        with patch(
            'app.services.invoicing_readiness_service.get_readiness',
            new=AsyncMock(return_value={
                'ready':   False,
                'checks':  {
                    'dev_flag_enabled':     False,
                    'fiscal_data_complete': True,
                    'active_resolution':    True,
                    'taxes_configured':     True,
                    'tax_requirement_satisfied': True,
                    'matias_company_id_configured': True,
                },
                'missing': ['Facturación electrónica deshabilitada por el equipo de WARO'],
            }),
        ), patch('app.core.middleware.require_valid_session', return_value=request.state.session_context):
            with pytest.raises(HTTPException) as exc:
                await require_invoicing_ready(request)

        assert exc.value.status_code == 403
        assert exc.value.detail['error'] == 'tenant_not_ready_for_invoicing'
        assert 'missing' in exc.value.detail
        assert 'checks' in exc.value.detail

    @pytest.mark.asyncio
    async def test_dependency_passes_through_when_ready(self):
        from app.core.dependencies import require_invoicing_ready

        request = MagicMock()
        request.state.session_context = MagicMock()
        request.state.session_context.is_valid = True
        request.state.session_context.tenant_id = _TENANT_ID

        ready_payload = {
            'ready':   True,
            'checks':  {
                'dev_flag_enabled':     True,
                'fiscal_data_complete': True,
                'active_resolution':    True,
                'taxes_configured':     True,
                'tax_requirement_satisfied': True,
                'matias_company_id_configured': True,
            },
            'missing': [],
        }
        with patch(
            'app.services.invoicing_readiness_service.get_readiness',
            new=AsyncMock(return_value=ready_payload),
        ), patch('app.core.middleware.require_valid_session', return_value=request.state.session_context):
            result = await require_invoicing_ready(request)

        assert result == ready_payload


class TestEmitEndpointSmokeTest:
    """End-to-end smoke: the emit endpoint exists, requires auth, and never
    crashes the app on import. Behaviour assertions are covered by the unit
    tests above."""

    @pytest.mark.asyncio
    async def test_emit_invoice_requires_auth(self, client: AsyncClient):
        response = await client.post(f'/orders/{uuid4()}/invoice')
        assert response.status_code in (401, 403, 422)
