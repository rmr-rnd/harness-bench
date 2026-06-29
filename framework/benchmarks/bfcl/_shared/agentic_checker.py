"""Agentic checker — copied verbatim from BFCL original eval_checker/agentic_eval/agentic_checker.py."""
import re


def agentic_checker(model_response: str, possible_answer_list: list[str]) -> dict:
    """
    Check if one of the possible answers is contained in the model response,
    ignoring case, whitespace and ",./-_*^" punctuation.
    """
    standardized_possible_answer_list = [
        standardize_string(possible_answer) for possible_answer in possible_answer_list
    ]
    if type(model_response) is list:
        model_response = model_response[0]
    if type(model_response) is not str:
        model_response = str(model_response)

    standardized_model_response = standardize_string(model_response)

    for possible_answer in standardized_possible_answer_list:
        if re.search(rf"\b{re.escape(possible_answer)}\b", standardized_model_response):
            return {"valid": True, "error": []}

    return {
        "valid": False,
        "error_message": "None of the expected answers were found in the model response.",
        "error_type": "agentic:answer_not_found",
        "details": {
            "model_response": model_response,
            "possible_answers": possible_answer_list,
            "standardized_model_response": standardized_model_response,
            "standardized_possible_answers": standardized_possible_answer_list,
        },
    }


def standardize_string(input_string: str) -> str:
    """Remove punctuation ,./-_*^(), lowercase, single→double quotes."""
    regex_string = r"[\,\.\/\-\_\*\^\(\)]"
    return re.sub(regex_string, "", input_string).lower().replace("'", '"')
