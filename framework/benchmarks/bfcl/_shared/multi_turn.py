"""BFCL multi-turn execution engine.

Ported from:
  bfcl_eval/eval_checker/multi_turn_eval/multi_turn_utils.py
  bfcl_eval/eval_checker/multi_turn_eval/multi_turn_checker.py

Runs the model turn-by-turn against simulated backend instances
(GorillaFileSystem, MathAPI, TwitterAPI, etc.), feeding execution
results back into the conversation each turn.
"""
from __future__ import annotations

import copy
import importlib
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

# Add backends to path so backends can import each other
_BACKENDS_DIR = str(Path(__file__).parent / "backends")
if _BACKENDS_DIR not in sys.path:
    sys.path.insert(0, _BACKENDS_DIR)

CLASS_FILE_MAPPING: dict[str, tuple[str, str]] = {
    "GorillaFileSystem": ("gorilla_file_system", "GorillaFileSystem"),
    "MathAPI": ("math_api", "MathAPI"),
    "MessageAPI": ("message_api", "MessageAPI"),
    "TwitterAPI": ("posting_api", "TwitterAPI"),
    "TicketAPI": ("ticket_api", "TicketAPI"),
    "TradingBot": ("trading_bot", "TradingBot"),
    "TravelAPI": ("travel_booking", "TravelAPI"),
    "VehicleControlAPI": ("vehicle_control", "VehicleControlAPI"),
    "MemoryAPI_kv": ("memory_kv", "MemoryAPI_kv"),
    "MemoryAPI_vector": ("memory_vector", "MemoryAPI_vector"),
    "MemoryAPI_rec_sum": ("memory_rec_sum", "MemoryAPI_rec_sum"),
}

STATELESS_CLASSES = frozenset({"MathAPI"})

DANGEROUS_CALLS = frozenset({"kill", "exit", "quit", "remove", "unlink", "popen", "Popen", "run"})


def _load_instances(
    involved_classes: list[str],
    initial_config: dict[str, Any],
    instance_store: dict[str, Any],
    suffix: str = "",
    long_context: bool = False,
) -> dict[str, Any]:
    """Load and initialise backend instances, reusing existing ones across turns."""
    instances: dict[str, Any] = {}
    for class_name in involved_classes:
        key = f"{class_name}{suffix}"
        if key not in instance_store:
            module_name, cls_name = CLASS_FILE_MAPPING[class_name]
            module = importlib.import_module(module_name)
            cls = getattr(module, cls_name)
            instance = cls()
            if class_name not in STATELESS_CLASSES:
                cfg = copy.deepcopy(initial_config.get(class_name, {}))
                instance._load_scenario(cfg, long_context=long_context)
            instance_store[key] = instance
        instances[class_name] = instance_store[key]
    return instances


