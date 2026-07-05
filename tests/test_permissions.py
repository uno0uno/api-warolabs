"""Unit tests for the permissions catalog (Epic 1 / #E1.1)."""
import pytest

from app.core.permissions import (
    DEFAULT_ROLE_MODULES,
    Module,
    Role,
    normalize_role,
)


class TestRoleEnum:
    def test_canonical_values(self):
        assert Role.OWNER.value == "owner"
        assert Role.ADMIN.value == "admin"
        assert Role.SUPERVISOR.value == "supervisor"
        assert Role.CASHIER.value == "cashier"
        assert Role.KITCHEN.value == "kitchen"
        assert Role.CUSTOMER.value == "customer"

    def test_role_is_str_enum(self):
        # Role(str, Enum) — values compare directly to str
        assert Role.OWNER == "owner"
        assert "owner" == Role.OWNER.value


class TestModuleEnum:
    def test_thirteen_modules_exposed(self):
        # Eventos lives in warotickets.com, so API exposes 13 WARO modules.
        assert len(list(Module)) == 13

    def test_no_duplicate_values(self):
        values = [m.value for m in Module]
        assert len(values) == len(set(values))

    def test_epic_2_contract_modules_present(self):
        expected = {
            "pos", "ventas", "despacho", "menu", "operaciones",
            "abastecimiento", "analitica", "finanzas", "facturacion",
            "equipo", "integraciones", "mi_plan", "mi_negocio",
        }
        assert {m.value for m in Module} == expected


class TestDefaultRoleModules:
    def test_every_role_has_an_entry(self):
        for role in Role:
            assert role in DEFAULT_ROLE_MODULES, f"missing default for {role}"

    def test_owner_gets_all_modules(self):
        assert DEFAULT_ROLE_MODULES[Role.OWNER] == frozenset(Module)

    def test_customer_gets_no_modules(self):
        assert DEFAULT_ROLE_MODULES[Role.CUSTOMER] == frozenset()

    def test_kitchen_only_has_despacho(self):
        assert DEFAULT_ROLE_MODULES[Role.KITCHEN] == frozenset({Module.DESPACHO})

    def test_cashier_only_has_pos(self):
        assert DEFAULT_ROLE_MODULES[Role.CASHIER] == frozenset({Module.POS})

    def test_admin_does_not_get_equipo(self):
        # EQUIPO (membership/role changes) is owner-only by default
        assert Module.EQUIPO not in DEFAULT_ROLE_MODULES[Role.ADMIN]
        assert Module.EQUIPO in DEFAULT_ROLE_MODULES[Role.OWNER]

    def test_cashier_subset_of_supervisor(self):
        assert DEFAULT_ROLE_MODULES[Role.CASHIER].issubset(
            DEFAULT_ROLE_MODULES[Role.SUPERVISOR]
        )

    def test_admin_subset_of_owner(self):
        assert DEFAULT_ROLE_MODULES[Role.ADMIN].issubset(
            DEFAULT_ROLE_MODULES[Role.OWNER]
        )

    def test_defaults_are_frozensets(self):
        for role, modules in DEFAULT_ROLE_MODULES.items():
            assert isinstance(modules, frozenset), (
                f"{role} defaults must be frozenset (immutable)"
            )


class TestNormalizeRole:
    @pytest.mark.parametrize("legacy,expected", [
        ("superuser", Role.OWNER),
        ("employee", Role.CASHIER),
        ("member", Role.CASHIER),
        ("promotor", Role.CASHIER),
    ])
    def test_legacy_mapping(self, legacy, expected):
        assert normalize_role(legacy) is expected

    @pytest.mark.parametrize("canonical", [
        "owner", "admin", "supervisor", "cashier", "kitchen", "customer",
    ])
    def test_canonical_round_trip(self, canonical):
        assert normalize_role(canonical) == Role(canonical)

    def test_idempotent_after_one_pass(self):
        first = normalize_role("superuser")
        second = normalize_role(first.value)
        assert first is second is Role.OWNER

    def test_case_insensitive(self):
        assert normalize_role("SUPERUSER") is Role.OWNER
        assert normalize_role("Owner") is Role.OWNER

    def test_strips_whitespace(self):
        assert normalize_role("  admin  ") is Role.ADMIN

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown role"):
            normalize_role("godmode")

    def test_non_string_raises(self):
        with pytest.raises(ValueError, match="must be a string"):
            normalize_role(None)  # type: ignore[arg-type]
