-- Migration: Add purchase attachments table
-- Description: Allows uploading supporting documents (invoices, receipts, contracts, etc.) for purchases
-- Date: 2025-11-16

-- Create purchase_attachments table
CREATE TABLE IF NOT EXISTS purchase_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    purchase_id UUID NOT NULL REFERENCES tenant_purchases(id) ON DELETE CASCADE,

    -- File information
    file_name VARCHAR(255) NOT NULL,
    file_type VARCHAR(100) NOT NULL, -- MIME type (e.g., application/pdf, image/jpeg)
    file_size BIGINT NOT NULL, -- Size in bytes
    s3_key TEXT NOT NULL, -- S3 path/key to the file
    s3_url TEXT, -- Optional: full S3 URL if needed

    -- Attachment metadata
    attachment_type VARCHAR(50) NOT NULL, -- e.g., 'invoice', 'receipt', 'contract', 'delivery_note', 'other'
    description TEXT, -- Optional description of the attachment

    -- Timestamps and audit
    uploaded_at TIMESTAMP NOT NULL DEFAULT NOW(),
    uploaded_by UUID, -- User who uploaded the file
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT positive_file_size CHECK (file_size > 0),
    CONSTRAINT valid_attachment_type CHECK (attachment_type IN ('invoice', 'receipt', 'contract', 'delivery_note', 'quotation', 'other'))
);

-- Create indexes for better query performance
CREATE INDEX IF NOT EXISTS idx_purchase_attachments_purchase_id ON purchase_attachments(purchase_id);
CREATE INDEX IF NOT EXISTS idx_purchase_attachments_tenant_id ON purchase_attachments(tenant_id);
CREATE INDEX IF NOT EXISTS idx_purchase_attachments_type ON purchase_attachments(attachment_type);
CREATE INDEX IF NOT EXISTS idx_purchase_attachments_uploaded_at ON purchase_attachments(uploaded_at DESC);

-- Add comments for documentation
COMMENT ON TABLE purchase_attachments IS 'Stores file attachments (invoices, receipts, contracts, etc.) for purchase orders';
COMMENT ON COLUMN purchase_attachments.attachment_type IS 'Type of attachment: invoice, receipt, contract, delivery_note, quotation, or other';
COMMENT ON COLUMN purchase_attachments.s3_key IS 'S3 key (path) to the file in the bucket';
COMMENT ON COLUMN purchase_attachments.file_size IS 'File size in bytes';
