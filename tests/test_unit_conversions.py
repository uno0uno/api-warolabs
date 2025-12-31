"""
Tests for unit conversion logic in purchase orders.

These tests verify the conversion between purchase units (Cajas, Paquetes, Docenas)
and base units (unidades, kg, g, etc.) when receiving purchases.

Key scenarios:
- Caja de 24 unidades → recibir 2 cajas = 48 unidades
- Paquete de 12 unidades → recibir 3 paquetes = 36 unidades
- Sin conversión (unidades directas)
"""
import pytest


class TestConversionFactorCalculation:
    """Test the conversion factor calculation logic"""

    def test_conversion_caja_24_unidades(self):
        """Test: 1 Caja = 24 unidades, recibir 2 cajas"""
        # Datos de compra original
        original_quantity = 48.0  # Total en unidades base (2 cajas * 24)
        original_purchase_quantity = 2.0  # Cantidad en unidades de compra (2 cajas)
        quantity_received = 2.0  # Recibido en unidades de compra (2 cajas)

        # Cálculo del factor de conversión
        conversion_factor = original_quantity / original_purchase_quantity
        assert conversion_factor == 24.0  # 1 caja = 24 unidades

        # Conversión de cantidad recibida a unidades base
        quantity_received_base = quantity_received * conversion_factor
        assert quantity_received_base == 48.0  # 2 cajas * 24 = 48 unidades

    def test_conversion_paquete_12_unidades(self):
        """Test: 1 Paquete = 12 unidades, recibir 3 paquetes"""
        original_quantity = 36.0  # 3 paquetes * 12 unidades
        original_purchase_quantity = 3.0  # 3 paquetes
        quantity_received = 3.0

        conversion_factor = original_quantity / original_purchase_quantity
        assert conversion_factor == 12.0

        quantity_received_base = quantity_received * conversion_factor
        assert quantity_received_base == 36.0

    def test_conversion_docena(self):
        """Test: 1 Docena = 12 unidades"""
        original_quantity = 24.0  # 2 docenas
        original_purchase_quantity = 2.0
        quantity_received = 1.0  # Recibir solo 1 docena

        conversion_factor = original_quantity / original_purchase_quantity
        assert conversion_factor == 12.0

        quantity_received_base = quantity_received * conversion_factor
        assert quantity_received_base == 12.0

    def test_conversion_bulto_50_kg(self):
        """Test: 1 Bulto = 50 kg"""
        original_quantity = 100.0  # 2 bultos = 100 kg
        original_purchase_quantity = 2.0
        quantity_received = 2.0

        conversion_factor = original_quantity / original_purchase_quantity
        assert conversion_factor == 50.0

        quantity_received_base = quantity_received * conversion_factor
        assert quantity_received_base == 100.0

    def test_no_conversion_needed_same_unit(self):
        """Test: Sin conversión cuando ya está en unidades base"""
        original_quantity = 100.0  # 100 unidades
        original_purchase_quantity = 0.0  # No hay purchase_quantity (compra directa en unidades)
        quantity_received = 100.0

        # Lógica: si purchase_quantity es 0, no hay conversión
        if original_purchase_quantity > 0 and original_quantity > 0:
            conversion_factor = original_quantity / original_purchase_quantity
            quantity_received_base = quantity_received * conversion_factor
        else:
            quantity_received_base = quantity_received

        assert quantity_received_base == 100.0

    def test_partial_reception_conversion(self):
        """Test: Recepción parcial con conversión"""
        # Pedido: 4 cajas de 24 unidades = 96 unidades
        original_quantity = 96.0
        original_purchase_quantity = 4.0
        quantity_received = 2.0  # Solo recibir 2 cajas

        conversion_factor = original_quantity / original_purchase_quantity
        assert conversion_factor == 24.0

        quantity_received_base = quantity_received * conversion_factor
        assert quantity_received_base == 48.0  # 2 cajas * 24 = 48 unidades

    def test_fractional_reception(self):
        """Test: Recepción de cantidad fraccionada"""
        # Pedido: 10 kg en 2 bolsas de 5 kg
        original_quantity = 10.0  # 10 kg
        original_purchase_quantity = 2.0  # 2 bolsas
        quantity_received = 1.5  # Recibir 1.5 bolsas (una completa, una mitad)

        conversion_factor = original_quantity / original_purchase_quantity
        assert conversion_factor == 5.0  # 1 bolsa = 5 kg

        quantity_received_base = quantity_received * conversion_factor
        assert quantity_received_base == 7.5  # 1.5 * 5 = 7.5 kg


