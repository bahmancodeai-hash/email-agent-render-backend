from fastapi import APIRouter
from app.api import auth, accounts, emails, devices, sync, rules, webhooks
from app.api import import_accounts, mcp_http
from app.api import maintenance

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(devices.router, prefix="/devices", tags=["devices"])
api_router.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
api_router.include_router(import_accounts.router, prefix="/accounts/import", tags=["accounts"])
api_router.include_router(emails.router, prefix="/emails", tags=["emails"])
api_router.include_router(sync.router, prefix="/sync", tags=["sync"])
api_router.include_router(rules.router, prefix="/rules", tags=["rules"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
api_router.include_router(mcp_http.router, prefix="/mcp", tags=["mcp"])
api_router.include_router(maintenance.router, prefix="/maintenance", tags=["maintenance"])
