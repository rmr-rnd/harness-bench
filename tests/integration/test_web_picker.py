"""P4: config-picker endpoints — preset switching must never change the
deployment database, and must reject path traversal."""
from __future__ import annotations

import json

import pytest

import framework.ui.web as web
from framework.config import DatabaseConfig


@pytest.mark.asyncio
async def test_load_config_preserves_deployment_db():
    web._deployment_db = DatabaseConfig(host="postgres", name="harness_results", user="harness")
    web._current_config_path = ""

    resp = await web.load_config({"name": "example.yaml"})
    raw = json.loads(resp.body.decode())

    assert web._current_config_path.endswith("example.yaml")
    # Deployment DB wins — never the file's database section.
    assert web._current_cfg.database.host == "postgres"
    # Raw yaml returned for the form (placeholders intact — symmetric with get_config).
    assert raw["model"]["api_key"] == "${OPENAI_API_KEY}"


@pytest.mark.asyncio
async def test_load_config_new_defaults_keeps_db():
    web._deployment_db = DatabaseConfig(host="postgres")
    resp = await web.load_config({"name": ""})
    assert resp.status_code == 200
    assert web._current_config_path == ""
    assert web._current_cfg.database.host == "postgres"   # deployment kept on reset


@pytest.mark.asyncio
@pytest.mark.parametrize("name,status", [
    ("../secrets.yaml", 400),
    ("sub/cfg.yaml", 400),
    ("notyaml.txt", 400),
    ("does_not_exist.yaml", 404),
])
async def test_load_config_rejects_bad_names(name, status):
    web._deployment_db = None
    resp = await web.load_config({"name": name})
    assert resp.status_code == status


@pytest.mark.asyncio
async def test_harness_discovery_includes_omp_variants():
    resp = await web.get_harnesses()
    names = set(json.loads(resp.body.decode()))
    assert {"omp", "pac1_omp"}.issubset(names)
