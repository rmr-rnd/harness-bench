"""Convert between BFCL function doc format and OpenAI tools format."""
from __future__ import annotations

import json
from typing import Any


def _fix_type(obj: Any) -> Any:
    """Recursively replace BFCL 'dict' type with JSON Schema 'object'."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k == "type" and v == "dict":
                result[k] = "object"
            else:
                result[k] = _fix_type(v)
        return result
    if isinstance(obj, list):
        return [_fix_type(i) for i in obj]
    return obj


def bfcl_doc_to_openai_tool(doc: dict) -> dict:
    """Convert one BFCL function doc → OpenAI tool definition."""
    params = _fix_type(doc.get("parameters", {"type": "object", "properties": {}}))
    # Remove BFCL-specific 'response' key that OpenAI doesn't understand
    return {
        "type": "function",
        "function": {
            "name": doc["name"],
            "description": doc.get("description", ""),
            "parameters": params,
        },
    }


def bfcl_docs_to_openai_tools(docs: list[dict]) -> list[dict]:
    return [bfcl_doc_to_openai_tool(d) for d in docs]


def tool_calls_to_bfcl_calls(tool_calls) -> list[dict]:
    """Convert OpenAI tool_calls → BFCL [{func_name: {params}}] format."""
    result = []
    for tc in tool_calls:
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
        except Exception:
            args = {}
        result.append({name: args})
    return result


def tool_calls_to_call_strings(tool_calls) -> list[str]:
    """Convert OpenAI tool_calls → executable call strings like 'func(a=1, b="x")'."""
    result = []
    for tc in tool_calls:
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
        except Exception:
            args = {}
        parts = ", ".join(f"{k}={repr(v)}" for k, v in args.items())
        result.append(f"{name}({parts})")
    return result
