import re
from typing import Optional
from unidiff import PatchSet


def get_modified_files(patch: str) -> list[str]:
    source_files = []
    for file in PatchSet(patch):
        if file.source_file != "/dev/null":
            source_files.append(file.source_file)
    return [x[2:] for x in source_files if x.startswith("a/")]


def get_new_files(patch: str) -> list[str]:
    new_files = []
    for file in PatchSet(patch):
        if file.source_file == "/dev/null":
            target = file.target_file
            if target.startswith("b/"):
                target = target[2:]
            new_files.append(target)
    return new_files


def ansi_escape(text: str) -> str:
    return re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])").sub("", text)


def load_cached_environment_yml(instance_id: str) -> Optional[str]:
    return None
