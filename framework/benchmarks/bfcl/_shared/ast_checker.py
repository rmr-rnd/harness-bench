"""
Simplified AST checker for BFCL.

Ported from:
  bfcl_eval/eval_checker/ast_eval/ast_checker.py

We parse the model's Python-style function call output and compare it
against the ground truth (possible_answer list). A prediction is correct
if it matches ANY of the possible_answer entries (all parameters match
after type coercion).
"""
from __future__ import annotations

import ast
import re
from typing import Any


def _parse_python_call(text: str) -> list[dict[str, Any]]:
    """Parse '[func(a=1, b="x"), ...]' → [{'func': {'a': 1, 'b': 'x'}}, ...]"""
    text = text.strip()
    # Remove wrapping list brackets if present
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
    else:
        inner = text

    results = []
    # Split on top-level commas between calls
    # Use ast to safely evaluate
    try:
        # Wrap in a list so ast can parse multiple calls
        tree = ast.parse(f"[{inner}]", mode="eval")
    except SyntaxError:
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            for elt in node.elts:
                if isinstance(elt, ast.Call):
                    func_name = _get_name(elt.func)
                    kwargs = {}
                    for kw in elt.keywords:
                        kwargs[kw.arg] = _eval_node(kw.value)
                    # Positional args mapped by order (less common in BFCL)
                    results.append({func_name: kwargs})
            break
    return results


def _get_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_get_name(node.value)}.{node.attr}"
    return ""


def _eval_node(node) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        if isinstance(node, ast.Name):
            return node.id
        return None


def _standardize_string(s: str) -> str:
    """Remove [ ,./\\-_*^], lowercase, normalize quotes — matches BFCL original."""
    return re.sub(r"[ \,\.\/\-\_\*\^]", "", s).lower().replace("'", '"')


def _values_match(predicted: Any, expected: Any) -> bool:
    """Value comparison matching BFCL original: string normalization, bool/int distinction."""
    if isinstance(expected, list):
        if not expected:
            return predicted is None or predicted == "" or predicted == []
        return any(_values_match(predicted, e) for e in expected)
    # bool is subclass of int — treat as distinct types
    if isinstance(predicted, bool) != isinstance(expected, bool):
        return False
    if isinstance(predicted, str) and isinstance(expected, str):
        return _standardize_string(predicted) == _standardize_string(expected)
    if isinstance(predicted, (int, float)) and isinstance(expected, (int, float)) and not isinstance(predicted, bool):
        # Python: int may match float expected value (coerce int→float)
        return abs(float(predicted) - float(expected)) < 1e-6
    return predicted == expected


def _is_optional(expected_vals) -> bool:
    """GT param is optional if "" is one of the acceptable values."""
    if isinstance(expected_vals, list):
        return any(v == "" or v == [] or v is None for v in expected_vals)
    return expected_vals == ""


def _call_matches(predicted_call: dict, gt_call: dict) -> bool:
    """Check if predicted function call matches one ground truth call."""
    if set(predicted_call.keys()) != set(gt_call.keys()):
        return False
    for func_name, gt_params in gt_call.items():
        pred_params = predicted_call.get(func_name, {})
        if not isinstance(gt_params, dict) or not isinstance(pred_params, dict):
            return False
        for param, expected_vals in gt_params.items():
            if param not in pred_params:
                # Optional param (GT accepts "" as valid) — model may omit it
                if _is_optional(expected_vals):
                    continue
                return False
            if not _values_match(pred_params[param], expected_vals):
                return False
    return True


def check(model_output: str, possible_answer: list[list[dict]]) -> tuple[bool, str]:
    """
    Check model_output against possible_answer.

    possible_answer: list of acceptable answers, each answer is a list of calls.
    Returns (correct: bool, explanation: str).
    """
    predicted_calls = _parse_python_call(model_output)
    if not predicted_calls:
        return False, f"Could not parse model output: {model_output[:100]!r}"

    for acceptable in possible_answer:
        if len(predicted_calls) != len(acceptable):
            continue
        if all(_call_matches(p, g) for p, g in zip(predicted_calls, acceptable)):
            return True, "Matches ground truth"

    return False, f"No match. Predicted: {predicted_calls}"
