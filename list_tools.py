import inspect
from code_sandbox_mcp.server import mcp
# FastMCP uses ._tool_manager in some versions
for attr in dir(mcp):
    if 'tool' in attr.lower():
        print(f"{attr}: {type(getattr(mcp, attr))}")
