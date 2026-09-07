"""Sanitize Matias infrastructure dumps before they reach cashiers."""
from app.services.invoicing_presentation.public_error import (
    PUBLIC_FACTURADOR_RETRY,
    public_invoice_error_message,
)


def test_sql_dump_is_replaced():
    raw = (
        "Matias API 500: Error interno del servidor. SQLSTATE[42S22]: "
        "Column not found: 1054 Unknown column 'company_id' in 'WHERE' "
        "(Connection: mysql, SQL: select * from `file_managers` where "
        "(`company_id` = 821) limit 1)"
    )
    assert public_invoice_error_message(raw) == PUBLIC_FACTURADOR_RETRY


def test_generic_matias_500_is_replaced():
    assert (
        public_invoice_error_message("Matias API 500: Error interno del servidor")
        == PUBLIC_FACTURADOR_RETRY
    )


def test_dian_business_errors_pass_through():
    nit = "Matias API 400: Falta NIT del cliente"
    assert public_invoice_error_message(nit) == nit
    ya = "Matias API 400: ya se encuentra validado."
    assert public_invoice_error_message(ya) == ya


def test_none_and_empty():
    assert public_invoice_error_message(None) is None
    assert public_invoice_error_message("") == ""
    assert public_invoice_error_message("   ") == ""
