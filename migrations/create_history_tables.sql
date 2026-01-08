-- =====================================================
-- TABLAS DE HISTORIAL PARA TRAZABILIDAD DE CAMBIOS
-- Permite analizar impacto de cambios en ventas
-- =====================================================

-- 1. HISTORIAL DE PRODUCTOS
CREATE TABLE IF NOT EXISTS product_change_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    product_id UUID NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    product_name VARCHAR(255) NOT NULL,
    change_type VARCHAR(20) NOT NULL CHECK (change_type IN ('create', 'update', 'delete')),
    field_changed VARCHAR(100),
    old_value JSONB,
    new_value JSONB,
    product_snapshot JSONB,
    changed_by UUID REFERENCES profile(id),
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_product_history_tenant ON product_change_history(tenant_id);
CREATE INDEX IF NOT EXISTS idx_product_history_product ON product_change_history(product_id);
CREATE INDEX IF NOT EXISTS idx_product_history_date ON product_change_history(created_at);
CREATE INDEX IF NOT EXISTS idx_product_history_field ON product_change_history(field_changed) WHERE field_changed IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_product_history_type ON product_change_history(change_type);

COMMENT ON TABLE product_change_history IS 'Historial de cambios en productos para trazabilidad y análisis de impacto';
COMMENT ON COLUMN product_change_history.product_snapshot IS 'Snapshot completo del producto al momento del cambio';
COMMENT ON COLUMN product_change_history.field_changed IS 'Campo que cambió: price, name, description, ingredients, recipe_bases, is_available, category_id';


-- 2. HISTORIAL DE RECETAS BASE
CREATE TABLE IF NOT EXISTS recipe_base_change_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    recipe_base_id UUID NOT NULL REFERENCES product_base_types(id) ON DELETE CASCADE,
    recipe_base_name VARCHAR(255) NOT NULL,
    change_type VARCHAR(20) NOT NULL CHECK (change_type IN ('create', 'update', 'delete')),
    field_changed VARCHAR(100),
    old_value JSONB,
    new_value JSONB,
    recipe_snapshot JSONB,
    changed_by UUID REFERENCES profile(id),
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recipe_history_tenant ON recipe_base_change_history(tenant_id);
CREATE INDEX IF NOT EXISTS idx_recipe_history_recipe ON recipe_base_change_history(recipe_base_id);
CREATE INDEX IF NOT EXISTS idx_recipe_history_date ON recipe_base_change_history(created_at);
CREATE INDEX IF NOT EXISTS idx_recipe_history_field ON recipe_base_change_history(field_changed) WHERE field_changed IS NOT NULL;

COMMENT ON TABLE recipe_base_change_history IS 'Historial de cambios en recetas base para trazabilidad';
COMMENT ON COLUMN recipe_base_change_history.field_changed IS 'Campo que cambió: name, description, ingredients, is_active';


-- 3. HISTORIAL DE MODIFICADORES
CREATE TABLE IF NOT EXISTS modifier_change_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    entity_type VARCHAR(20) NOT NULL CHECK (entity_type IN ('modifier_group', 'modifier')),
    modifier_group_id UUID REFERENCES modifier_groups(id) ON DELETE CASCADE,
    modifier_id UUID REFERENCES modifiers(id) ON DELETE CASCADE,
    entity_name VARCHAR(255) NOT NULL,
    change_type VARCHAR(20) NOT NULL CHECK (change_type IN ('create', 'update', 'delete')),
    field_changed VARCHAR(100),
    old_value JSONB,
    new_value JSONB,
    modifier_snapshot JSONB,
    changed_by UUID REFERENCES profile(id),
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT chk_modifier_entity CHECK (
        (entity_type = 'modifier_group' AND modifier_group_id IS NOT NULL) OR
        (entity_type = 'modifier' AND modifier_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_modifier_history_tenant ON modifier_change_history(tenant_id);
CREATE INDEX IF NOT EXISTS idx_modifier_history_group ON modifier_change_history(modifier_group_id) WHERE modifier_group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_modifier_history_modifier ON modifier_change_history(modifier_id) WHERE modifier_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_modifier_history_date ON modifier_change_history(created_at);
CREATE INDEX IF NOT EXISTS idx_modifier_history_field ON modifier_change_history(field_changed) WHERE field_changed IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_modifier_history_entity_type ON modifier_change_history(entity_type);

COMMENT ON TABLE modifier_change_history IS 'Historial de cambios en grupos de modificadores y modificadores individuales';
COMMENT ON COLUMN modifier_change_history.field_changed IS 'Campo que cambió: name, price, ingredient, is_available, min_qty, max_qty, products';
