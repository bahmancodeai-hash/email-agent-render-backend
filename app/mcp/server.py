import asyncio
import json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from app.mcp.tools import (
    list_email_accounts,
    search_emails,
    get_email,
    list_folders,
    get_attachments,
    send_email_tool,
    draft_email_tool,
    reply_to_email_tool,
    archive_email_tool,
    mark_email_read_tool,
    create_email_rule_tool,
)

server = Server("email-agent")


async def _get_local_user_id() -> str:
    from app.database import AsyncSessionLocal
    from app.services.auth_service import get_or_create_local_app_user

    async with AsyncSessionLocal() as db:
        user = await get_or_create_local_app_user(db)
        await db.commit()
        return str(user.id)

TOOL_DEFINITIONS = [
    types.Tool(
        name="list_email_accounts",
        description="List all connected email accounts",
        inputSchema={
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    ),
    types.Tool(
        name="search_emails",
        description="Search emails across all accounts or a specific account",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "query": {"type": "string"},
                "account_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20, "maximum": 100},
            },
            "required": ["user_id", "query"],
        },
    ),
    types.Tool(
        name="get_email",
        description="Get full content of a specific email by ID",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "message_id": {"type": "string"},
            },
            "required": ["user_id", "message_id"],
        },
    ),
    types.Tool(
        name="list_folders",
        description="List folders for a specific email account",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "account_id": {"type": "string"},
            },
            "required": ["user_id", "account_id"],
        },
    ),
    types.Tool(
        name="get_attachments",
        description="Get attachment metadata for an email",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "message_id": {"type": "string"},
            },
            "required": ["user_id", "message_id"],
        },
    ),
    types.Tool(
        name="send_email",
        description="Send an email. Use dry_run=true to validate without sending.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "account_id": {"type": "string"},
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body_text": {"type": "string"},
                "body_html": {"type": "string"},
                "cc": {"type": "array", "items": {"type": "string"}},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["user_id", "account_id", "to", "subject"],
        },
    ),
    types.Tool(
        name="draft_email",
        description="Save an email as a draft",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "account_id": {"type": "string"},
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body_text": {"type": "string"},
            },
            "required": ["user_id", "account_id", "to", "subject"],
        },
    ),
    types.Tool(
        name="reply_to_email",
        description="Reply to an existing email. Use dry_run=true to validate.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "message_id": {"type": "string"},
                "body_text": {"type": "string"},
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["user_id", "message_id", "body_text"],
        },
    ),
    types.Tool(
        name="archive_email",
        description="Move an email to the archive folder",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "message_id": {"type": "string"},
            },
            "required": ["user_id", "message_id"],
        },
    ),
    types.Tool(
        name="mark_email_read",
        description="Mark an email as read or unread",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "message_id": {"type": "string"},
                "is_read": {"type": "boolean", "default": True},
            },
            "required": ["user_id", "message_id"],
        },
    ),
    types.Tool(
        name="create_email_rule",
        description="Create an email automation rule (e.g. move emails from X to folder Y)",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "name": {"type": "string"},
                "conditions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string", "enum": ["from", "to", "subject", "body", "has_attachment"]},
                            "operator": {"type": "string", "enum": ["contains", "equals", "starts_with", "ends_with", "is_true"]},
                            "value": {"type": "string"},
                        },
                        "required": ["field", "operator"],
                    },
                },
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["move_to_folder", "mark_read", "mark_flagged", "delete", "label", "forward_to", "auto_reply"]},
                            "value": {"type": "string"},
                        },
                        "required": ["type"],
                    },
                },
                "account_id": {"type": "string"},
                "conditions_match": {"type": "string", "enum": ["all", "any"], "default": "all"},
                "stop_processing": {"type": "boolean", "default": False},
            },
            "required": ["user_id", "name", "conditions", "actions"],
        },
    ),
]

for tool in TOOL_DEFINITIONS:
    schema = tool.inputSchema
    schema.get("properties", {}).pop("user_id", None)
    if "required" in schema:
        schema["required"] = [name for name in schema["required"] if name != "user_id"]


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return TOOL_DEFINITIONS


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        uid = await _get_local_user_id()
        if name == "list_email_accounts":
            result = await list_email_accounts(uid)
        elif name == "search_emails":
            result = await search_emails(uid, arguments["query"], arguments.get("account_id"), arguments.get("limit", 20))
        elif name == "get_email":
            result = await get_email(uid, arguments["message_id"])
        elif name == "list_folders":
            result = await list_folders(uid, arguments["account_id"])
        elif name == "get_attachments":
            result = await get_attachments(uid, arguments["message_id"])
        elif name == "send_email":
            result = await send_email_tool(uid, arguments["account_id"], arguments["to"], arguments["subject"], arguments.get("body_text"), arguments.get("body_html"), arguments.get("cc"), arguments.get("dry_run", False))
        elif name == "draft_email":
            result = await draft_email_tool(uid, arguments["account_id"], arguments["to"], arguments["subject"], arguments.get("body_text"))
        elif name == "reply_to_email":
            result = await reply_to_email_tool(uid, arguments["message_id"], arguments["body_text"], arguments.get("dry_run", False))
        elif name == "archive_email":
            result = await archive_email_tool(uid, arguments["message_id"])
        elif name == "mark_email_read":
            result = await mark_email_read_tool(uid, arguments["message_id"], arguments.get("is_read", True))
        elif name == "create_email_rule":
            result = await create_email_rule_tool(
                uid,
                arguments["name"],
                arguments["conditions"],
                arguments["actions"],
                arguments.get("account_id"),
                arguments.get("conditions_match", "all"),
                arguments.get("stop_processing", False),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}

        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def run_mcp_server():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run_mcp_server())
