"""HTTP wrapper around MCP tools for direct REST access."""
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Any

from app.api.auth import get_current_user
from app.models.user import User
import app.mcp.tools as tools

router = APIRouter()
logger = logging.getLogger(__name__)


class ToolCallRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.post("/call")
async def call_mcp_tool(
    body: ToolCallRequest,
    current_user: User = Depends(get_current_user),
):
    """Call any MCP tool via HTTP. user_id is injected from the auth token."""
    uid = str(current_user.id)
    args = {**body.arguments, "user_id": uid}
    name = body.tool

    TOOL_MAP = {
        "list_email_accounts": tools.list_email_accounts,
        "search_emails": tools.search_emails,
        "get_email": tools.get_email,
        "list_folders": tools.list_folders,
        "get_attachments": tools.get_attachments,
        "send_email": tools.send_email_tool,
        "draft_email": tools.draft_email_tool,
        "reply_to_email": tools.reply_to_email_tool,
        "archive_email": tools.archive_email_tool,
        "mark_email_read": tools.mark_email_read_tool,
        "create_email_rule": tools.create_email_rule_tool,
    }

    handler = TOOL_MAP.get(name)
    if not handler:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")

    try:
        result = await handler(**args)
        return {"tool": name, "result": result}
    except TypeError as e:
        logger.warning("Invalid MCP tool arguments for %s: %s", name, e)
        raise HTTPException(status_code=422, detail="Invalid tool arguments")
    except Exception:
        logger.exception("MCP tool call failed for %s", name)
        raise HTTPException(status_code=500, detail="Tool call failed")


@router.get("/tools")
async def list_mcp_tools(current_user: User = Depends(get_current_user)):
    """List all available MCP tools with their schemas."""
    return {
        "tools": [
            {"name": "list_email_accounts", "description": "List all connected email accounts", "scope": "read"},
            {"name": "search_emails", "description": "Search emails by keyword", "scope": "read"},
            {"name": "get_email", "description": "Get full email content by ID", "scope": "read"},
            {"name": "list_folders", "description": "List folders for an account", "scope": "read"},
            {"name": "get_attachments", "description": "Get attachment metadata for an email", "scope": "read"},
            {"name": "send_email", "description": "Send an email (supports dry_run)", "scope": "send"},
            {"name": "draft_email", "description": "Save email as draft", "scope": "draft"},
            {"name": "reply_to_email", "description": "Reply to an email (supports dry_run)", "scope": "send"},
            {"name": "archive_email", "description": "Archive an email", "scope": "manage"},
            {"name": "mark_email_read", "description": "Mark email as read/unread", "scope": "manage"},
            {"name": "create_email_rule", "description": "Create an email automation rule", "scope": "manage_rules"},
        ]
    }
