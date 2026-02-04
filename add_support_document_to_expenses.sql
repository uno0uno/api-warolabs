-- Add expense_id column to purchase_attachments table to support expense documents
-- This allows reusing the same attachment infrastructure for both purchases and expenses
ALTER TABLE purchase_attachments 
ADD COLUMN IF NOT EXISTS expense_id UUID REFERENCES tenant_expenses(id) ON DELETE CASCADE;

-- Make purchase_id nullable since now we can have either purchase_id OR expense_id
ALTER TABLE purchase_attachments 
ALTER COLUMN purchase_id DROP NOT NULL;

-- Add comment
COMMENT ON COLUMN purchase_attachments.expense_id IS 'Reference to expense if this attachment belongs to an expense (mutually exclusive with purchase_id)';

-- Add index for performance
CREATE INDEX IF NOT EXISTS idx_purchase_attachments_expense_id ON purchase_attachments(expense_id) WHERE expense_id IS NOT NULL;
