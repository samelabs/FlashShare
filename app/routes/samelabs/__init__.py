"""app.routes.samelabs — new back-office route package (batch A).

app.main keeps
the SAME registration:
    from app.routes import samelabs
    app.include_router(samelabs.router)

Split:
    common.py    operator session gate + page context + presentation
    overview.py  /samelabs                    truthful asset-separated dashboard
    users.py     /samelabs/users              user rows + credits adjustment
    tools.py     /samelabs/tools              tool supply (merged Tools+Remote)
                                              + tool detail /samelabs/tools/{key}
    content.py   /samelabs/content            shared files + /r results governance
    calls.py     /samelabs/calls              tool call log
    commerce.py  /samelabs/commerce/*         orders / memberships / products
    economy.py   /samelabs/economy/*          credits / earnings / withdrawals
                                              / platform income
    audit.py     /samelabs/audit              append-only audit trail
"""
from fastapi import APIRouter

from app.routes.samelabs import (overview, users, tools, content, calls,  # noqa: F401
                                 commerce, economy, audit)

router = APIRouter()
router.include_router(overview.router)
router.include_router(users.router)
router.include_router(tools.router)
router.include_router(content.router)
router.include_router(calls.router)
router.include_router(commerce.router)
router.include_router(economy.router)
router.include_router(audit.router)
