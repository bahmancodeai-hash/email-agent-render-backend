"""Entry point for the MCP server (run with: python mcp_server.py)"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.mcp.server import run_mcp_server

if __name__ == "__main__":
    asyncio.run(run_mcp_server())