class TestStateTransitionValidation:
    """Test purchase order state transition rules"""

    # Define the state transition rules (from the service)
    STATE_TRANSITIONS = {
        'quotation': ['pending', 'cancelled'],
        'pending': ['confirmed', 'cancelled'],
        'confirmed': ['preparing', 'paid', 'invoiced', 'cancelled'],
        'preparing': ['paid', 'invoiced', 'cancelled'],
        'paid': ['invoiced'],
        'invoiced': ['shipped'],
        'shipped': ['received', 'partially_received', 'overdue'],
        'partially_received': ['received', 'overdue'],
        'received': ['paid'],
        'cancelled': [],
        'overdue': ['shipped', 'received', 'cancelled']
    }

    def validate_state_transition(self, from_status: str, to_status: str) -> bool:
        """Validate if a state transition is allowed"""
        allowed_transitions = self.STATE_TRANSITIONS.get(from_status, [])
        return to_status in allowed_transitions

    # Valid transitions
    def test_quotation_to_pending(self):
        """Test: quotation → pending (valid)"""
        assert self.validate_state_transition('quotation', 'pending') is True

    def test_pending_to_confirmed(self):
        """Test: pending → confirmed (valid)"""
        assert self.validate_state_transition('pending', 'confirmed') is True

    def test_confirmed_to_invoiced(self):
        """Test: confirmed → invoiced (valid for contado)"""
        assert self.validate_state_transition('confirmed', 'invoiced') is True

    def test_invoiced_to_shipped(self):
        """Test: invoiced → shipped (valid)"""
        assert self.validate_state_transition('invoiced', 'shipped') is True

    def test_shipped_to_received(self):
        """Test: shipped → received (valid)"""
        assert self.validate_state_transition('shipped', 'received') is True

    def test_shipped_to_partially_received(self):
        """Test: shipped → partially_received (valid)"""
        assert self.validate_state_transition('shipped', 'partially_received') is True

    def test_received_to_paid(self):
        """Test: received → paid (valid for credito flow)"""
        assert self.validate_state_transition('received', 'paid') is True

    # Invalid transitions
    def test_pending_to_shipped_invalid(self):
        """Test: pending → shipped (invalid - must be invoiced first)"""
        assert self.validate_state_transition('pending', 'shipped') is False

    def test_shipped_to_paid_invalid(self):
        """Test: shipped → paid (invalid - must be received first)"""
        assert self.validate_state_transition('shipped', 'paid') is False

    def test_cancelled_to_any_invalid(self):
        """Test: cancelled → any (invalid - final state)"""
        assert self.validate_state_transition('cancelled', 'pending') is False
        assert self.validate_state_transition('cancelled', 'confirmed') is False

    def test_paid_to_cancelled_invalid(self):
        """Test: paid → cancelled (invalid - already paid)"""
        assert self.validate_state_transition('paid', 'cancelled') is False

    # Cancellation tests
    def test_quotation_can_be_cancelled(self):
        """Test: quotation can be cancelled"""
        assert self.validate_state_transition('quotation', 'cancelled') is True

    def test_pending_can_be_cancelled(self):
        """Test: pending can be cancelled"""
        assert self.validate_state_transition('pending', 'cancelled') is True

    def test_confirmed_can_be_cancelled(self):
        """Test: confirmed can be cancelled"""
        assert self.validate_state_transition('confirmed', 'cancelled') is True

    def test_shipped_cannot_be_cancelled(self):
        """Test: shipped cannot be cancelled (in transit)"""
        assert self.validate_state_transition('shipped', 'cancelled') is False

    def test_received_cannot_be_cancelled(self):
        """Test: received cannot be cancelled (inventory updated)"""
        assert self.validate_state_transition('received', 'cancelled') is False


