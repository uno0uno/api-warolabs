-- Remove electronic invoicing test data for Waro Colombia (warocolombia).
-- Does NOT delete POS orders — only DIAN audit rows.
--
-- Tenant UUID: 93b3e582-34fa-44a6-8d0f-bf82a3608727

BEGIN;

DELETE FROM dian_sequence_gaps
WHERE resolution_id IN (
  SELECT id FROM dian_resolutions
  WHERE tenant_id = '93b3e582-34fa-44a6-8d0f-bf82a3608727'
);

DELETE FROM electronic_invoices
WHERE tenant_id = '93b3e582-34fa-44a6-8d0f-bf82a3608727';

DELETE FROM dian_resolutions
WHERE tenant_id = '93b3e582-34fa-44a6-8d0f-bf82a3608727';

COMMIT;
