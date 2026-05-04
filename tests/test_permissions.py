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
    def test_seventeen_modules_exposed(self):
        assert len(list(Module)) == 17

    def test_no_duplicate_values(self):
        values = [m.value for m in Module]
        assert len(values) == len(set(values))


class TestDefaultRoleModules:
    def test_every_role_has_an_entry(self):
        for role in Role:
            assert role in DEFAULT_ROLE_MODULES, f"missing default for {role}"

    def test_owner_gets_all_modules(self):
        assert DEFAULT_ROLE_MODULES[Role.OWNER] == frozenset(Module)

    def test_customer_gets_no_modules(self):
        assert DEFAULT_ROLE_MODULES[Role.CUSTOMER] == frozenset()

    def test_kitchen_only_has_kds_and_orders(self):
        assert DEFAULT_ROLE_MODULES[Role.KITCHEN] == frozenset({
            Module.KDS, Module.ORDERS,
        })

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