class TestInventoryUpdateScenarios:
    """Test different inventory update scenarios"""

    def calculate_new_stock(
        self,
        previous_stock: float,
        quantity_received: float,
        conversion_factor: float = 1.0
    ) -> float:
        """Calculate new stock after receiving items"""
        quantity_in_base_units = quantity_received * conversion_factor
        return previous_stock + quantity_in_base_units

    def test_add_to_empty_inventory(self):
        """Test: Adding items to empty inventory"""
        previous_stock = 0.0
        quantity_received = 2.0  # 2 cajas
        conversion_factor = 24.0  # 1 caja = 24 unidades

        new_stock = self.calculate_new_stock(previous_stock, quantity_received, conversion_factor)
        assert new_stock == 48.0

    def test_add_to_existing_inventory(self):
        """Test: Adding items to existing inventory"""
        previous_stock = 50.0  # Ya hay 50 unidades
        quantity_received = 1.0  # 1 caja
        conversion_factor = 24.0

        new_stock = self.calculate_new_stock(previous_stock, quantity_received, conversion_factor)
        assert new_stock == 74.0  # 50 + 24

    def test_partial_reception_inventory(self):
        """Test: Partial reception updates inventory correctly"""
        previous_stock = 100.0
        quantity_received = 0.5  # Media caja
        conversion_factor = 24.0

        new_stock = self.calculate_new_stock(previous_stock, quantity_received, conversion_factor)
        assert new_stock == 112.0  # 100 + 12

    def test_multiple_items_different_conversions(self):
        """Test: Multiple items with different conversion factors"""
        inventory = {}

        # Item 1: Huevos (1 caja = 30 unidades)
        inventory['huevos'] = {
            'previous': 60.0,
            'received': 2.0,
            'factor': 30.0
        }

        # Item 2: Harina (1 bulto = 50 kg)
        inventory['harina'] = {
            'previous': 25.0,
            'received': 1.0,
            'factor': 50.0
        }

        # Calculate new stocks
        for key, item in inventory.items():
            item['new_stock'] = self.calculate_new_stock(
                item['previous'],
                item['received'],
                item['factor']
            )

        assert inventory['huevos']['new_stock'] == 120.0  # 60 + (2 * 30)
        assert inventory['harina']['new_stock'] == 75.0  # 25 + (1 * 50)


class TestReceptionScenarios:
    """Test different purchase reception scenarios"""

    def test_full_reception_scenario(self):
        """Test: Full reception of all items"""
        # Pedido: 3 cajas de 24 unidades = 72 unidades
        order = {
            'quantity': 72.0,
            'purchase_quantity': 3.0,
            'purchase_unit': 'Caja'
        }

        # Recepción completa
        reception = {
            'quantity_received': 3.0,  # 3 cajas
            'partial': False
        }

        conversion_factor = order['quantity'] / order['purchase_quantity']
        quantity_base = reception['quantity_received'] * conversion_factor

        assert quantity_base == 72.0
        assert reception['partial'] is False

    def test_partial_reception_scenario(self):
        """Test: Partial reception (not all items received)"""
        # Pedido: 4 cajas de 24 unidades = 96 unidades
        order = {
            'quantity': 96.0,
            'purchase_quantity': 4.0,
            'purchase_unit': 'Caja'
        }

        # Recepción parcial: solo 2 cajas
        reception = {
            'quantity_received': 2.0,
            'partial': True
        }

        conversion_factor = order['quantity'] / order['purchase_quantity']
        quantity_base = reception['quantity_received'] * conversion_factor
        remaining = order['quantity'] - quantity_base

        assert quantity_base == 48.0
        assert remaining == 48.0  # 2 cajas pendientes
        assert reception['partial'] is True

    def test_over_reception_scenario(self):
        """Test: Over reception (received more than ordered - bonus)"""
        # Pedido: 2 cajas de 24 unidades = 48 unidades
        order = {
            'quantity': 48.0,
            'purchase_quantity': 2.0
        }

        # Recepción: proveedor envió 3 cajas
        reception = {
            'quantity_received': 3.0
        }

        conversion_factor = order['quantity'] / order['purchase_quantity']
        quantity_base = reception['quantity_received'] * conversion_factor
        extra = quantity_base - order['quantity']

        assert quantity_base == 72.0
        assert extra == 24.0  # 1 caja extra

    def test_damaged_items_scenario(self):
        """Test: Reception with some damaged items"""
        # Pedido: 5 cajas de 24 = 120 unidades
        order = {
            'quantity': 120.0,
            'purchase_quantity': 5.0
        }

        # Recepción: 5 cajas pero 1 caja dañada
        reception = {
            'quantity_received': 4.0,  # Solo 4 cajas en buen estado
            'damaged': 1.0,
            'quality_status': 'partial_rejected'
        }

        conversion_factor = order['quantity'] / order['purchase_quantity']
        quantity_good = reception['quantity_received'] * conversion_factor
        quantity_damaged = reception['damaged'] * conversion_factor

        assert quantity_good == 96.0  # 4 cajas buenas
        assert quantity_damaged == 24.0  # 1 caja dañada


