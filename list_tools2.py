"""List all registered MCP tools."""
from code_sandbox_mcp.server import mcp
import json

# Try different ways to access tools
for attr in sorted(dir(mcp)):
    if attr.endswith('ools') or 'tool' in attr:
        try:
            val = getattr(mcp, attr)
            if callable(val):
                print(f"{attr} -> callable")
            else:
                print(f"{attr} -> {type(val).__name__}")
        except Exception as e:
            print(f"{attr} -> ERROR: {e}")
