"""core: backoffice — operator command layer (batch A).

HTTP route → strict request validation → command here → ONE DB
transaction (business mutation + operator_audit together or not at
all). Route layers never mutate directly. Mechanisms migrated from the
retired app/routes/samelabs.py monolith with behavior preserved:

  credits.py     operator credits adjustment (stable action id,
                 billing grant/consume, ledger idem, same-tx audit)
  tools.py       tool pricing / presentation / operator HTTP tool
                 create+delete (tool_supply + pricing single points)
  products.py    plan + package create/update with Creem price mirror
                 and DB commit-outcome reconciliation
  withdrawals.py thin re-exports of the economy S4 command set
"""