class TestPaymentTypeFlows:
    """Test payment type flows: contado vs crédito"""

    # Document types
    DOCUMENT_TYPES = {
        'remision': {'requires_payment': False, 'description': 'Remisión (sin factura)'},
        'factura_contado': {'requires_payment': True, 'description': 'Factura de contado'},
        'factura_credito': {'requires_payment': False, 'description': 'Factura a crédito'}
    }

    def get_payment_flow(self, document_type: str) -> dict:
        """Determine payment flow based on document type"""
        doc_config = self.DOCUMENT_TYPES.get(document_type, {})

        if document_type == 'factura_contado':
            return {
                'flow': 'contado',
                'requires_immediate_payment': True,
                'next_states': ['paid', 'invoiced'],
                'payment_due_date': None  # Pago inmediato
            }
        elif document_type == 'factura_credito':
            return {
                'flow': 'credito',
                'requires_immediate_payment': False,
                'next_states': ['invoiced', 'shipped'],
                'payment_due_date': 'calculated_from_credit_days'
            }
        else:  # remision
            return {
                'flow': 'remision',
                'requires_immediate_payment': False,
                'next_states': ['shipped'],
                'payment_due_date': None
            }

    def test_factura_contado_flow(self):
        """Test: Factura contado requires immediate payment"""
        flow = self.get_payment_flow('factura_contado')

        assert flow['flow'] == 'contado'
        assert flow['requires_immediate_payment'] is True
        assert 'paid' in flow['next_states']

    def test_factura_credito_flow(self):
        """Test: Factura crédito allows deferred payment"""
        flow = self.get_payment_flow('factura_credito')

        assert flow['flow'] == 'credito'
        assert flow['requires_immediate_payment'] is False
        assert flow['payment_due_date'] == 'calculated_from_credit_days'

    def test_remision_flow(self):
        """Test: Remisión has no payment requirement"""
        flow = self.get_payment_flow('remision')

        assert flow['flow'] == 'remision'
        assert flow['requires_immediate_payment'] is False
        assert 'shipped' in flow['next_states']

    def test_state_after_contado_invoice(self):
        """Test: After contado invoice, can go to paid or invoiced"""
        flow = self.get_payment_flow('factura_contado')

        # Contado: confirmed → paid → invoiced OR confirmed → invoiced (if already paid)
        assert 'paid' in flow['next_states']
        assert 'invoiced' in flow['next_states']

    def test_state_after_credito_invoice(self):
        """Test: After crédito invoice, goes to shipped then received then paid"""
        flow = self.get_payment_flow('factura_credito')

        # Crédito: confirmed → invoiced → shipped → received → paid
        assert 'invoiced' in flow['next_states']
        assert 'shipped' in flow['next_states']


