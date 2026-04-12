-- Migration 016: add payment_method_id to tenant_purchases (issue #343)
-- Stores the specific sub-method UUID (FK to payment_methods) when the user
-- selects a sub-method like Nequi or Daviplata inside a payment group.
-- payment_method keeps the parent group slug for backward compatibility.

ALTER TABLE tenant_purchases
  ADD COLUMN IF NOT EXISTS payment_method_id uuid REFERENCES payment_methods(id) ON DELETE SET NULL;
