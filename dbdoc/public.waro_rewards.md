# public.waro_rewards

## Columns

| Name | Type | Default | Nullable | Children | Parents | Comment |
| ---- | ---- | ------- | -------- | -------- | ------- | ------- |
| id | uuid | gen_random_uuid() | false | [public.order_waro_redemptions](public.order_waro_redemptions.md) |  |  |
| tenant_id | uuid |  | false |  | [public.tenants](public.tenants.md) |  |
| name | varchar(120) |  | false |  |  |  |
| reward_type | varchar(20) |  | false |  |  | fixed_cop_off \| free_product |
| waros_cost | integer |  | false |  |  |  |
| fixed_cop_off | numeric(12,2) |  | true |  |  |  |
| product_id | uuid |  | true |  | [public.product](public.product.md) |  |
| is_active | boolean | true | false |  |  |  |
| created_at | timestamptz | now() | false |  |  |  |
| updated_at | timestamptz | now() | false |  |  |  |

---
> api-warolabs#370
