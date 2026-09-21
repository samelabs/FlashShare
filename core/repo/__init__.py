# core/repo - data layer (batch 1b)
#
# One repo file per domain. Functions are named queries: (con, ...) -> rows.
# Rules:
#   1. No transactions: never commit/rollback nor open `with db()` here;
#      transaction boundaries belong to domain services (batch 2).
#   2. No business rules: validation/aggregation semantics stay in services.
#   3. Parameterized: every value goes through %s; column names for
#      update_tool_row come from service-layer whitelists, never user input.
#   4. Domain isolation: core/<domain>/ may only import its own repo file
#      (assertion A4).
#
# Transitional (batch 1): existing core/*.py services call repo in place;
# batch 2 moves them under core/<domain>/.