class TestPaymentDueDateCalculation:
    """Test payment due date calculation based on credit days"""

    def calculate_payment_due_date(
        self,
        invoice_date: str,
        credit_days: int
    ) -> str:
        """
        Calculate payment due date from invoice date + credit days.
        Returns date in YYYY-MM-DD format.
        """
        from datetime import datetime, timedelta

        invoice_dt = datetime.strptime(invoice_date, '%Y-%m-%d')
        due_dt = invoice_dt + timedelta(days=credit_days)
        return due_dt.strftime('%Y-%m-%d')

    def test_credit_15_days(self):
        """Test: 15 days credit"""
        due_date = self.calculate_payment_due_date('2024-01-01', 15)
        assert due_date == '2024-01-16'

    def test_credit_30_days(self):
        """Test: 30 days credit (common)"""
        due_date = self.calculate_payment_due_date('2024-01-15', 30)
        assert due_date == '2024-02-14'

    def test_credit_45_days(self):
        """Test: 45 days credit"""
        due_date = self.calculate_payment_due_date('2024-01-01', 45)
        assert due_date == '2024-02-15'

    def test_credit_60_days(self):
        """Test: 60 days credit"""
        due_date = self.calculate_payment_due_date('2024-01-01', 60)
        assert due_date == '2024-03-01'

    def test_credit_90_days(self):
        """Test: 90 days credit"""
        due_date = self.calculate_payment_due_date('2024-01-01', 90)
        assert due_date == '2024-03-31'

    def test_credit_crosses_month(self):
        """Test: Credit period crosses month boundary"""
        due_date = self.calculate_payment_due_date('2024-01-20', 15)
        assert due_date == '2024-02-04'

    def test_credit_crosses_year(self):
        """Test: Credit period crosses year boundary"""
        due_date = self.calculate_payment_due_date('2024-12-15', 30)
        assert due_date == '2025-01-14'

    def test_zero_credit_days(self):
        """Test: Zero credit days (contado)"""
        due_date = self.calculate_payment_due_date('2024-01-15', 0)
        assert due_date == '2024-01-15'  # Same day


