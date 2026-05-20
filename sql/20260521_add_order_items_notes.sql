-- POS special notes on tab/order lines → kitchen comanda print (#757)
ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS notes TEXT NULL;

COMMENT ON COLUMN order_items.notes IS 'Special preparation notes from POS (Notas Especiales); copied to comanda_items on fire.';
