-- warocol.com#911 — persist captured tips on cash-close summaries
ALTER TABLE closing_summary
    ADD COLUMN IF NOT EXISTS total_tips numeric(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_tip_tax numeric(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cash_tips numeric(14,2) NOT NULL DEFAULT 0;

COMMENT ON COLUMN closing_summary.total_tips IS
    'Sum of orders.tip_amount captured in the closed period (#911).';
COMMENT ON COLUMN closing_summary.total_tip_tax IS
    'Sum of orders.tip_tax_amount captured in the closed period (#911).';
COMMENT ON COLUMN closing_summary.cash_tips IS
    'Tip settlement collected via cash payment method during the period (#911).';