class TestPaymentAgreementTypes:
    """Test supplier payment agreement types"""

    # Agreement types from the service
    AGREEMENT_TYPES = {
        'net_days': 'Payment due X days after invoice',
        'specific_day': 'Payment on specific day of month',
        'end_of_month': 'Payment at end of invoice month',
        'next_month_specific': 'Payment on specific day of next month'
    }

    def calculate_due_date_from_agreement(
        self,
        invoice_date: str,
        agreement_type: str,
        days_offset: int = 0,
        specific_day: int = None
    ) -> str:
        """
        Calculate payment due date based on agreement type.
        """
        from datetime import datetime, timedelta
        from calendar import monthrange

        invoice_dt = datetime.strptime(invoice_date, '%Y-%m-%d')

        if agreement_type == 'net_days':
            # Simple: invoice date + days_offset
            due_dt = invoice_dt + timedelta(days=days_offset)

        elif agreement_type == 'specific_day':
            # Payment on specific day of current month
            year = invoice_dt.year
            month = invoice_dt.month
            max_day = monthrange(year, month)[1]
            day = min(specific_day, max_day)
            due_dt = datetime(year, month, day)

            # If specific day already passed, use next month
            if due_dt <= invoice_dt:
                if month == 12:
                    year += 1
                    month = 1
                else:
                    month += 1
                max_day = monthrange(year, month)[1]
                day = min(specific_day, max_day)
                due_dt = datetime(year, month, day)

        elif agreement_type == 'end_of_month':
            # Last day of invoice month
            year = invoice_dt.year
            month = invoice_dt.month
            last_day = monthrange(year, month)[1]
            due_dt = datetime(year, month, last_day)

        elif agreement_type == 'next_month_specific':
            # Specific day of next month
            year = invoice_dt.year
            month = invoice_dt.month + 1
            if month > 12:
                year += 1
                month = 1
            max_day = monthrange(year, month)[1]
            day = min(specific_day, max_day)
            due_dt = datetime(year, month, day)

        else:
            due_dt = invoice_dt

        return due_dt.strftime('%Y-%m-%d')

    def test_net_days_30(self):
        """Test: Net 30 days agreement"""
        due_date = self.calculate_due_date_from_agreement(
            invoice_date='2024-01-15',
            agreement_type='net_days',
            days_offset=30
        )
        assert due_date == '2024-02-14'

    def test_net_days_60(self):
        """Test: Net 60 days agreement"""
        due_date = self.calculate_due_date_from_agreement(
            invoice_date='2024-01-01',
            agreement_type='net_days',
            days_offset=60
        )
        assert due_date == '2024-03-01'

    def test_specific_day_15(self):
        """Test: Payment on day 15 of month"""
        due_date = self.calculate_due_date_from_agreement(
            invoice_date='2024-01-05',
            agreement_type='specific_day',
            specific_day=15
        )
        assert due_date == '2024-01-15'

    def test_specific_day_already_passed(self):
        """Test: Specific day already passed, use next month"""
        due_date = self.calculate_due_date_from_agreement(
            invoice_date='2024-01-20',
            agreement_type='specific_day',
            specific_day=15
        )
        assert due_date == '2024-02-15'

    def test_end_of_month_january(self):
        """Test: End of month (January = 31)"""
        due_date = self.calculate_due_date_from_agreement(
            invoice_date='2024-01-15',
            agreement_type='end_of_month'
        )
        assert due_date == '2024-01-31'

    def test_end_of_month_february_leap(self):
        """Test: End of month (February leap year = 29)"""
        due_date = self.calculate_due_date_from_agreement(
            invoice_date='2024-02-15',
            agreement_type='end_of_month'
        )
        assert due_date == '2024-02-29'

    def test_end_of_month_february_non_leap(self):
        """Test: End of month (February non-leap = 28)"""
        due_date = self.calculate_due_date_from_agreement(
            invoice_date='2023-02-15',
            agreement_type='end_of_month'
        )
        assert due_date == '2023-02-28'

    def test_next_month_specific_day_5(self):
        """Test: Payment on day 5 of next month"""
        due_date = self.calculate_due_date_from_agreement(
            invoice_date='2024-01-20',
            agreement_type='next_month_specific',
            specific_day=5
        )
        assert due_date == '2024-02-05'

    def test_next_month_december_to_january(self):
        """Test: Next month from December to January"""
        due_date = self.calculate_due_date_from_agreement(
            invoice_date='2024-12-15',
            agreement_type='next_month_specific',
            specific_day=10
        )
        assert due_date == '2025-01-10'

    def test_specific_day_31_february(self):
        """Test: Day 31 in February adjusts to max day"""
        due_date = self.calculate_due_date_from_agreement(
            invoice_date='2024-01-31',
            agreement_type='next_month_specific',
            specific_day=31
        )
        assert due_date == '2024-02-29'  # Feb 2024 has 29 days


class TestPaymentStatusTracking:
    """Test payment status tracking for purchase orders"""

    PAYMENT_STATUSES = ['pending', 'partial', 'paid', 'overdue']

    def calculate_payment_status(
        self,
        total_amount: float,
        amount_paid: float,
        payment_due_date: str,
        current_date: str
    ) -> str:
        """
        Determine payment status based on amounts and dates.
        """
        from datetime import datetime

        if amount_paid >= total_amount:
            return 'paid'

        due_dt = datetime.strptime(payment_due_date, '%Y-%m-%d')
        current_dt = datetime.strptime(current_date, '%Y-%m-%d')

        if current_dt > due_dt:
            return 'overdue'

        if amount_paid > 0:
            return 'partial'

        return 'pending'

    def test_status_paid_full(self):
        """Test: Fully paid"""
        status = self.calculate_payment_status(
            total_amount=100000.0,
            amount_paid=100000.0,
            payment_due_date='2024-02-15',
            current_date='2024-01-20'
        )
        assert status == 'paid'

    def test_status_paid_overpaid(self):
        """Test: Overpaid (still considered paid)"""
        status = self.calculate_payment_status(
            total_amount=100000.0,
            amount_paid=105000.0,
            payment_due_date='2024-02-15',
            current_date='2024-01-20'
        )
        assert status == 'paid'

    def test_status_pending(self):
        """Test: Pending (not paid, not due yet)"""
        status = self.calculate_payment_status(
            total_amount=100000.0,
            amount_paid=0.0,
            payment_due_date='2024-02-15',
            current_date='2024-01-20'
        )
        assert status == 'pending'

    def test_status_partial(self):
        """Test: Partial payment"""
        status = self.calculate_payment_status(
            total_amount=100000.0,
            amount_paid=50000.0,
            payment_due_date='2024-02-15',
            current_date='2024-01-20'
        )
        assert status == 'partial'

    def test_status_overdue(self):
        """Test: Overdue (not paid, past due date)"""
        status = self.calculate_payment_status(
            total_amount=100000.0,
            amount_paid=0.0,
            payment_due_date='2024-01-15',
            current_date='2024-01-20'
        )
        assert status == 'overdue'

    def test_status_overdue_with_partial(self):
        """Test: Overdue even with partial payment"""
        status = self.calculate_payment_status(
            total_amount=100000.0,
            amount_paid=50000.0,
            payment_due_date='2024-01-15',
            current_date='2024-01-20'
        )
        # Partial payment past due is still overdue
        assert status == 'overdue'

    def test_status_due_today(self):
        """Test: Due today, not yet overdue"""
        status = self.calculate_payment_status(
            total_amount=100000.0,
            amount_paid=0.0,
            payment_due_date='2024-01-20',
            current_date='2024-01-20'
        )
        assert status == 'pending'  # Same day is not overdue


