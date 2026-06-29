import os
import tempfile
from pathlib import Path


def make_work_dir(prefix: str = "harness-") -> Path:
    base = os.environ.get("HARNESS_WORK_DIR", "")
    if base:
        Path(base).mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    return Path(tempfile.mkdtemp(prefix=prefix))


def make_work_file(prefix: str = "harness-", suffix: str = "") -> Path:
    base = os.environ.get("HARNESS_WORK_DIR", "")
    if base:
        Path(base).mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=base)
        os.close(fd)
        return Path(path)
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    return Path(path)
