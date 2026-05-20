-- #745: operational (perceived) cost — tenant-set, independent of costo_calculado
ALTER TABLE product
    ADD COLUMN IF NOT EXISTS costo_percibido NUMERIC(10, 2) NULL;

COMMENT ON COLUMN product.costo_percibido IS
    'Costo operativo/percibido definido por el tenant; no se recalcula con compras ni recetas.';