class TestCreditoVsContadoStateTransitions:
    """Test different state transitions for crédito vs contado purchases"""

    # Crédito flow: confirmed → invoiced → shipped → received → paid
    # Contado flow: confirmed → paid → invoiced → shipped → received

    CREDITO_TRANSITIONS = {
        'confirmed': ['invoiced', 'cancelled'],
        'invoiced': ['shipped'],
        'shipped': ['received', 'partially_received'],
        'received': ['paid'],
        'paid': []  # Final state
    }

    CONTADO_TRANSITIONS = {
        'confirmed': ['paid', 'cancelled'],
        'paid': ['invoiced'],
        'invoiced': ['shipped'],
        'shipped': ['received', 'partially_received'],
        'received': []  # Final state (already paid)
    }

    def validate_transition(
        self,
        payment_type: str,
        from_status: str,
        to_status: str
    ) -> bool:
        """Validate state transition based on payment type"""
        if payment_type == 'credito':
            allowed = self.CREDITO_TRANSITIONS.get(from_status, [])
        else:  # contado
            allowed = self.CONTADO_TRANSITIONS.get(from_status, [])

        return to_status in allowed

    # Crédito tests
    def test_credito_confirmed_to_invoiced(self):
        """Test: Crédito - confirmed → invoiced (valid)"""
        assert self.validate_transition('credito', 'confirmed', 'invoiced') is True

    def test_credito_invoiced_to_shipped(self):
        """Test: Crédito - invoiced → shipped (valid)"""
        assert self.validate_transition('credito', 'invoiced', 'shipped') is True

    def test_credito_received_to_paid(self):
        """Test: Crédito - received → paid (valid - pay after receiving)"""
        assert self.validate_transition('credito', 'received', 'paid') is True

    def test_credito_confirmed_to_paid_invalid(self):
        """Test: Crédito - confirmed → paid (invalid - must invoice first)"""
        assert self.validate_transition('credito', 'confirmed', 'paid') is False

    # Contado tests
    def test_contado_confirmed_to_paid(self):
        """Test: Contado - confirmed → paid (valid - pay first)"""
        assert self.validate_transition('contado', 'confirmed', 'paid') is True

    def test_contado_paid_to_invoiced(self):
        """Test: Contado - paid → invoiced (valid)"""
        assert self.validate_transition('contado', 'paid', 'invoiced') is True

    def test_contado_confirmed_to_invoiced_invalid(self):
        """Test: Contado - confirmed → invoiced (invalid - must pay first)"""
        assert self.validate_transition('contado', 'confirmed', 'invoiced') is False

    def test_contado_received_is_final(self):
        """Test: Contado - received is final (already paid)"""
        assert self.validate_transition('contado', 'received', 'paid') is False
