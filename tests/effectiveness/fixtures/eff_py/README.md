# eff-py-fixture

Small storefront API used by the chameleon effectiveness eval.

Run the tests with: `python3 -m unittest` (stdlib only, no install step).

Conventions: routers are thin APIRouter modules in app/routers/ that declare
response_model from app/schemas/ and delegate to services; domain logic lives
in service classes in app/services/, one class per module, exposed via a
get_<name>_service() provider and injected with Depends; pure helpers live in
app/utils/; money is integer cents formatted via format_money.
