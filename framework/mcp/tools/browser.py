"""Browser tool group: web_browser_* via inspect-tool-support."""
from __future__ import annotations

from framework.mcp.types import MCPTool

# Maps tool name → (inspect-tool-support method, required param keys)
_METHOD_MAP: dict[str, tuple[str, list[str]]] = {
    "web_browser_go":           ("web_go",          ["url"]),
    "web_browser_click":        ("web_click",       ["element_id"]),
    "web_browser_type":         ("web_type",        ["element_id", "text"]),
    "web_browser_type_submit":  ("web_type_submit", ["element_id", "text"]),
    "web_browser_scroll":       ("web_scroll",      ["direction"]),
    "web_browser_back":         ("web_back",        []),
    "web_browser_forward":      ("web_forward",     []),
    "web_browser_refresh":      ("web_refresh",     []),
}


def _make_handler(its_method: str, param_keys: list[str]):
    async def handler(args: dict, bridge) -> str:
        params = {k: args[k] for k in param_keys if k in args}
        return await bridge.web_browser(its_method, params)
    return handler


TOOLS: list[MCPTool] = [
    MCPTool(
        name="web_browser_go",
        description=(
            "Navigate the web browser to a URL. "
            "Returns an accessibility tree of the resulting page."
        ),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to navigate to"}},
            "required": ["url"],
        },
        handler=_make_handler("web_go", ["url"]),
    ),
    MCPTool(
        name="web_browser_click",
        description="Click an element on the currently displayed web page.",
        parameters={
            "type": "object",
            "properties": {"element_id": {"type": "integer", "description": "ID of the element to click"}},
            "required": ["element_id"],
        },
        handler=_make_handler("web_click", ["element_id"]),
    ),
    MCPTool(
        name="web_browser_type",
        description="Type text into an input element on the current web page.",
        parameters={
            "type": "object",
            "properties": {
                "element_id": {"type": "integer", "description": "ID of the input element"},
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["element_id", "text"],
        },
        handler=_make_handler("web_type", ["element_id", "text"]),
    ),
    MCPTool(
        name="web_browser_type_submit",
        description="Type text into a form input and press ENTER to submit.",
        parameters={
            "type": "object",
            "properties": {
                "element_id": {"type": "integer", "description": "ID of the input element"},
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["element_id", "text"],
        },
        handler=_make_handler("web_type_submit", ["element_id", "text"]),
    ),
    MCPTool(
        name="web_browser_scroll",
        description='Scroll the web browser up or down by one page.',
        parameters={
            "type": "object",
            "properties": {"direction": {"type": "string", "description": '"up" or "down"'}},
            "required": ["direction"],
        },
        handler=_make_handler("web_scroll", ["direction"]),
    ),
    MCPTool(
        name="web_browser_back",
        description="Navigate the web browser back in history.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_make_handler("web_back", []),
    ),
    MCPTool(
        name="web_browser_forward",
        description="Navigate the web browser forward in history.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_make_handler("web_forward", []),
    ),
    MCPTool(
        name="web_browser_refresh",
        description="Refresh the current web page.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_make_handler("web_refresh", []),
    ),
]
