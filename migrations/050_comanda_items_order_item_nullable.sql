-- Migration 050: make comanda_items.order_item_id nullable
-- Allows cancelled items to persist after the order_item row is deleted.
-- The FK is kept so live items still reference a valid order_item.

ALTER TABLE comanda_items ALTER COLUMN order_item_id DROP NOT NULL;
