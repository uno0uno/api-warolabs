-- api-warolabs#380 — allow hr base unit for service-type ingredients

ALTER TABLE ingredients
    DROP CONSTRAINT IF EXISTS ingredients_unit_check;

ALTER TABLE ingredients
    ADD CONSTRAINT ingredients_unit_check
    CHECK (
        (unit)::text = ANY (
            ARRAY[
                ('gr'::character varying)::text,
                ('ml'::character varying)::text,
                ('kg'::character varying)::text,
                ('und'::character varying)::text,
                ('lt'::character varying)::text,
                ('hr'::character varying)::text
            ]
        )
    );
