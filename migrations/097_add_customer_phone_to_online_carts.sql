-- Store the phone number captured during public checkout for this specific order.
-- Phone numbers are contact data, not unique customer identity.
ALTER TABLE online_carts
ADD COLUMN IF NOT EXISTS customer_phone varchar;
