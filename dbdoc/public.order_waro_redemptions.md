# public.order_waro_redemptions

## Columns

| Name | Type | Default | Nullable | Children | Parents | Comment |
| ---- | ---- | ------- | -------- | -------- | ------- | ------- |
| id | uuid | gen_random_uuid() | false |  |  |  |
| order_id | uuid |  | false |  | [public.orders](public.orders.md) |  |
| tenant_id | uuid |  | false |  | [public.tenants](public.tenants.md) |  |
| redemption_type | varchar(20) |  | false |  |  | points_cop \| reward_fixed_cop \| reward_free_product |
| waros_spent | integer |  | false |  |  |  |
| cop_discount | numeric(12,2) | 0 | false |  |  |  |
| waro_reward_id | uuid |  | true |  | [public.waro_rewards](public.waro_rewards.md) |  |
| order_item_id | uuid |  | true |  | [public.order_items](public.order_items.md) | B2 free product line |
| created_at | timestamptz | now() | false |  |  |  |

---
> api-warolabs#370