def _build_method_map(instances: dict[str, Any]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for instance in instances.values():
        for name, method in inspect.getmembers(instance, predicate=inspect.ismethod):
            if not name.startswith("_"):
                mapping[name] = method
    return mapping


def _prepend_instance(func_call: str, instance_name: str, method_map: dict[str, Any]) -> str:
    """Prepend instance.method(...) for each method name found in the call."""
    def replace(match: re.Match) -> str:
        name = match.group(1)
        return f"{instance_name}.{name}" if name in method_map else name
    return re.sub(r"\b([a-zA-Z_]\w*)\s*(?=\()", replace, func_call)


def execute_func_calls(
    func_calls: list[str],
    method_map: dict[str, Any],
) -> list[str]:
    """Execute a list of function call strings and return their results."""
    results: list[str] = []
    for call in func_calls:
        # Safety check
        fn = call.split("(")[0].strip().split(".")[-1]
        if fn in DANGEROUS_CALLS:
            results.append(f"Error: {fn} is not allowed")
            continue
        try:
            method_name = re.match(r"([a-zA-Z_]\w*)\s*\(", call)
            if method_name and method_name.group(1) in method_map:
                method = method_map[method_name.group(1)]
                # Parse args from the call string
                result = _eval_call(call, method_map)
            else:
                result = _eval_call(call, method_map)
            if isinstance(result, dict):
                try:
                    result = json.dumps(result)
                except Exception:
                    result = str(result)
            else:
                result = str(result)
            results.append(result)
        except Exception as e:
            results.append(f"Error during execution: {e}")
    return results


def _eval_call(call: str, method_map: dict[str, Any]) -> Any:
    """Eval a function call string against the method map namespace."""
    return eval(call, {"__builtins__": {}}, method_map)  # noqa: S307


# ─── Checker (ported from multi_turn_checker.py) ─────────────────────────────

def _compare_instances(model_obj: Any, gt_obj: Any) -> tuple[bool, dict]:
    """Check non-private attributes match. Mirrors original _compare_instances."""
    differences: dict[str, Any] = {}
    valid = True
    for attr_name in vars(gt_obj):
        if attr_name.startswith("_"):
            continue
        model_attr = getattr(model_obj, attr_name)
        gt_attr = getattr(gt_obj, attr_name)
        if model_attr != gt_attr:
            valid = False
            differences[attr_name] = {"model": model_attr, "ground_truth": gt_attr}
    return valid, differences


def _state_checker(model_instances: dict, gt_instances: dict) -> dict:
    """Mirrors original state_checker."""
    for class_name, gt_inst in gt_instances.items():
        model_inst = model_instances[class_name]
        valid, differences = _compare_instances(model_inst, gt_inst)
        if not valid:
            return {
                "valid": False,
                "error_message": f"Model instance for {class_name} does not match the state with ground truth instance.",
                "error_type": "multi_turn:instance_state_mismatch",
                "differences": differences,
            }
    return {"valid": True}


def _is_subsequence_unordered(gt_list: list, model_list: list) -> tuple[bool, list]:
    """Check all GT items are present in model results (unordered). Mirrors original."""
    model_copy = list(model_list)
    missing = []
    for item in gt_list:
        try:
            model_copy.remove(item)
        except ValueError:
            missing.append(item)
    return len(missing) == 0, missing


def _response_checker(
    all_model_results: list[str],
    gt_results: list[str],
    turn_idx: int,
) -> dict:
    """Mirrors original response_checker: GT results must be subset of model results."""
    ok, missing = _is_subsequence_unordered(gt_results, all_model_results)
    if not ok:
        return {
            "valid": False,
            "error_message": (
                f"Model response execution results so far does not contain all the "
                f"ground truth response execution results for turn {turn_idx}."
            ),
            "error_type": "multi_turn:execution_response_mismatch",
            "missing_items": missing,
        }
    return {"valid": True}


def multi_turn_check(
    per_turn_model_calls: list[list[str]],
    per_turn_gt_calls: list[list[str]],
    initial_config: dict,
    involved_classes: list[str],
    task_id: str,
    long_context: bool = False,
) -> tuple[bool, str]:
    """
    Full multi-turn correctness check matching the original multi_turn_checker.

    Args:
        per_turn_model_calls: model function calls per turn (list of call strings per turn)
        per_turn_gt_calls:    ground truth calls per turn
        initial_config:       initial backend state
        involved_classes:     which backend classes are involved
        task_id:              used to namespace instances
        long_context:         whether to use long_context init

    Returns:
        (correct: bool, explanation: str)
    """
    model_store: dict[str, Any] = {}
    gt_store: dict[str, Any] = {}
    all_model_results: list[str] = []

    for turn_idx, gt_calls in enumerate(per_turn_gt_calls):
        model_calls = per_turn_model_calls[turn_idx] if turn_idx < len(per_turn_model_calls) else []

        # Execute model calls
        model_instances = _load_instances(involved_classes, initial_config, model_store,
                                          suffix=f"_model_{task_id}", long_context=long_context)
        model_map = _build_method_map(model_instances)
        model_results = execute_func_calls(model_calls, model_map)
        all_model_results.extend(model_results)

        # Execute GT calls
        gt_instances = _load_instances(involved_classes, initial_config, gt_store,
                                       suffix=f"_gt_{task_id}", long_context=long_context)
        gt_map = _build_method_map(gt_instances)
        gt_results = execute_func_calls(gt_calls, gt_map)

        # If GT is empty, this is a miss_func turn — model should not call anything
        if not gt_calls:
            if model_calls and any(c.strip() for c in model_calls):
                return False, f"Turn {turn_idx}: model made calls when it should not (miss_func turn)"
            continue

        # Model must have called something
        if not model_calls:
            return False, f"Turn {turn_idx}: model made no calls but GT expects {gt_calls}"

        # State check
        state_result = _state_checker(model_instances, gt_instances)
        if not state_result["valid"]:
            return False, state_result["error_message"]

        # Response check
        resp_result = _response_checker(all_model_results, gt_results, turn_idx)
        if not resp_result["valid"]:
            return False, resp_result["error_message"]

    return True, "All turns correct"


# ─── Execution loop (called during task run) ─────────────────────────────────

def parse_func_calls(text: str) -> list[str]:
    """Parse '[func(a=1), func2(b=2)]' → ['func(a=1)', 'func2(b=2)']"""
    text = text.strip()
    if not text.startswith("["):
        return []
    # Strip outer brackets and split on top-level commas
    inner = text[1:-1].strip()
    calls: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                calls.append(part)
            current = []
        else:
            current.append(ch)
    part = "".join(current).strip()
    if part:
        calls.append(part)
    return calls


MULTITURN_SYSTEM_PROMPT = (
    "You are an expert in composing functions. You are given a question and a set of possible functions. "
    "Based on the question, you will need to make one or more function/tool calls to achieve the purpose. "
    "If none of the functions can be used, point it out. If the given question lacks the parameters required "
    "by the function, also point it out. You should only return the function calls in your response.\n\n"
    "If you decide to invoke any of the function(s), you MUST put it in the format of "
    "[func_name1(params_name1=params_value1, params_name2=params_value2...), func_name2(params)]. "
    "You SHOULD NOT include any other text in the response.\n\n"
    "At each turn, you should try your best to complete the tasks requested by the user within the current turn. "
    "Continue to output functions to call until you have fulfilled the user's request to the best of your ability. "
    "Once you have no more functions to call, the system will consider the current turn complete and proceed to "
    "the next turn or task.\n\n"
    "Here is a list of functions in JSON format that you can invoke.\n{functions}\n"
)

# Sent as user message when new functions are revealed (miss_func category)
ADDITIONAL_FUNCTION_PROMPT = "{functions}\nI have updated some more functions you can choose from. What about now?"


async def run_multi_turn_task(
    task,
    client,           # AsyncOpenAI client
    model_name: str,
    step_cb=None,     # optional fn(step_type: str, content) for UI
    use_tool_calls: bool = True,
) -> tuple[list[list[str]], list[str], int, int]:
    """
    Run a multi-turn BFCL task turn-by-turn, feeding execution results back.

    When use_tool_calls=True (default): uses OpenAI tools= API — no plaintext parsing.
    When use_tool_calls=False: legacy plaintext [func(params)] mode.

    Returns:
        per_turn_model_calls: list of function call strings per turn
        all_outputs: list of raw model outputs per turn
        total_input_tokens: accumulated prompt tokens across all turns
        total_output_tokens: accumulated completion tokens across all turns
    """
    from framework.benchmarks.bfcl._shared.tool_convert import (
        bfcl_docs_to_openai_tools, tool_calls_to_call_strings,
    )

    involved_classes: list[str] = task.metadata.get("involved_classes", [])
    initial_config: dict = task.metadata.get("initial_config", {})
    question_turns: list = task.metadata.get("question_turns", [])
    missed_function: dict = task.metadata.get("missed_function", {})
    long_context: bool = task.metadata.get("long_context", False)
    func_docs: list[dict] = task.metadata.get("functions", [])

    # Determine initially withheld functions
    withheld: set[str] = set()
    for names in missed_function.values():
        withheld.update(names)

    if use_tool_calls:
        # tool_calls mode: functions passed via tools= parameter, no system prompt injection
        messages: list[dict] = [{"role": "system", "content": (
            "You are an expert assistant. Use the provided tools to complete the user's requests. "
            "At each turn, call the necessary tools to fulfill the request."
        )}]
    else:
        available_funcs = [f for f in func_docs if f["name"] not in withheld]
        func_text = json.dumps(available_funcs, ensure_ascii=False, indent=2)
        system_prompt = MULTITURN_SYSTEM_PROMPT.format(functions=func_text)
        messages = [{"role": "system", "content": system_prompt}]

    instance_store: dict[str, Any] = {}
    per_turn_model_calls: list[list[str]] = []
    all_outputs: list[str] = []
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    for turn_idx, turn in enumerate(question_turns):
        # Reveal functions that become available at this turn (miss_func category)
        revealed_names = missed_function.get(str(turn_idx), [])
        if revealed_names:
            for fn in revealed_names:
                withheld.discard(fn)
            if not use_tool_calls:
                # Plaintext mode: send reveal as user message
                revealed_docs = [f for f in func_docs if f["name"] in revealed_names]
                reveal_text = json.dumps(revealed_docs, ensure_ascii=False, indent=2)
                reveal_msg = ADDITIONAL_FUNCTION_PROMPT.format(functions=reveal_text)
                messages.append({"role": "user", "content": reveal_msg})

        if not turn:
            if revealed_names:
                # miss_func: function was just revealed — model must retry previous request
                # Send a trigger message so the model knows to use the new tools
                trigger = "New tools are now available. Please retry your previous request using the available tools."
                if not use_tool_calls:
                    # plaintext: reveal message already added above; still need model call
                    pass
                else:
                    messages.append({"role": "user", "content": trigger})
                # Fall through to model call below
            else:
                per_turn_model_calls.append([])
                all_outputs.append("")
                continue
            user_content = ""  # empty turn — no new user content beyond the trigger
        else:
            user_content = turn[-1]["content"] if turn else ""
            messages.append({"role": "user", "content": user_content})

        if step_cb:
            step_cb("input", list(messages))

        # Build tools list for this turn (excluding withheld)
        if use_tool_calls:
            available_docs = [f for f in func_docs if f["name"] not in withheld]
            tools_payload = bfcl_docs_to_openai_tools(available_docs)
            kwargs: dict = dict(
                model=model_name,
                messages=messages,
                temperature=0.0,
                max_tokens=512,
                tools=tools_payload,
                tool_choice="auto",
            )
        else:
            kwargs = dict(
                model=model_name,
                messages=messages,
                temperature=0.0,
                max_tokens=512,
            )

        resp = await client.chat.completions.create(**kwargs)
        total_input_tokens += resp.usage.prompt_tokens if resp.usage else 0
        total_output_tokens += resp.usage.completion_tokens if resp.usage else 0
        msg = resp.choices[0].message

        if use_tool_calls and msg.tool_calls:
            calls = tool_calls_to_call_strings(msg.tool_calls)
            model_output = "[" + ", ".join(
                f"{tc.function.name}({tc.function.arguments.strip('{}').replace('\"', repr(None)[1:3])})"
                for tc in msg.tool_calls
            ) + "]"
            # Store as serialisable text for UI
            model_output = json.dumps([{tc.function.name: json.loads(tc.function.arguments)} for tc in msg.tool_calls])
        elif use_tool_calls:
            calls = []
            model_output = msg.content or ""
        else:
            model_output = (msg.content or "").strip()
            calls = parse_func_calls(model_output)

        all_outputs.append(model_output)
        per_turn_model_calls.append(calls)

        if step_cb:
            step_cb("output", model_output)

        # Execute calls against backend and inject results for next turn
        if calls and involved_classes:
            model_instances = _load_instances(
                involved_classes, initial_config, instance_store,
                suffix=f"_run_{task.id}", long_context=long_context,
            )
            method_map = _build_method_map(model_instances)
            results = execute_func_calls(calls, method_map)
            result_text = "\n".join(results)

            if use_tool_calls:
                # Append assistant message with tool_calls, then tool results
                messages.append(msg.model_dump(exclude_unset=False))
                for tc, res in zip(msg.tool_calls, results):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": res,
                    })
            else:
                messages.append({"role": "assistant", "content": model_output})
                messages.append({"role": "user", "content": f"[Execution results]: {result_text}"})

            if step_cb:
                step_cb("tool_result", result_text)
        else:
            if use_tool_calls:
                messages.append({"role": "assistant", "content": msg.content or ""})
            else:
                messages.append({"role": "assistant", "content": model_output})

    return per_turn_model_calls, all_outputs, total_input_tokens, total_output_tokens
