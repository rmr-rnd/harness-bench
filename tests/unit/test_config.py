"""Tests for config.py: _expand_env and DatabaseConfig.build_url."""
from __future__ import annotations

import pytest

from framework.config import (
    DatabaseConfig, _expand_env, database_from_env, resolve_database,
)


# ---------------------------------------------------------------------------
# _expand_env
# ---------------------------------------------------------------------------

def test_expand_simple(monkeypatch):
    monkeypatch.setenv("MY_VAR", "hello")
    assert _expand_env("${MY_VAR}") == "hello"


def test_expand_default_used():
    # MISSING_XYZ should not exist in any real env
    assert _expand_env("${MISSING_XYZ_123:-fallback}") == "fallback"


def test_expand_default_empty():
    assert _expand_env("${MISSING_XYZ_123:-}") == ""


def test_expand_no_default_missing():
    # When var is absent and no default: leave original text unchanged
    assert _expand_env("${MISSING_XYZ_123}") == "${MISSING_XYZ_123}"


def test_expand_dollar_no_braces(monkeypatch):
    monkeypatch.setenv("MY_VAR", "world")
    assert _expand_env("$MY_VAR") == "world"


def test_expand_env_overrides_default(monkeypatch):
    monkeypatch.setenv("MY_VAR", "real")
    assert _expand_env("${MY_VAR:-fallback}") == "real"


def test_expand_plain_string_unchanged():
    assert _expand_env("no_vars_here") == "no_vars_here"


# ---------------------------------------------------------------------------
# DatabaseConfig.build_url
# ---------------------------------------------------------------------------

def test_url_built_from_parts():
    cfg = DatabaseConfig(host="db", port=5432, user="u", password="p", name="mydb")
    assert cfg.url == "postgresql://u:p@db:5432/mydb"


def test_explicit_url_not_overwritten():
    explicit = "postgresql://custom:pass@myhost:9999/custom_db"
    cfg = DatabaseConfig(url=explicit)
    assert cfg.url == explicit


# ---------------------------------------------------------------------------
# database_from_env / resolve_database (P4: deployment-level DB)
# ---------------------------------------------------------------------------

def test_database_from_env_none_without_db_host(monkeypatch):
    monkeypatch.delenv("DB_HOST", raising=False)
    assert database_from_env() is None


def test_database_from_env_builds_from_env(monkeypatch):
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.delenv("DB_PORT", raising=False)
    db = database_from_env()
    assert db is not None
    assert db.host == "postgres" and db.port == 5432
    assert db.user == "harness" and db.name == "harness_results"
    assert db.url == "postgresql://harness:secret@postgres:5432/harness_results"


def test_resolve_database_env_overrides_config(monkeypatch):
    monkeypatch.setenv("DB_HOST", "postgres")
    monkeypatch.setenv("DB_PASSWORD", "x")
    cfg_db = DatabaseConfig(host="localhost", name="other")
    assert resolve_database(cfg_db).host == "postgres"   # env wins


def test_resolve_database_falls_back_to_config(monkeypatch):
    monkeypatch.delenv("DB_HOST", raising=False)
    cfg_db = DatabaseConfig(host="localhost")
    assert resolve_database(cfg_db) is cfg_db
    assert resolve_database(None) is None
