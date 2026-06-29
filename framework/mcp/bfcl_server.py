"""BFCL MCP HTTP server — wraps BFCL Python backends as MCP tools for Hermes.

No Docker bridge needed: backends run in-process on the host.
Hermes reaches this server via host.docker.internal:{port}.

Architecture:
  Hermes Agent (Docker)
      ↓  MCP tools/call
  BFCLMCPServer (host, FastAPI)
      ↓  Python method call
  BFCL backends (GorillaFileSystem, TradingBot, …)

The server tracks all tool calls per-turn so the orchestrator can
compare them against ground truth after each Hermes run completes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from framework.mcp.http_server import _free_port

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-11-25"


def _fix_type(obj: Any) -> Any:
    """Replace BFCL 'dict' type with JSON Schema 'object' recursively."""
    if isinstance(obj, dict):
        return {k: ("object" if k == "type" and v == "dict" else _fix_type(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fix_type(i) for i in obj]
    return obj


_MOCK_FIELD_DEFAULTS: dict[str, Any] = {
    # weather
    "temperature": 22, "temp": 22, "humidity": 60, "condition": "clear",
    "feels_like": 21, "wind_speed": 10, "wind_direction": "N",
    # location
    "city": None, "country": None, "timezone": "UTC", "lat": 0.0, "lon": 0.0,
    # finance
    "price": 150.0, "open": 149.0, "close": 151.0, "high": 152.0, "low": 148.0,
    "volume": 1000000, "change": 1.0, "change_percent": 0.67,
    # travel / hotel
    "rating": 4.2, "stars": 4, "available": True, "rooms": 5, "price_per_night": 120.0,
    # general
    "status": "success", "message": "OK", "count": 1, "total": 1,
    "results": [], "items": [], "data": None, "id": "mock-001",
    "name": None, "description": None, "url": None, "error": None,
}


def _mock_response(func_name: str, args: dict, func_doc: dict | None) -> dict:
    """
    Build a plausible mock response for a fictional BFCL function.

    Strategy:
    1. Echo each input arg as a field in the response (agent sees its inputs confirmed).
    2. Add common output fields guessed from function name keywords.
    3. Wrap in {"status": "success", "result": {...}}.
    """
    result: dict[str, Any] = {}

    # Echo inputs so agent knows the call succeeded with the right params
    result.update(args)

    # Add typical output fields based on function name tokens
    name_lower = func_name.lower()
    keyword_fields: list[str] = []
    if any(w in name_lower for w in ("weather", "forecast", "climate")):
        keyword_fields = ["temperature", "condition", "humidity", "wind_speed"]
    elif any(w in name_lower for w in ("stock", "quote", "price", "trade")):
        keyword_fields = ["price", "change", "change_percent", "volume"]
    elif any(w in name_lower for w in ("hotel", "room", "accommodation")):
        keyword_fields = ["rating", "available", "price_per_night", "rooms"]
    elif any(w in name_lower for w in ("flight", "travel", "trip")):
        keyword_fields = ["available", "price", "duration", "airline"]
    elif any(w in name_lower for w in ("search", "find", "list", "lookup")):
        keyword_fields = ["count", "results"]
    elif any(w in name_lower for w in ("get", "fetch", "retrieve")):
        keyword_fields = ["status", "data"]
    elif any(w in name_lower for w in ("multiply", "add", "subtract", "divide", "calc")):
        # Try to compute arithmetic result if args are numeric
        vals = [v for v in args.values() if isinstance(v, (int, float))]
        if "multiply" in name_lower and len(vals) >= 2:
            result["result"] = vals[0] * vals[1]
        elif "add" in name_lower and len(vals) >= 2:
            result["result"] = vals[0] + vals[1]
        elif "subtract" in name_lower and len(vals) >= 2:
            result["result"] = vals[0] - vals[1]
        elif "divide" in name_lower and len(vals) >= 2 and vals[1] != 0:
            result["result"] = vals[0] / vals[1]
        return {"status": "success", "result": result}

    for field in keyword_fields:
        if field not in result:
            default = _MOCK_FIELD_DEFAULTS.get(field)
            # Propagate string arg values where field name matches
            if default is None:
                for k, v in args.items():
                    if k in field or field in k:
                        default = v
                        break
            if default is not None:
                result[field] = default

    return {"status": "success", "result": result}


class BFCLMCPServer:
    """
    FastAPI MCP server that exposes BFCL backend methods as tools.

    Lifecycle:
        port = await server.start()
        # configure Hermes to point at http://host.docker.internal:{port}
        server.begin_turn()
        # let Hermes run one turn
        calls = server.end_turn()   # list of 'func(arg=val)' strings
        await server.stop()
    """

    def __init__(
        self,
        func_docs: list[dict],
        involved_classes: list[str],
        initial_config: dict,
        long_context: bool = False,
        task_id: str = "",
        withheld: set[str] | None = None,
        step_cb=None,
        n_expected: int = 0,
        llm_client=None,
        llm_model: str = "",
    ) -> None:
        self._func_docs = func_docs
        self._func_doc_map: dict[str, dict] = {d["name"]: d for d in func_docs}
        self._withheld: set[str] = set(withheld or [])
        self._step_cb = step_cb
        self._n_expected: int = n_expected  # 0 = no limit (multi-turn)
        self._llm_client = llm_client
        self._llm_model = llm_model
        self._mock_cache: dict[str, str] = {}  # (name, args_key) → result_text

        # Initialise Python backends in-process
        from framework.benchmarks.bfcl._shared.multi_turn import _load_instances, _build_method_map
        self._instance_store: dict[str, Any] = {}
        instances = _load_instances(
            involved_classes, initial_config, self._instance_store,
            suffix=f"_hermes_{task_id}", long_context=long_context,
        )
        self._instances = instances  # keep reference for flush_memory()
        self._method_map = _build_method_map(instances)

        # Per-turn call tracking
        self._turn_calls: list[str] = []
        self._frozen: bool = False  # when True, calls are answered but not recorded

        # Fires when enough calls have been made (n_expected reached, or first call if n=0)
        self.quota_reached_event: asyncio.Event = asyncio.Event()

        self._port: int | None = None
        self._server_task: asyncio.Task | None = None
        self.app = self._build_app()

    # ── Public API ────────────────────────────────────────────────────

    def flush_memory(self) -> None:
        """Persist memory state to disk after a prereq conversation."""
        for instance in self._instances.values():
            if hasattr(instance, "_flush_memory_to_local_file"):
                instance._flush_memory_to_local_file()

    def get_core_memory_content(self) -> str:
        """Return current core memory content for injection into system prompt."""
        for instance in self._instances.values():
            if hasattr(instance, "_dump_core_memory_to_context"):
                return instance._dump_core_memory_to_context()
        return ""

    def reveal_functions(self, names: list[str]) -> None:
        """Make withheld functions visible (miss_func category)."""
        for n in names:
            self._withheld.discard(n)

    def freeze(self) -> None:
        """Stop recording new calls (answer them but ignore for evaluation)."""
        self._frozen = True

    def begin_turn(self, n_expected: int = 0) -> None:
        """Reset per-turn call log before a new Hermes run. n_expected=0 means no limit."""
        self._turn_calls = []
        self._frozen = False
        self._n_expected = n_expected
        self.quota_reached_event.clear()

    def end_turn(self) -> list[str]:
        """Return call strings made since begin_turn()."""
        return list(self._turn_calls)

    async def start(self) -> int:
        """Start HTTP server on a free host port. Returns the port."""
        import uvicorn
        self._port = _free_port()
        config = uvicorn.Config(
            self.app, host="0.0.0.0", port=self._port,
            log_level="warning", access_log=False,
        )
        server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(server.serve())
        for _ in range(50):
            await asyncio.sleep(0.1)
            if server.started:
                break
        logger.info("BFCL MCP server started on port %d", self._port)
        return self._port

    async def stop(self) -> None:
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("BFCL MCP server stopped")

    @property
    def port(self) -> int | None:
        return self._port

    # ── FastAPI app ───────────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="BFCL MCP Server")

        @app.post("/mcp")
        @app.post("/")
        async def handle(request: Request) -> Response:
            body = await request.json()
            resp = await self._dispatch(body)
            if resp is None:
                return Response(status_code=202)
            return JSONResponse(content=resp)

        return app

    async def _dispatch(self, body: dict) -> dict | None:
        method: str = body.get("method", "")
        req_id: Any = body.get("id")
        params: dict = body.get("params") or {}

        # Notifications have no id — acknowledge silently
        if req_id is None:
            return None

        if method == "initialize":
            return self._ok(req_id, {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "bfcl-backend", "version": "1.0"},
            })

        if method == "tools/list":
            return self._ok(req_id, {"tools": self._tool_list()})

        if method == "tools/call":
            return await self._call_tool(req_id, params)

        return self._err(req_id, -32601, f"Method not found: {method}")

    # ── Tool registry ─────────────────────────────────────────────────

    def _tool_list(self) -> list[dict]:
        tools = []
        for doc in self._func_docs:
            if doc["name"] in self._withheld:
                continue
            schema = _fix_type(
                doc.get("parameters", {"type": "object", "properties": {}})
            )
            # Add request_heartbeat so Hermes (Letta) recognises the tool as compatible.
            # Letta uses this to control its agentic loop: False = agent is done.
            props = schema.setdefault("properties", {})
            props["request_heartbeat"] = {
                "type": "boolean",
                "description": "Set to false when the task is complete.",
            }
            tools.append({
                "name": doc["name"],
                "description": doc.get("description", ""),
                "inputSchema": schema,
            })
        return tools

    async def _llm_mock(self, name: str, args: dict) -> str:
        """Generate a realistic JSON response for a fictional function via LLM.

        Falls back to the heuristic mock if the LLM client is unavailable or fails.
        Results are cached by (name, args) so repeated identical calls are instant.
        """
        cache_key = f"{name}:{json.dumps(args, sort_keys=True)}"
        if cache_key in self._mock_cache:
            return self._mock_cache[cache_key]

        if self._llm_client and self._llm_model:
            func_doc = self._func_doc_map.get(name, {})
            description = func_doc.get("description", "")
            system = (
                "You are a function executor simulator. "
                "When given a function name, its description, and the arguments it was called with, "
                "you return ONLY a valid JSON object that this function would realistically return. "
                "Make the response specific and data-rich — include concrete values, not nulls. "
                "No explanation, no markdown, just the raw JSON."
            )
            user = (
                f"Function: {name}\n"
                f"Description: {description}\n"
                f"Called with: {json.dumps(args, ensure_ascii=False)}\n\n"
                "Return the JSON response this function would produce."
            )
            try:
                resp = await self._llm_client.chat.completions.create(
                    model=self._llm_model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.3,
                    max_tokens=512,
                )
                result_text = resp.choices[0].message.content.strip()
                # Strip markdown code fences if model wrapped the JSON
                if result_text.startswith("```"):
                    result_text = result_text.split("```")[1]
                    if result_text.startswith("json"):
                        result_text = result_text[4:]
                    result_text = result_text.strip()
                # Validate it's parseable JSON
                json.loads(result_text)
                self._mock_cache[cache_key] = result_text
                return result_text
            except Exception as exc:
                logger.debug("LLM mock generation failed for %s: %s", name, exc)

        # Fallback: heuristic mock
        result_text = json.dumps(_mock_response(name, args, self._func_doc_map.get(name)))
        self._mock_cache[cache_key] = result_text
        return result_text

    async def _call_tool(self, req_id: Any, params: dict) -> dict:
        name: str = params.get("name", "")
        args: dict = params.get("arguments") or {}

        if self._step_cb:
            self._step_cb("tool_call", {"name": name, "args": args})

        # Strip Letta heartbeat param — not part of BFCL function signatures
        backend_args = {k: v for k, v in args.items() if k != "request_heartbeat"}

        if name not in self._method_map:
            # No real backend (single-turn fictional schemas) — generate realistic response.
            result_text = await self._llm_mock(name, backend_args)
        else:
            try:
                result = self._method_map[name](**backend_args)
                result_text = json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as exc:
                result_text = f"Error: {exc}"

        # Record call string for evaluation (without Letta's request_heartbeat)
        if not self._frozen:
            parts = ", ".join(f"{k}={repr(v)}" for k, v in backend_args.items())
            self._turn_calls.append(f"{name}({parts})")
            # Freeze and signal when we've captured the expected number of calls
            n = self._n_expected
            if n > 0 and len(self._turn_calls) >= n:
                self._frozen = True
                self.quota_reached_event.set()
            elif n == 0:
                # No limit set — signal on first call (used when caller manages freezing)
                self.quota_reached_event.set()

        if self._step_cb:
            self._step_cb("tool_result", result_text)

        return self._ok(req_id, {
            "content": [{"type": "text", "text": result_text}],
            "isError": False,
        })

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _ok(req_id: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _err(req_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}
