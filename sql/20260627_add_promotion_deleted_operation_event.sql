-- Allow promotion deletes to appear in Bitácora de operaciones.
ALTER TABLE tenant_operation_events
    DROP CONSTRAINT IF EXISTS tenant_operation_events_action_check;

ALTER TABLE tenant_operation_events
    ADD CONSTRAINT tenant_operation_events_action_check
    CHECK (action = ANY (ARRAY[
        'tab_item_added'::text,
        'tab_item_removed'::text,
        'tab_item_qty_changed'::text,
        'tab_item_edited'::text,
        'tab_item_edit_blocked'::text,
        'tab_cleared'::text,
        'cart_line_removed'::text,
        'cart_cleared'::text,
        'payment_voided'::text,
        'comanda_line_cancelled'::text,
        'promotion_deleted'::text
    ]));
