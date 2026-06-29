"""Browser-based web UI for Harness Testing Framework."""
from __future__ import annotations

import asyncio
import json
import socket
import webbrowser
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from framework.config import BenchmarkConfig, RunConfig

# ──────────────────────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────────────────────

_current_cfg: RunConfig = RunConfig()
_current_config_path: str = ""
_run_task: asyncio.Task | None = None
_current_orchestrator = None
_ws_clients: set[WebSocket] = set()
_db = None  # Database instance, set in start_web_ui
_deployment_db = None  # DatabaseConfig fixed at startup (env-first); config picker never changes it

# Report generation state: task_id -> html_bytes
_report_results: dict[str, bytes] = {}

# Analysis model settings (in-memory, loaded from config on start)
_analysis_model_cfg = None  # AnalysisModelConfig instance


# ──────────────────────────────────────────────────────────────────────────────
# HTML (embedded SPA)
# ──────────────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Harness Testing Framework</title>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --primary: #58a6ff;
    --text: #c9d1d9;
    --muted: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d29922;
    --radius: 6px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 13px;
    line-height: 1.5;
    min-height: 100vh;
  }
  a { color: var(--primary); }
  h2 { color: var(--primary); font-size: 14px; letter-spacing: .05em; margin-bottom: 12px; text-transform: uppercase; }
  h3 { color: var(--muted); font-size: 11px; letter-spacing: .08em; text-transform: uppercase; margin: 20px 0 8px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }

  /* Layout */
  #app { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
  .header {
    display: flex; align-items: center; gap: 12px;
    border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px;
  }
  .header h1 { font-size: 16px; color: var(--primary); }
  .header .badge {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 2px 10px; font-size: 11px; color: var(--muted);
  }

  /* Cards */
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 20px; margin-bottom: 16px;
  }

  /* Form */
  .form-grid { display: grid; grid-template-columns: 180px 1fr; gap: 8px 12px; align-items: center; }
  .form-grid label { color: var(--muted); text-align: right; padding-right: 8px; }
  input[type=text], input[type=password], input[type=number] {
    background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
    color: var(--text); padding: 6px 10px; font-family: inherit; font-size: 13px;
    width: 100%; outline: none; transition: border-color .15s;
  }
  input:focus { border-color: var(--primary); }
  .checkbox-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 8px; }
  .checkbox-item {
    display: flex; align-items: center; gap: 8px; cursor: pointer;
    background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 8px 10px; transition: border-color .15s;
  }
  .checkbox-item:hover { border-color: var(--primary); }
  .checkbox-item input[type=checkbox] { accent-color: var(--primary); width: 14px; height: 14px; }
  .toggle-row { display: flex; align-items: center; gap: 10px; }
  .toggle { position: relative; display: inline-block; width: 40px; height: 22px; }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute; inset: 0; background: var(--border);
    border-radius: 22px; cursor: pointer; transition: .2s;
  }
  .slider:before {
    content: ''; position: absolute; height: 16px; width: 16px;
    left: 3px; bottom: 3px; background: #fff; border-radius: 50%; transition: .2s;
  }
  input:checked + .slider { background: var(--primary); }
  input:checked + .slider:before { transform: translateX(18px); }

  /* Buttons */
  .btn {
    border: none; border-radius: var(--radius); cursor: pointer;
    font-family: inherit; font-size: 13px; font-weight: 600;
    padding: 8px 18px; transition: opacity .15s;
  }
  .btn:hover { opacity: .85; }
  .btn-primary { background: var(--primary); color: #0d1117; }
  .btn-success { background: var(--green); color: #0d1117; }
  .btn-danger  { background: var(--red); color: #fff; }
  .btn-ghost   { background: transparent; color: var(--muted); border: 1px solid var(--border); }
  .btn-row { display: flex; gap: 10px; margin-top: 20px; flex-wrap: wrap; align-items: center; }
  .error-msg { color: var(--red); font-size: 12px; }

  /* Run view */
  .run-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 800px) { .run-layout { grid-template-columns: 1fr; } }

  .meta-bar {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 12px 16px; margin-bottom: 16px;
    display: flex; flex-wrap: wrap; gap: 16px; font-size: 12px; color: var(--muted);
  }
  .meta-bar strong { color: var(--text); }

  .bench-row {
    background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 10px 12px; margin-bottom: 8px;
  }
  .bench-row-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .bench-name { color: var(--primary); font-weight: 600; }
  .bench-stats { color: var(--muted); font-size: 12px; }
  .bench-stat-correct { color: var(--green); }
  .bench-stat-incorrect { color: var(--red); }
  .progress-track {
    height: 6px; background: var(--border); border-radius: 3px; overflow: hidden;
  }
  .progress-fill {
    height: 100%; background: var(--primary); border-radius: 3px;
    transition: width .3s ease; width: 0%;
  }
  .progress-fill.done { background: var(--green); }

  /* Log */
  #log-container {
    background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
    height: 420px; overflow-y: auto; padding: 10px;
  }
  .log-entry { padding: 2px 0; font-size: 12px; }
  .log-bench { color: var(--primary); font-weight: 600; }
  .log-correct { color: var(--green); }
  .log-incorrect { color: var(--red); }
  .log-partial { color: var(--yellow); }
  .log-error { color: var(--red); font-weight: 600; }
  .log-skip, .log-start { color: var(--muted); }

  /* Summary cards */
  .acc-high { color: var(--green); }
  .acc-mid  { color: var(--yellow); }
  .acc-low  { color: var(--red); }
  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 12px;
  }
  .summary-card {
    background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 14px 16px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .summary-card-name {
    font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .summary-card-acc {
    font-size: 32px; font-weight: 700; line-height: 1;
  }
  .summary-card-bar {
    height: 4px; background: var(--border); border-radius: 2px; overflow: hidden;
  }
  .summary-card-fill {
    height: 100%; border-radius: 2px; transition: width .4s ease;
  }
  .summary-card-fill.acc-high { background: var(--green); }
  .summary-card-fill.acc-mid  { background: var(--yellow); }
  .summary-card-fill.acc-low  { background: var(--red); }
  .summary-card-stats {
    display: flex; gap: 12px; font-size: 11px; color: var(--muted);
  }
  .summary-card-stats .s-correct   { color: var(--green); }
  .summary-card-stats .s-incorrect { color: var(--red); }
  .summary-card-tok {
    font-size: 10px; color: var(--muted);
  }

  /* Status pill */
  .status-pill {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 3px 12px; font-size: 12px; color: var(--muted);
  }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
  .dot.running { background: var(--primary); animation: pulse 1s infinite; }
  .dot.done { background: var(--green); }
  .dot.error { background: var(--red); }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.4; } }

  /* Per-task live panels */
  .task-panel {
    background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
    overflow: hidden;
  }
  .task-panel-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 7px 10px; background: var(--surface); border-bottom: 1px solid var(--border);
    cursor: pointer; user-select: none;
  }
  .task-panel-header:hover { background: #1e2530; }
  .task-panel-toggle { color: var(--muted); font-size: 11px; }
  .task-panel-id { font-size: 11px; color: var(--primary); font-weight: 600; word-break: break-all; }
  .task-panel-badge {
    font-size: 10px; padding: 2px 7px; border-radius: 10px; font-weight: 600; white-space: nowrap;
  }
  .task-panel-badge.running  { background: #1c3a5c; color: var(--primary); animation: pulse 1s infinite; }
  .task-panel-badge.done     { background: #1a3828; color: var(--green); }
  .task-panel-badge.failed   { background: #3a1a1a; color: var(--red); }
  .task-panel-badge.partial  { background: #3a2f10; color: var(--yellow); }
  .task-panel-badge.timeout  { background: #2e1f00; color: #e3b341; }
  .task-panel-log {
    padding: 8px; max-height: 500px; overflow-y: auto; font-size: 11px; line-height: 1.5;
  }
  .task-panel-log > * + * { margin-top: 4px; }
  .task-panel-log.collapsed { display: none; }

  /* Benchmark group in active tasks */
  .bench-group { margin-bottom: 16px; }
  .bench-group-header {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 3px solid var(--primary);
    border-radius: var(--radius);
    cursor: pointer; user-select: none; margin-bottom: 8px;
  }
  .bench-group-header:hover { background: #1e2530; border-left-color: #79c0ff; }
  .bench-group-name { color: var(--primary); font-size: 13px; font-weight: 700; letter-spacing: .04em; flex: 1; text-transform: uppercase; }
  .bench-group-meta { display: flex; align-items: center; gap: 10px; }
  .bench-group-count { color: var(--muted); font-size: 11px; }
  .bench-group-running { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--primary); }
  .bench-group-running .dot { width: 6px; height: 6px; }
  .bench-group-toggle { color: var(--muted); font-size: 12px; }
  .bench-group-tasks { display: flex; flex-direction: column; gap: 6px; padding-left: 4px; }
  .bench-group-tasks.collapsed { display: none; }

  /* Step blocks */
  .step-block { border-radius: 4px; overflow: hidden; border: 1px solid var(--border); }
  .step-block-header {
    display: flex; align-items: center; gap: 8px; padding: 4px 8px;
    cursor: pointer; user-select: none; border-left: 3px solid transparent;
  }
  .step-block-header:hover { filter: brightness(1.1); }
  .step-block-label { font-size: 10px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; flex: 1; }
  .step-block-toggle { color: var(--muted); font-size: 10px; }
  .step-block-preview { color: var(--muted); font-size: 10px; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .step-block-body {
    padding: 8px 12px; background: #0d1117; font-size: 11px; line-height: 1.6;
    white-space: pre-wrap; overflow-wrap: break-word; word-break: normal;
    max-height: 400px; overflow-y: auto; border-top: 1px solid var(--border);
  }
  .step-block-body.collapsed { display: none; }

  /* Category breakdown table inside bench-row */
  .cat-table { width: 100%; margin-top: 8px; font-size: 11px; border-collapse: collapse; }
  .cat-table td { padding: 3px 8px; border-bottom: 1px solid #21262d; }
  .cat-table td:first-child { color: var(--muted); width: 40%; }
  .cat-table .cat-acc-high { color: var(--green); }
  .cat-table .cat-acc-mid  { color: var(--yellow); }
  .cat-table .cat-acc-low  { color: var(--red); }
  .cat-expand-btn { font-size: 10px; color: var(--muted); cursor: pointer; background: none; border: none; padding: 2px 6px; font-family: inherit; }
  .cat-expand-btn:hover { color: var(--primary); }

  /* Runs history */
  .run-hist-item {
    display: flex; align-items: center; gap: 12px;
    padding: 8px 12px; background: var(--bg); border: 1px solid var(--border);
    border-radius: var(--radius); margin-bottom: 6px;
  }
  .run-hist-id { color: var(--primary); font-size: 12px; flex: 1; }
  .run-hist-meta { color: var(--muted); font-size: 11px; }
  .run-hist-acc { font-size: 12px; font-weight: 600; }
  .run-hist-del { background: none; border: none; color: var(--red); cursor: pointer; font-size: 12px; padding: 2px 6px; border-radius: 4px; }
  .run-hist-del:hover { background: #3a1a1a; }
  .run-hist-cb { width: 15px; height: 15px; cursor: pointer; accent-color: var(--primary); flex-shrink: 0; }
  .run-hist-item.selected { border-color: var(--primary); background: #0d1f3c; }
  #report-bar { display:none; align-items:center; gap:12px; padding:10px 14px;
    background:#0d1f3c; border:1px solid var(--primary); border-radius:var(--radius);
    margin-bottom:12px; }
  #report-bar.visible { display:flex; }
  #report-sel-count { color:var(--primary); font-size:12px; }
  .btn-report { background:var(--primary); color:#fff; border:none; border-radius:4px;
    padding:6px 14px; font-size:12px; cursor:pointer; font-family:inherit; }
  .btn-report:hover { background:#1f6feb; }
  .btn-report-ai { background:#1a4a6e; color:#fff; border:none; border-radius:4px;
    padding:6px 14px; font-size:12px; cursor:pointer; font-family:inherit; }
  .btn-report-ai:hover { background:#215d8a; }
  .btn-report-clear { background:none; border:1px solid var(--border); color:var(--muted);
    border-radius:4px; padding:6px 10px; font-size:12px; cursor:pointer; font-family:inherit; }
  .btn-report-clear:hover { color:var(--text); border-color:var(--muted); }

  /* Colors per step type */
  .step-block.type-input    .step-block-header { background: #161b22; border-left-color: #444c56; }
  .step-block.type-input    .step-block-label  { color: #8b949e; }
  .step-block.type-thinking .step-block-header { background: #0d1f3c; border-left-color: #388bfd; }
  .step-block.type-thinking .step-block-label  { color: #388bfd; }
  .step-block.type-tool_call  .step-block-header { background: #1a1040; border-left-color: #8957e5; }
  .step-block.type-tool_call  .step-block-label  { color: #8957e5; }
  .step-block.type-tool_result .step-block-header { background: #0d2818; border-left-color: #3fb950; }
  .step-block.type-tool_result .step-block-label  { color: #3fb950; }
  .step-block.type-output   .step-block-header { background: #0a2018; border-left-color: #2ea043; }
  .step-block.type-output   .step-block-label  { color: #2ea043; }
  .step-block.type-judge    .step-block-header { background: #251e00; border-left-color: #d4a017; }
  .step-block.type-judge    .step-block-label  { color: #d4a017; }
  .step-block.type-expected .step-block-header { background: #161b22; border-left-color: #6e7681; }
  .step-block.type-expected .step-block-label  { color: #6e7681; }
  .step-block.type-error    .step-block-header { background: #2d0f0f; border-left-color: #f85149; }
  .step-block.type-error    .step-block-label  { color: #f85149; }
  /* Runs list */
  .run-item {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 12px; background: var(--bg); border: 1px solid var(--border);
    border-radius: var(--radius); margin-bottom: 8px;
  }
  .run-item-id { color: var(--primary); }
  .run-item-meta { color: var(--muted); font-size: 12px; }

  .hidden { display: none !important; }
</style>
</head>
<body>
<div id="app">
  <div class="header">
    <h1>Harness Testing Framework</h1>
  </div>

  <!-- ═══════════════════════════ SETUP VIEW ═══════════════════════════ -->
  <div id="setup-view">
    <div class="card">
      <h2>Config</h2>
      <div class="form-grid">
        <label>preset</label>
        <select id="config-select" onchange="onConfigPick()"></select>
      </div>
    </div>
    <div class="card">
      <h2>Benchmarks</h2>
      <div class="checkbox-grid" id="bench-checks"></div>
    </div>

    <div class="card">
      <h2>Harness</h2>
      <div class="form-grid">
        <label>type</label>
        <div id="harness-type-radios" style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
          <label style="display:flex;gap:6px;align-items:center;cursor:pointer">
            <input type="radio" name="harness_type" value="hermes" id="harness_hermes" checked onchange="onHarnessChange()">
            hermes
          </label>
        </div>
      </div>
      <div id="hermes-opts" style="display:none;margin-top:12px">
        <div class="form-grid">
          <label>docker image</label>
          <input type="text" id="hermes_image" value="nousresearch/hermes-agent:latest" autocomplete="off" style="font-family:monospace">
          <label>api key</label>
          <input type="text" id="hermes_api_key" placeholder="auto-generated" autocomplete="off" style="font-family:monospace">
          <label>yolo mode</label>
          <div class="toggle-row">
            <label class="toggle"><input type="checkbox" id="hermes_approvals_off" checked><span class="slider"></span></label>
          </div>
        </div>
      </div>
      <div id="opencode-opts" style="display:none;margin-top:12px">
        <div class="form-grid">
          <label>docker image</label>
          <input type="text" id="opencode_image" value="ghcr.io/anomalyco/opencode:latest" autocomplete="off" style="font-family:monospace">
        </div>
      </div>
      <div id="openclaw-opts" style="display:none;margin-top:12px">
        <div class="form-grid">
          <label>docker image</label>
          <input type="text" id="openclaw_image" value="ghcr.io/openclaw/openclaw:latest" autocomplete="off" style="font-family:monospace">
          <label>gateway token</label>
          <input type="text" id="openclaw_token" placeholder="auto-generated" autocomplete="off" style="font-family:monospace">
          <label>yolo mode</label>
          <div class="toggle-row">
            <label class="toggle"><input type="checkbox" id="openclaw_approvals_off" checked><span class="slider"></span></label>
          </div>
        </div>
      </div>

      <div class="form-grid" style="margin-top:12px">
        <label>web search</label>
        <div class="toggle-row">
          <label class="toggle"><input type="checkbox" id="web_search" onchange="onWebSearchChange()"><span class="slider"></span></label>
        </div>
        <label id="tavily-label" style="display:none">tavily_api_key</label>
        <input type="text" id="tavily_key" placeholder="tvly-..." autocomplete="off" style="font-family:monospace;display:none">
      </div>

      <hr style="margin:16px 0;border:none;border-top:1px solid var(--border)">
      <h3 style="margin:0 0 12px;font-size:14px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em">Model</h3>
      <div class="form-grid">
        <label>base_url</label>
        <input type="text" id="model_url" placeholder="https://api.openai.com/v1" autocomplete="off">
        <label>api_key</label>
        <input type="text" id="model_key" placeholder="sk-..." autocomplete="off" style="font-family:monospace">
        <label>model_name</label>
        <input type="text" id="model_name" placeholder="gpt-4o" autocomplete="off">
        <label>temperature</label>
        <input type="number" id="temperature" step="0.1" min="0" max="2" autocomplete="off">
        <label>max_tokens</label>
        <input type="number" id="max_tokens" min="1" autocomplete="off">
        <label>reasoning_effort</label>
        <select id="reasoning_effort">
          <option value="">— default —</option>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
          <option value="xhigh">xhigh</option>
        </select>
      </div>
    </div>

    <div class="card">
      <h2>Judge Model</h2>
      <div class="form-grid">
        <label>base_url</label>
        <input type="text" id="judge_url" placeholder="https://api.openai.com/v1" autocomplete="off">
        <label>api_key</label>
        <input type="text" id="judge_key" placeholder="sk-..." autocomplete="off" style="font-family:monospace">
        <label>model_name</label>
        <input type="text" id="judge_name" placeholder="gpt-4o-mini" autocomplete="off">
      </div>
    </div>

    <div class="card" id="pac1-opts" style="display:none">
      <h2>PAC1</h2>
      <div class="form-grid">
        <label>bitgn_api_key</label>
        <input type="text" id="pac1_bitgn_api_key" value="" autocomplete="off" style="font-family:monospace">
        <label>bitgn_benchmark_host</label>
        <input type="text" id="pac1_bitgn_benchmark_host" value="https://api.bitgn.com" autocomplete="off" style="font-family:monospace">
        <label>bitgn_benchmark_id</label>
        <input type="text" id="pac1_bitgn_benchmark_id" value="bitgn/pac1-dev" autocomplete="off" style="font-family:monospace">
        <label>bitgn_run_name</label>
        <input type="text" id="pac1_bitgn_run_name" value="hermes-pac1" autocomplete="off" style="font-family:monospace">
      </div>
    </div>

    <div class="card">
      <h2>Run Options</h2>
      <div class="form-grid">
        <label>limit per benchmark</label>
        <input type="number" id="limit" placeholder="all (leave blank)">
        <label>workers</label>
        <input type="number" id="workers" min="1" value="1">
        <label>timeout per task (s)</label>
        <input type="number" id="timeout" min="1" value="180">
        <label>eval timeout (s)</label>
        <input type="number" id="eval_timeout" min="1" value="120">
        <label>stream idle timeout (s)</label>
        <input type="number" id="stream_idle_timeout" min="0" value="60">
      </div>
    </div>

    <div class="card">
      <h2>Save Config</h2>
      <div class="form-grid">
        <label>file path</label>
        <input type="text" id="save_path" placeholder="configs/my_run.yaml">
      </div>
      <div class="btn-row">
        <button class="btn btn-ghost" onclick="saveConfig()">Save YAML</button>
      </div>
    </div>

    <div class="btn-row">
      <button class="btn btn-success" onclick="startRun()">▶ RUN</button>
      <button class="btn btn-ghost" onclick="navigate('#/history')">History</button>
      <span class="error-msg" id="setup-error"></span>
    </div>
  </div>

  <!-- ═══════════════════════════ HISTORY VIEW ═══════════════════════════ -->
  <div id="history-view" class="hidden">
    <div id="history-list-wrap">
      <div class="card">
        <h2>Run History</h2>
        <div id="report-bar">
          <span id="report-sel-count">0 runs selected</span>
          <button class="btn-report" onclick="generateReport(false)">Report</button>
          <div style="display:inline-flex;align-items:center;gap:2px">
            <button class="btn-report-ai" onclick="generateReport(true)" title="Includes AI analysis per benchmark (~1 min)">Report + AI Analysis</button>
            <button onclick="openAnalysisModelSettings()" title="Настройки модели для AI анализа" style="background:#21262d;border:1px solid #30363d;border-radius:0 6px 6px 0;border-left:none;color:#8b949e;cursor:pointer;font-size:13px;padding:5px 8px;line-height:1" onmouseover="this.style.color='#e6edf3'" onmouseout="this.style.color='#8b949e'">⚙</button>
          </div>
          <button class="btn-report-clear" onclick="clearReportSelection()">Clear</button>
          <span id="report-progress" style="color:#8b949e;font-size:11px;margin-left:8px"></span>
        </div>
        <div id="history-list"><span style="color:var(--muted);font-size:12px">Loading…</span></div>
      </div>
    </div>
    <div id="run-detail" class="hidden"></div>
    <div class="btn-row" id="history-back-row">
      <button class="btn btn-ghost" onclick="navigate('#/')">← Back to Setup</button>
    </div>
  </div>

  <!-- ═══════════════════════════ RUN VIEW ═══════════════════════════ -->
  <div id="run-view" class="hidden">
    <div class="meta-bar" id="meta-bar">
      <span>Run: <strong id="meta-run-id">—</strong></span>
      <span>Model: <strong id="meta-model">—</strong></span>
      <span>Judge: <strong id="meta-judge">—</strong></span>
      <span>Harness: <strong id="meta-harness">—</strong></span>
      <span>Workers: <strong id="meta-workers">—</strong></span>
      <span>Timeout: <strong id="meta-timeout">—</strong>s</span>
      <span class="status-pill" id="status-pill"><span class="dot running" id="status-dot"></span><span id="status-text">Running…</span></span>
    </div>

    <!-- Activity / preparation log (Docker, DB, run-level events) -->
    <div class="card" style="padding:16px; margin-bottom:16px">
      <h2>Activity</h2>
      <div id="activity-log" style="font-family:var(--mono,monospace);font-size:12px;line-height:1.6;max-height:160px;overflow-y:auto"></div>
    </div>

    <!-- Benchmark progress -->
    <div class="card" style="padding:16px; margin-bottom:16px">
      <h2>Benchmarks</h2>
      <div id="bench-rows"></div>
    </div>

    <!-- Active task panels (live logs per parallel agent) -->
    <div class="card" style="padding:16px; margin-bottom:16px">
      <h2>Active Tasks</h2>
      <div id="active-tasks" style="display:flex; flex-direction:column; gap:10px"></div>
      <div id="active-tasks-empty" style="color:var(--muted); font-size:12px">No tasks running yet</div>
    </div>

    <!-- Summary -->
    <div class="card" style="padding:16px" id="summary-card">
      <h2>Summary</h2>
      <div class="summary-grid" id="summary-body"></div>
    </div>

    <div class="btn-row">
      <button class="btn btn-ghost" onclick="backToSetup()">← Back to Setup</button>
      <button class="btn btn-danger" onclick="stopRun()" id="btn-stop">Stop</button>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let ws = null;
let benchState = {}; // name -> {total, done, grades:{}, categories:{}}
let currentCfg = null;
let activeTasks = {}; // task_id -> {el, logEl, collapsed}
let benchGroups = {}; // benchmark -> {groupEl, tasksEl, count}

const ALL_BENCHMARKS = __BENCHMARKS_PLACEHOLDER__;
const GRADE_CLASS = {
  CORRECT:'correct', PASS:'correct',
  INCORRECT:'incorrect', FAIL:'incorrect', ERROR:'incorrect', AGENT_DEAD:'incorrect',
  PARTIAL:'partial', NOT_ATTEMPTED:'skip',
};
const STEP_LABELS = {
  input: 'Input', thinking: 'Thinking', tool_call: 'Tool Call',
  tool_result: 'Tool Result', output: 'Output', judge: 'Judge', expected: 'Expected', error: 'Error',
};

// ── Init ───────────────────────────────────────────────────────────────────
async function init() {
  // Build benchmark checkboxes
  const grid = document.getElementById('bench-checks');
  ALL_BENCHMARKS.forEach(name => {
    const item = document.createElement('label');
    item.className = 'checkbox-item';
    const onchange = name === 'pac1' ? ' onchange="onHarnessChange()"' : '';
    item.innerHTML = `<input type="checkbox" id="cb_${name}" value="${name}"${onchange}> ${name}`;
    grid.appendChild(item);
  });

  // Load available harness types and populate radio buttons dynamically
  try {
    const r = await fetch('/api/harnesses');
    window.AVAILABLE_HARNESSES = await r.json();
  } catch(e) { window.AVAILABLE_HARNESSES = []; }
  {
    // Static harnesses already in HTML; add any extras discovered at runtime
    const STATIC = ['hermes'];
    const container = document.getElementById('harness-type-radios');
    if (container) {
      for (const ht of window.AVAILABLE_HARNESSES) {
        if (STATIC.includes(ht)) continue;
        // Skip per-benchmark variants (e.g. pac1_hermes) — not selectable as base type
        if (ht.includes('_')) continue;
        const lbl = document.createElement('label');
        lbl.style.cssText = 'display:flex;gap:6px;align-items:center;cursor:pointer';
        lbl.innerHTML = `<input type="radio" name="harness_type" value="${ht}" id="harness_${ht}" onchange="onHarnessChange()"> ${ht}`;
        container.appendChild(lbl);
      }
    }
  }

  // Load config from server
  try {
    const r = await fetch('/api/config');
    currentCfg = await r.json();
    applyConfig(currentCfg);
  } catch(e) { console.warn('Could not load config', e); }

  loadConfigList();
  applyRoute();   // restore the view from the URL hash (so F5 / shared links work)
}

// ── Hash router ──────────────────────────────────────────────────────────────
// Views are addressable: #/ (setup), #/history, #/run/<id>. Click handlers only
// set the hash via navigate(); applyRoute() is the single place that renders a
// view, so there is no risk of recursion (render fns never touch the hash).
async function applyRoute() {
  const h = location.hash.replace(/^#[/]?/, '');     // "", "history", "run/<id>"
  if (h === 'history') return showHistory();
  if (h.startsWith('run/')) return openRunDetail(decodeURIComponent(h.slice(4)));
  return showSetup();
}
function navigate(hash) {
  if (location.hash === hash) applyRoute();          // same hash → re-apply (refreshes data)
  else location.hash = hash;                         // else → hashchange → applyRoute
}
window.addEventListener('hashchange', applyRoute);

// Populate the config-preset dropdown from configs/*.yaml.
async function loadConfigList() {
  const sel = document.getElementById('config-select');
  if (!sel) return;
  try {
    const r = await fetch('/api/configs');
    const data = await r.json();
    const opts = ['<option value="">New (defaults)</option>'];
    for (const name of (data.configs || [])) {
      const selAttr = name === data.current ? ' selected' : '';
      opts.push(`<option value="${escHtml(name)}"${selAttr}>${escHtml(name)}</option>`);
    }
    sel.innerHTML = opts.join('');
  } catch(e) { console.warn('Could not load config list', e); }
}

// Switch the active preset. Server reloads run-params (DB stays the deployment one).
async function onConfigPick() {
  const name = document.getElementById('config-select').value;
  try {
    const r = await fetch('/api/load-config', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name}),
    });
    if (!r.ok) { document.getElementById('setup-error').textContent = 'Could not load config'; return; }
    currentCfg = await r.json();
    applyConfig(currentCfg);
    document.getElementById('save_path').value = name ? ('configs/' + name) : '';
    document.getElementById('setup-error').textContent = '';
  } catch(e) { console.warn('Could not load config', e); }
}

function applyConfig(cfg) {
  if (!cfg) return;
  const selected = new Set((cfg.benchmarks || []).map(b => b.name));
  ALL_BENCHMARKS.forEach(name => {
    const cb = document.getElementById(`cb_${name}`);
    if (cb) cb.checked = selected.has(name);
  });
  if (cfg.model) {
    document.getElementById('model_url').value   = cfg.model.base_url || '';
    document.getElementById('model_key').value   = cfg.model.api_key || '';
    document.getElementById('model_name').value  = cfg.model.model_name || '';
    document.getElementById('temperature').value = cfg.model.temperature ?? 0;
    document.getElementById('max_tokens').value  = cfg.model.max_tokens || 4096;
    if (cfg.model.reasoning_effort) document.getElementById('reasoning_effort').value = cfg.model.reasoning_effort;
  }
  const judgeModel = cfg.judge_model || cfg.model;
  document.getElementById('judge_url').value  = judgeModel?.base_url || '';
  document.getElementById('judge_key').value  = judgeModel?.api_key || '';
  document.getElementById('judge_name').value = judgeModel?.model_name || '';

  if (cfg.search) {
    document.getElementById('tavily_key').value = cfg.search.tavily_api_key || '';
  }
  if (cfg.harness) {
    // pac1_hermes in yaml maps to hermes radio (pac1_hermes is auto-selected per task)
    const htype = cfg.harness.type || 'hermes';
    const effectiveType = htype.includes('_') ? htype.split('_').slice(1).join('_') : htype;
    const radio = document.getElementById(`harness_${effectiveType}`)
                  || document.getElementById(`harness_${htype}`)
                  || document.getElementById('harness_hermes');
    if (radio) { radio.checked = true; onHarnessChange(); }
    if (cfg.harness.hermes_image) document.getElementById('hermes_image').value = cfg.harness.hermes_image;
    if (cfg.harness.hermes_api_key) document.getElementById('hermes_api_key').value = cfg.harness.hermes_api_key;
    document.getElementById('hermes_approvals_off').checked = cfg.harness.hermes_approvals_off !== false;
    if (cfg.harness.opencode_image) document.getElementById('opencode_image').value = cfg.harness.opencode_image;
    if (cfg.harness.openclaw_image) document.getElementById('openclaw_image').value = cfg.harness.openclaw_image;
    if (cfg.harness.openclaw_token) document.getElementById('openclaw_token').value = cfg.harness.openclaw_token;
    document.getElementById('openclaw_approvals_off').checked = cfg.harness.openclaw_approvals_off !== false;
    // PAC1 fields (shown when pac1 benchmark is selected, regardless of harness type)
    if (htype === 'pac1_hermes' || htype === 'pac1_openclaw' || cfg.harness.bitgn_api_key) {
      if (cfg.harness.bitgn_api_key) document.getElementById('pac1_bitgn_api_key').value = cfg.harness.bitgn_api_key;
      if (cfg.harness.bitgn_benchmark_host) document.getElementById('pac1_bitgn_benchmark_host').value = cfg.harness.bitgn_benchmark_host;
      if (cfg.harness.bitgn_benchmark_id) document.getElementById('pac1_bitgn_benchmark_id').value = cfg.harness.bitgn_benchmark_id;
      if (cfg.harness.bitgn_run_name) document.getElementById('pac1_bitgn_run_name').value = cfg.harness.bitgn_run_name;
    }
  }
  const firstBench = cfg.benchmarks?.[0];
  const sqBench = cfg.benchmarks?.find(b => b.name === 'simpleqa');
  document.getElementById('web_search').checked = sqBench?.web_search || false;
  onWebSearchChange();
  if (firstBench?.limit) document.getElementById('limit').value = firstBench.limit;
  if (cfg.parallelism) {
    document.getElementById('workers').value = cfg.parallelism.workers || 1;
    document.getElementById('timeout').value = cfg.parallelism.timeout_per_task || 180;
    document.getElementById('eval_timeout').value = cfg.parallelism.eval_timeout || 120;
    document.getElementById('stream_idle_timeout').value =
      cfg.parallelism.stream_idle_timeout != null ? cfg.parallelism.stream_idle_timeout : 60;
  }
}

function readFields() {
  const selected = ALL_BENCHMARKS.filter(n => document.getElementById(`cb_${n}`)?.checked);
  if (!selected.length) return { error: 'Select at least one benchmark' };

  const gv = id => document.getElementById(id).value.trim();
  const limitStr = gv('limit');

  const cfg = {
    model: {
      base_url:    gv('model_url') || 'https://api.openai.com/v1',
      api_key:     gv('model_key'),
      model_name:  gv('model_name') || 'gpt-4o',
      temperature: parseFloat(gv('temperature')) || 0,
      max_tokens:  parseInt(gv('max_tokens')) || 4096,
      reasoning_effort: gv('reasoning_effort') || null,
    },
    judge_model: {
      base_url:    gv('judge_url') || gv('model_url') || 'https://api.openai.com/v1',
      api_key:     gv('judge_key') || gv('model_key'),
      model_name:  gv('judge_name') || gv('model_name'),
      temperature: 0,
      max_tokens:  4096,
    },
    search: { tavily_api_key: gv('tavily_key') },
    harness: {
      type: document.querySelector('input[name="harness_type"]:checked')?.value || 'hermes',
      hermes_image: gv('hermes_image') || 'nousresearch/hermes-agent:latest',
      hermes_api_key: gv('hermes_api_key') || '',
      hermes_approvals_off: document.getElementById('hermes_approvals_off')?.checked ?? true,
      opencode_image: gv('opencode_image') || 'ghcr.io/anomalyco/opencode:latest',
      openclaw_image: gv('openclaw_image') || 'ghcr.io/openclaw/openclaw:latest',
      openclaw_token: gv('openclaw_token') || '',
      openclaw_approvals_off: document.getElementById('openclaw_approvals_off')?.checked ?? true,
    },
    parallelism: {
      workers: parseInt(gv('workers')) || 1,
      timeout_per_task: parseInt(gv('timeout')) || 180,
      eval_timeout: parseInt(gv('eval_timeout')) || 120,
      // 0 is a valid value (disables the idle watchdog), so don't use `|| 60`.
      stream_idle_timeout: (() => { const v = parseInt(gv('stream_idle_timeout')); return isNaN(v) ? 60 : v; })(),
    },
    benchmarks: selected.map(name => ({
      name,
      limit: limitStr ? parseInt(limitStr) : null,
      web_search: name === 'simpleqa' && document.getElementById('web_search').checked,
    })),
  };

  // PAC1: add bitgn harness fields when pac1 benchmark is selected (any harness type)
  if (selected.includes('pac1')) {
    cfg.harness.bitgn_api_key        = gv('pac1_bitgn_api_key');
    cfg.harness.bitgn_benchmark_host = gv('pac1_bitgn_benchmark_host');
    cfg.harness.bitgn_benchmark_id   = gv('pac1_bitgn_benchmark_id');
    cfg.harness.bitgn_run_name       = gv('pac1_bitgn_run_name') || 'harness-bench';
  }

  // Auto-detect benchmark_harness by convention: if "{benchmark}_{harness_type}"
  // exists as a harness, use it automatically for that benchmark.
  const htype = cfg.harness.type;
  const available = window.AVAILABLE_HARNESSES || [];
  const overrides = {};
  selected.forEach(bname => {
    const variant = `${bname}_${htype}`;
    if (available.includes(variant)) overrides[bname] = variant;
  });
  if (Object.keys(overrides).length > 0) cfg.harness.benchmark_harness = overrides;

  return cfg;
}

async function saveConfig() {
  const cfg = readFields();
  if (cfg.error) { document.getElementById('setup-error').textContent = 'Error: ' + cfg.error; return; }
  const savePath = document.getElementById('save_path').value.trim() || 'configs/my_run.yaml';
  const r = await fetch('/api/save-config', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({config: cfg, path: savePath}),
  });
  const data = await r.json();
  document.getElementById('setup-error').textContent = data.ok ? `Saved to ${savePath}` : 'Error: ' + (data.error || 'Unknown');
  if (data.ok) document.getElementById('setup-error').style.color = 'var(--green)';
}

async function startRun() {
  const cfg = readFields();
  if (cfg.error) { document.getElementById('setup-error').textContent = 'Error: ' + cfg.error; return; }
  document.getElementById('setup-error').textContent = '';

  // Switch to run view
  document.getElementById('setup-view').classList.add('hidden');
  document.getElementById('run-view').classList.remove('hidden');

  // Populate meta
  const judgeModel = cfg.judge_model || cfg.model;
  document.getElementById('meta-model').textContent   = cfg.model.model_name;
  document.getElementById('meta-judge').textContent   = judgeModel.model_name;
  document.getElementById('meta-harness').textContent = cfg.harness?.type || '—';
  document.getElementById('meta-workers').textContent = cfg.parallelism.workers;
  document.getElementById('meta-timeout').textContent = cfg.parallelism.timeout_per_task;

  benchState = {};
  activeTasks = {};
  benchGroups = {};
  document.getElementById('bench-rows').innerHTML = '';
  document.getElementById('active-tasks').innerHTML = '';
  document.getElementById('active-tasks-empty').style.display = '';
  document.getElementById('summary-body').innerHTML = '';
  document.getElementById('activity-log').innerHTML = '';
  document.getElementById('btn-stop').disabled = false;
  setStatus('running', 'Running…');

  // Connect WebSocket and wait for it to open before starting run
  await connectWS();

  // POST to start run
  let r, data;
  try {
    r = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(cfg),
    });
    data = await r.json();
  } catch(e) {
    setStatus('error', 'Network error: ' + e.message);
    document.getElementById('setup-view').classList.remove('hidden');
    document.getElementById('run-view').classList.add('hidden');
    return;
  }
  if (!r.ok || data.error || data.detail) {
    const msg = data.detail || data.error || `HTTP ${r.status}`;
    setStatus('error', 'Error: ' + (typeof msg === 'string' ? msg : JSON.stringify(msg)).substring(0, 120));
    document.getElementById('setup-view').classList.remove('hidden');
    document.getElementById('run-view').classList.add('hidden');
    return;
  }
  if (data.run_id) {
    document.getElementById('meta-run-id').textContent = data.run_id;
  }
}

function connectWS() {
  return new Promise(resolve => {
    if (ws && ws.readyState === WebSocket.OPEN) { resolve(); return; }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => resolve();
    ws.onmessage = e => handleEvent(JSON.parse(e.data));
    ws.onclose = () => { ws = null; };
    ws.onerror = () => resolve(); // don't block run on WS error
  });
}

function handleEvent(ev) {
  const { event } = ev;

  if (event === 'benchmark_start') {
    const { benchmark, total } = ev;
    benchState[benchmark] = { total, done: 0, grades: {}, categories: {} };
    renderBenchRow(benchmark);

  } else if (event === 'start') {
    createTaskPanel(ev.task_id, ev.benchmark);

  } else if (event === 'skip') {
    if (benchState[ev.benchmark]) {
      benchState[ev.benchmark].done++;
      updateBenchRow(ev.benchmark);
    }

  } else if (event === 'step') {
    appendTaskStep(ev.task_id, ev.step_type, ev.content);

  } else if (event === 'done') {
    const { task_id, benchmark, grade, ground_truth, explanation, judge_model, judge_output, category } = ev;
    finishTaskPanel(task_id, grade, ground_truth, explanation, judge_model || '', judge_output || '');
    if (benchState[benchmark]) {
      // Track per-task grade so re-emitted 'done' events (e.g. PAC1 placeholder
      // → real grade after submit_run) update counters in-place instead of
      // double-counting.
      benchState[benchmark]._taskGrades = benchState[benchmark]._taskGrades || {};
      const prev = benchState[benchmark]._taskGrades[task_id];
      if (prev === undefined) {
        benchState[benchmark].done++;
      } else {
        benchState[benchmark].grades[prev] = Math.max(0, (benchState[benchmark].grades[prev] || 0) - 1);
        if (category && benchState[benchmark].categories[category]) {
          benchState[benchmark].categories[category].grades[prev] = Math.max(
            0, (benchState[benchmark].categories[category].grades[prev] || 0) - 1);
        }
      }
      benchState[benchmark]._taskGrades[task_id] = grade;
      benchState[benchmark].grades[grade] = (benchState[benchmark].grades[grade] || 0) + 1;
      if (category) {
        if (!benchState[benchmark].categories[category]) benchState[benchmark].categories[category] = { done: 0, grades: {} };
        if (prev === undefined) benchState[benchmark].categories[category].done++;
        benchState[benchmark].categories[category].grades[grade] = (benchState[benchmark].categories[category].grades[grade] || 0) + 1;
      }
      updateBenchRow(benchmark);
    }

  } else if (event === 'error') {
    const errText = (ev.error || '').substring(0, 200);
    if (ev.task_id && activeTasks[ev.task_id]) {
      finishTaskPanel(ev.task_id, 'ERROR');
      appendTaskStep(ev.task_id, 'error', errText);
    }

  } else if (event === 'benchmark_done') {
    const { benchmark, summary } = ev;
    const correct   = (summary.grades.CORRECT || 0) + (summary.grades.PASS || 0);
    const incorrect = (summary.grades.INCORRECT || 0) + (summary.grades.FAIL || 0);
    const partial   = (summary.grades.PARTIAL || 0);
    const dead      = (summary.grades.AGENT_DEAD || 0);
    const acc = summary.accuracy;
    const accClass = acc >= 0.7 ? 'acc-high' : (acc >= 0.4 ? 'acc-mid' : 'acc-low');
    const cats = summary.categories || {};
    const hasCats = Object.keys(cats).length > 0;
    const catId = `cats-${benchmark}`;

    const fmtTok = n => n >= 1e6 ? (n/1e6).toFixed(2)+'M' : n >= 1e3 ? (n/1e3).toFixed(1)+'K' : String(n);
    const tokStr = `↑${fmtTok(summary.input_tokens||0)} ↓${fmtTok(summary.output_tokens||0)}`;

    const card = document.createElement('div');
    card.className = 'summary-card';
    card.innerHTML = `
      <div class="summary-card-name" title="${escHtml(benchmark)}">${escHtml(benchmark)}</div>
      <div class="summary-card-acc ${accClass}">${(acc*100).toFixed(1)}%</div>
      <div class="summary-card-bar"><div class="summary-card-fill ${accClass}" style="width:${(acc*100).toFixed(1)}%"></div></div>
      <div class="summary-card-stats">
        <span class="s-correct">✓ ${correct}</span>
        <span class="s-incorrect">✗ ${incorrect}</span>
        ${partial ? `<span style="color:var(--yellow)">~ ${partial}</span>` : ''}
        ${dead ? `<span style="color:var(--red)" title="agent died (infra)">☠ ${dead}</span>` : ''}
        <span>n=${summary.total}</span>
      </div>
      <div class="summary-card-tok">${tokStr}</div>
      ${hasCats ? `<button class="cat-expand-btn" onclick="toggleCats('${catId}')">▸ by domain</button>` : ''}
    `;
    document.getElementById('summary-body').appendChild(card);

    if (hasCats) {
      const catDiv = document.createElement('div');
      catDiv.id = catId;
      catDiv.style.display = 'none';
      catDiv.style.gridColumn = '1 / -1';
      let catHtml = '<table class="cat-table" style="width:100%">';
      for (const [cat, cdata] of Object.entries(cats).sort()) {
        const cacc = cdata.accuracy;
        const cacc_cls = cacc >= 0.7 ? 'cat-acc-high' : (cacc >= 0.4 ? 'cat-acc-mid' : 'cat-acc-low');
        const ccorrect = (cdata.grades.CORRECT || 0) + (cdata.grades.PASS || 0);
        const cincorrect = (cdata.grades.INCORRECT || 0) + (cdata.grades.FAIL || 0);
        catHtml += `<tr><td>${escHtml(cat)}</td><td class="${cacc_cls}">${(cacc*100).toFixed(1)}%</td><td style="color:var(--green)">+${ccorrect}</td><td style="color:var(--red)">-${cincorrect}</td><td>n=${cdata.n}</td></tr>`;
      }
      catHtml += '</table>';
      catDiv.innerHTML = catHtml;
      document.getElementById('summary-body').appendChild(catDiv);
    }

    const fill = document.getElementById(`fill-${benchmark}`);
    if (fill) { fill.style.width = '100%'; fill.classList.add('done'); }

  } else if (event === 'log') {
    appendActivityLog(ev.level || 'info', ev.message || '');

  } else if (event === 'run_started') {
    document.getElementById('meta-run-id').textContent = ev.run_id || '—';

  } else if (event === 'run_done') {
    setStatus('done', 'Done');
    document.getElementById('btn-stop').disabled = true;

  } else if (event === 'run_error') {
    const errText = ev.error || 'Unknown error';
    setStatus('error', 'Error');
    appendActivityLog('error', errText);
    document.getElementById('btn-stop').disabled = true;

  } else if (event === 'report_progress') {
    if (window._pendingReportTask === ev.task_id) {
      _setReportProgress(ev.message || '');
    }

  } else if (event === 'report_done') {
    if (window._pendingReportTask === ev.task_id) {
      _setReportProgress('Downloading…');
      fetch(`/api/report/result/${ev.task_id}`)
        .then(r => r.blob())
        .then(b => { _downloadBlob(b, 'benchmark_report.html'); })
        .catch(e => alert('Download error: ' + e.message))
        .finally(() => {
          window._pendingReportTask = null;
          _setReportBtns(false);
          _setReportProgress('');
        });
    }

  } else if (event === 'report_error') {
    if (window._pendingReportTask === ev.task_id) {
      alert('Analysis error: ' + (ev.message || 'unknown'));
      window._pendingReportTask = null;
      _setReportBtns(false);
      _setReportProgress('');
    }
  }
}

function onHarnessChange() {
  const type = document.querySelector('input[name="harness_type"]:checked')?.value || '';
  document.getElementById('hermes-opts').style.display   = type.includes('hermes')   ? '' : 'none';
  document.getElementById('opencode-opts').style.display = type.includes('opencode') ? '' : 'none';
  document.getElementById('openclaw-opts').style.display = type.includes('openclaw') ? '' : 'none';
  document.getElementById('pac1-opts').style.display = document.getElementById('cb_pac1')?.checked ? '' : 'none';
}

function onWebSearchChange() {
  const on = document.getElementById('web_search').checked;
  document.getElementById('tavily-label').style.display = on ? '' : 'none';
  document.getElementById('tavily_key').style.display = on ? '' : 'none';
}

function toggleCats(id) {
  const row = document.getElementById(id);
  if (!row) return;
  const vis = row.style.display === 'none';
  row.style.display = vis ? '' : 'none';
  // update button text
  const btn = document.querySelector(`button[onclick="toggleCats('${id}')"]`);
  if (btn) btn.textContent = vis ? '▾ by domain' : '▸ by domain';
}

// ── Per-task panels ─────────────────────────────────────────────────────────

function _getOrCreateBenchGroup(benchmark) {
  if (benchGroups[benchmark]) {
    return benchGroups[benchmark];
  }
  const safeBench = CSS.escape(benchmark);
  const groupEl = document.createElement('div');
  groupEl.className = 'bench-group';
  groupEl.id = `bgroup-${safeBench}`;

  const hdr = document.createElement('div');
  hdr.className = 'bench-group-header';
  hdr.innerHTML = `
    <span class="bench-group-name">${escHtml(benchmark)}</span>
    <span class="bench-group-meta">
      <span class="bench-group-running"><span class="dot running"></span><span id="bgroup-cnt-${safeBench}">0 running</span></span>
    </span>
    <span class="bench-group-toggle" id="bgroup-tog-${safeBench}">▸</span>`;
  hdr.onclick = () => {
    const tasksEl = document.getElementById(`bgroup-tasks-${safeBench}`);
    const tog = document.getElementById(`bgroup-tog-${safeBench}`);
    const collapsed = tasksEl.classList.toggle('collapsed');
    if (tog) tog.textContent = collapsed ? '▸' : '▾';
  };

  // Tasks start collapsed
  const tasksEl = document.createElement('div');
  tasksEl.className = 'bench-group-tasks collapsed';
  tasksEl.id = `bgroup-tasks-${safeBench}`;

  groupEl.appendChild(hdr);
  groupEl.appendChild(tasksEl);
  document.getElementById('active-tasks').appendChild(groupEl);

  benchGroups[benchmark] = { groupEl, tasksEl, count: 0, done: 0 };
  return benchGroups[benchmark];
}

function _updateBenchGroupCounter(benchmark) {
  const g = benchGroups[benchmark];
  if (!g) return;
  const running = g.count - g.done;
  const cntEl = document.getElementById(`bgroup-cnt-${CSS.escape(benchmark)}`);
  if (!cntEl) return;
  if (running > 0) {
    cntEl.textContent = `${running} running`;
    cntEl.parentElement.querySelector('.dot').className = 'dot running';
  } else {
    cntEl.textContent = `${g.done} done`;
    cntEl.parentElement.querySelector('.dot').className = 'dot done';
  }
}

function createTaskPanel(taskId, benchmark) {
  document.getElementById('active-tasks-empty').style.display = 'none';
  const safeId = CSS.escape(taskId);
  const group = _getOrCreateBenchGroup(benchmark);
  group.count++;
  _updateBenchGroupCounter(benchmark);

  const el = document.createElement('div');
  el.id = `task-${safeId}`;
  el.className = 'task-panel';
  el.innerHTML = `
    <div class="task-panel-header" onclick="toggleTaskLog('${taskId.replace(/'/g,"\\'")}')">
      <span class="task-panel-id">${escHtml(taskId)}</span>
      <div style="display:flex;gap:6px;align-items:center">
        <span class="task-panel-badge running" id="badge-${safeId}">running</span>
        <span class="task-panel-toggle" id="toggle-${safeId}">▾</span>
      </div>
    </div>
    <div class="task-panel-log collapsed" id="tlog-${safeId}"></div>`;
  group.tasksEl.appendChild(el);
  activeTasks[taskId] = { el, logEl: el.querySelector('.task-panel-log'), collapsed: true, benchmark };
}

function toggleTaskLog(taskId) {
  const t = activeTasks[taskId];
  if (!t) return;
  const safeId = CSS.escape(taskId);
  t.collapsed = !t.collapsed;
  t.logEl.classList.toggle('collapsed', t.collapsed);
  const tog = document.getElementById(`toggle-${safeId}`);
  if (tog) tog.textContent = t.collapsed ? '▸' : '▾';
}

function makeStepBlock(stepType, text, startCollapsed) {
  const label = STEP_LABELS[stepType] || stepType;
  const preview = text.substring(0, 100).replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim();
  const block = document.createElement('div');
  block.className = `step-block type-${stepType}`;
  const bodyId = 'sb-' + Math.random().toString(36).slice(2);
  block.innerHTML = `
    <div class="step-block-header" onclick="toggleStepBlock(this,'${bodyId}')">
      <span class="step-block-label">${escHtml(label)}</span>
      <span class="step-block-preview">${escHtml(preview)}</span>
      <span class="step-block-toggle">▾</span>
    </div>
    <div class="step-block-body${startCollapsed ? ' collapsed' : ''}" id="${bodyId}"></div>`;
  // Use textContent to render actual newlines correctly (pre-wrap CSS handles them)
  block.querySelector('.step-block-body').textContent = text;
  return block;
}

function toggleStepBlock(headerEl, bodyId) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  const tog = headerEl.querySelector('.step-block-toggle');
  const collapsed = body.classList.toggle('collapsed');
  if (tog) tog.textContent = collapsed ? '▸' : '▾';
}

function formatInput(msgs) {
  if (!Array.isArray(msgs)) return JSON.stringify(msgs, null, 2);
  return msgs.map(m => {
    const role = (m.role || '?').toUpperCase();
    let body = '';
    if (typeof m.content === 'string') {
      body = m.content;
    } else if (Array.isArray(m.content)) {
      body = m.content.map(p => typeof p === 'string' ? p : (p.text || JSON.stringify(p))).join('\\n');
    } else if (m.content === null || m.content === undefined) {
      // assistant message with tool_calls has null content — show tool calls instead
      if (Array.isArray(m.tool_calls) && m.tool_calls.length > 0) {
        body = m.tool_calls.map(tc => {
          let args = tc.function?.arguments || '';
          try { args = JSON.stringify(JSON.parse(args), null, 2); } catch(e) {}
          return `${tc.function?.name}(\n${args}\n)`;
        }).join('\\n');
      } else {
        body = '';
      }
    } else {
      body = JSON.stringify(m.content, null, 2);
    }
    return `── ${role} ──\n${body}`;
  }).join('\\n\\n');
}

function appendTaskStep(taskId, stepType, content) {
  const t = activeTasks[taskId];
  if (!t) return;

  // status = infrastructure message (container start, pull, etc.) — show in header, not as a step block
  if (stepType === 'status') {
    const safeId = CSS.escape(taskId);
    let statusEl = document.getElementById(`status-${safeId}`);
    if (!statusEl) {
      statusEl = document.createElement('div');
      statusEl.id = `status-${safeId}`;
      statusEl.style.cssText = 'font-size:0.75em;color:var(--muted);padding:2px 12px 4px;font-style:italic';
      t.el.querySelector('.task-panel-header').insertAdjacentElement('afterend', statusEl);
    }
    statusEl.textContent = String(content || '');
    if (!content) statusEl.style.display = 'none';
    return;
  }

  let text = '';
  if (typeof content === 'object' && content !== null) {
    if (stepType === 'tool_call') {
      const args = JSON.stringify(content.args || {}, null, 2);
      text = `${content.name}(\n${args}\n)`;
    } else if (stepType === 'input') {
      text = formatInput(content);
    } else {
      text = JSON.stringify(content, null, 2);
    }
  } else {
    text = String(content || '');
  }
  // All blocks start collapsed — user expands manually
  const block = makeStepBlock(stepType, text, true);
  t.logEl.appendChild(block);
  t.logEl.scrollTop = t.logEl.scrollHeight;
}

function finishTaskPanel(taskId, grade, groundTruth, explanation, judgeModel, judgeOutput) {
  const t = activeTasks[taskId];
  if (!t) return;
  const isReemit = !!t._finished;
  const safeId = CSS.escape(taskId);
  const badge = document.getElementById(`badge-${safeId}`);
  if (badge) {
    const isOk = grade === 'CORRECT' || grade === 'PASS';
    const isTimeout = grade === 'TIMEOUT';
    const isErr = grade === 'INCORRECT' || grade === 'FAIL' || grade === 'ERROR' || grade === 'AGENT_DEAD';
    badge.textContent = grade;
    badge.className = `task-panel-badge ${isOk ? 'done' : isTimeout ? 'timeout' : isErr ? 'failed' : 'partial'}`;
  }
  // Judge block: show verdict + detail + judge model. Replace any previous
  // judge block so re-emitted 'done' events (PAC1 EVALUATING → real grade)
  // don't pile up duplicate blocks in the task log.
  if (t._judgeBlock && t._judgeBlock.parentNode) {
    t._judgeBlock.parentNode.removeChild(t._judgeBlock);
    t._judgeBlock = null;
  }
  if (explanation || judgeModel || judgeOutput) {
    const lines = [];
    if (judgeModel) lines.push(`Model    ${judgeModel}`);
    lines.push(`Verdict  ${grade}`);
    if (explanation) lines.push(`Detail   ${explanation}`);
    if (judgeOutput) lines.push(`Response ${judgeOutput}`);
    const block = makeStepBlock('judge', lines.join('\\n'), false);
    t.logEl.appendChild(block);
    t._judgeBlock = block;
  }
  if (groundTruth && !t._gtBlock) {
    t._gtBlock = makeStepBlock('expected', groundTruth, false);
    t.logEl.appendChild(t._gtBlock);
  }
  t.logEl.scrollTop = t.logEl.scrollHeight;
  // Update benchmark group counter only on the first finish for this task.
  if (!isReemit && t.benchmark) {
    const g = benchGroups[t.benchmark];
    if (g) { g.done++; _updateBenchGroupCounter(t.benchmark); }
  }
  t._finished = true;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function renderBenchRow(name) {
  const div = document.createElement('div');
  div.className = 'bench-row';
  div.id = `bench-row-${name}`;
  div.innerHTML = `
    <div class="bench-row-header">
      <span class="bench-name">${name}</span>
      <span class="bench-stats" id="stats-${name}">0 / 0</span>
    </div>
    <div class="progress-track"><div class="progress-fill" id="fill-${name}"></div></div>`;
  document.getElementById('bench-rows').appendChild(div);
}

function updateBenchRow(name) {
  const st = benchState[name];
  if (!st) return;
  const pct = st.total ? (st.done / st.total * 100).toFixed(1) : 0;
  const fill = document.getElementById(`fill-${name}`);
  if (fill) fill.style.width = pct + '%';
  const correct   = (st.grades.CORRECT || 0) + (st.grades.PASS || 0);
  const incorrect = (st.grades.INCORRECT || 0) + (st.grades.FAIL || 0);
  const timeouts  = (st.grades.TIMEOUT || 0);
  const dead      = (st.grades.AGENT_DEAD || 0);
  const acc = st.done ? ((correct / st.done) * 100).toFixed(1) : '—';
  const statsEl = document.getElementById(`stats-${name}`);
  if (statsEl) {
    const toPart = timeouts ? ` <span style="color:#e3b341">T:${timeouts}</span>` : '';
    const deadPart = dead ? ` <span style="color:var(--red)" title="agent died (infra)">D:${dead}</span>` : '';
    statsEl.innerHTML = `${st.done}/${st.total} &nbsp; ${acc}% &nbsp; <span class="bench-stat-correct">+${correct}</span> <span class="bench-stat-incorrect">-${incorrect}</span>${toPart}${deadPart}`;
  }
}

function setStatus(state, text) {
  const dot = document.getElementById('status-dot');
  dot.className = 'dot ' + state;
  document.getElementById('status-text').textContent = text;
}

// Append a run-level activity line (Docker/DB/preparation). Colored by level.
const _LOG_COLORS = { info: 'var(--muted,#8b949e)', ok: 'var(--green,#3fb950)',
                      warn: '#d29922', error: 'var(--red,#f85149)' };
function appendActivityLog(level, message) {
  const wrap = document.getElementById('activity-log');
  if (!wrap || !message) return;
  const line = document.createElement('div');
  line.style.color = _LOG_COLORS[level] || _LOG_COLORS.info;
  line.style.whiteSpace = 'pre-wrap';
  line.textContent = message;
  wrap.appendChild(line);
  wrap.scrollTop = wrap.scrollHeight;
}

async function stopRun() {
  try {
    await fetch('/api/stop', { method: 'POST' });
  } catch(e) {}
  setStatus('error', 'Stopped');
  document.getElementById('btn-stop').disabled = true;
}

function showSetup() {
  document.getElementById('run-view').classList.add('hidden');
  document.getElementById('history-view').classList.add('hidden');
  document.getElementById('setup-view').classList.remove('hidden');
  document.getElementById('setup-error').textContent = '';
  document.getElementById('setup-error').style.color = '';
}

function backToSetup() {
  // Leaving a (possibly live) run: tear down the ws session, then route to setup.
  if (ws) { ws.close(); ws = null; }
  activeTasks = {};
  navigate('#/');
}

async function showHistory() {
  _reportSelected.clear();
  document.getElementById('report-bar').classList.remove('visible');
  document.getElementById('setup-view').classList.add('hidden');
  document.getElementById('history-view').classList.remove('hidden');
  document.getElementById('run-detail').classList.add('hidden');
  document.getElementById('history-list-wrap').classList.remove('hidden');
  document.getElementById('history-back-row').classList.remove('hidden');
  const list = document.getElementById('history-list');
  list.innerHTML = '<span style="color:var(--muted);font-size:12px">Loading…</span>';
  try {
    const r = await fetch('/api/runs');
    const runs = await r.json();
    if (!runs.length) {
      list.innerHTML = '<span style="color:var(--muted);font-size:12px">No runs yet.</span>';
      return;
    }
    list.innerHTML = '';
    for (const run of runs) {
      const benches = Object.entries(run.benchmarks || {})
        .map(([b, s]) => {
          const acc = (s.accuracy*100).toFixed(0);
          const cls = s.accuracy >= 0.7 ? 'var(--green)' : s.accuracy >= 0.4 ? 'var(--yellow)' : 'var(--red)';
          return `<span style="color:${cls}">${escHtml(b)} ${acc}%</span>`;
        }).join(' &nbsp; ');
      const el = document.createElement('div');
      el.className = 'run-hist-item';
      el.style.cursor = 'pointer';
      const harnessTag = run.harness
        ? ` &nbsp;<span style="background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:10px">${escHtml(run.harness)}</span>`
        : '';
      const safeId = run.run_id.replace(/'/g,"\\'");
      el.innerHTML = `
        <input type="checkbox" class="run-hist-cb" onchange="onReportCbChange(this,'${safeId}',event)">
        <div style="flex:1" onclick="navigate('#/run/${safeId}')">
          <div class="run-hist-id">${escHtml(run.run_id)}${harnessTag}</div>
          <div class="run-hist-meta" style="margin-top:3px">${escHtml(run.model || '')} &nbsp;&nbsp; ${benches}</div>
        </div>
        <button class="run-hist-del" onclick="deleteRun('${safeId}', this)" title="Delete run">✕</button>`;
      list.appendChild(el);
    }
  } catch(e) {
    list.innerHTML = `<span style="color:var(--red);font-size:12px">Error: ${e.message}</span>`;
  }
}

// ── Report selection ──────────────────────────────────────────────────────────
const _reportSelected = new Set();

function onReportCbChange(cb, runId, evt) {
  evt.stopPropagation();
  const item = cb.closest('.run-hist-item');
  if (cb.checked) {
    _reportSelected.add(runId);
    item.classList.add('selected');
  } else {
    _reportSelected.delete(runId);
    item.classList.remove('selected');
  }
  const bar = document.getElementById('report-bar');
  const cnt = document.getElementById('report-sel-count');
  if (_reportSelected.size > 0) {
    bar.classList.add('visible');
    cnt.textContent = `${_reportSelected.size} run${_reportSelected.size > 1 ? 's' : ''} selected`;
  } else {
    bar.classList.remove('visible');
  }
}

function clearReportSelection() {
  _reportSelected.clear();
  document.querySelectorAll('.run-hist-cb').forEach(cb => { cb.checked = false; });
  document.querySelectorAll('.run-hist-item').forEach(el => el.classList.remove('selected'));
  document.getElementById('report-bar').classList.remove('visible');
}

function _setReportBtns(disabled) {
  document.querySelectorAll('.btn-report, .btn-report-ai').forEach(b => b.disabled = disabled);
}

function _setReportProgress(msg) {
  const el = document.getElementById('report-progress');
  if (el) el.textContent = msg;
}

async function generateReport(withAnalysis) {
  if (!_reportSelected.size) return;
  _setReportBtns(true);

  if (!withAnalysis) {
    // Sync path — immediate download
    _setReportProgress('Generating…');
    try {
      const resp = await fetch('/api/report', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({run_ids: [..._reportSelected]}),
      });
      if (!resp.ok) { alert('Report generation failed'); return; }
      _downloadBlob(await resp.blob(), 'benchmark_report.html');
    } catch(e) {
      alert('Error: ' + e.message);
    } finally {
      _setReportBtns(false);
      _setReportProgress('');
    }
    return;
  }

  // Async path — analysis via WS progress
  _setReportProgress('Starting analysis…');
  await connectWS();  // ensure WS is open before starting
  try {
    const resp = await fetch('/api/report/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({run_ids: [..._reportSelected]}),
    });
    if (!resp.ok) { alert('Failed to start analysis'); _setReportBtns(false); _setReportProgress(''); return; }
    const {task_id} = await resp.json();
    // progress + done handled by WS listener below
    window._pendingReportTask = task_id;
  } catch(e) {
    alert('Error: ' + e.message);
    _setReportBtns(false);
    _setReportProgress('');
  }
}

function _downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

const _histTaskSteps = new Map();  // tid -> steps[]

async function openRunDetail(runId) {
  _histTaskSteps.clear();
  // Make this route self-sufficient: reveal history-view even on a direct
  // load of #/run/<id> (F5 / shared link), where it would otherwise be hidden.
  document.getElementById('setup-view').classList.add('hidden');
  document.getElementById('run-view').classList.add('hidden');
  document.getElementById('history-view').classList.remove('hidden');
  // Hide list, show detail as its own page
  document.getElementById('history-list-wrap').classList.add('hidden');
  document.getElementById('history-back-row').classList.add('hidden');
  const detail = document.getElementById('run-detail');
  detail.classList.remove('hidden');
  detail.innerHTML = `<div class="card"><span style="color:var(--muted);font-size:12px">Loading ${escHtml(runId)}…</span></div>`;
  try {
    const [summaryR, tasksR] = await Promise.all([
      fetch(`/api/runs/${encodeURIComponent(runId)}`),
      fetch(`/api/runs/${encodeURIComponent(runId)}/tasks`),
    ]);
    const summary = await summaryR.json();
    const tasksByBench = await tasksR.json();

    let html = `<div class="btn-row" style="margin-bottom:12px">
      <button class="btn btn-ghost" onclick="navigate('#/history')">← All Runs</button>
    </div>
    <div class="card">
      <h2>${escHtml(runId)}</h2>
      <div style="color:var(--muted);font-size:12px;margin-bottom:16px">Model: <strong style="color:var(--text)">${escHtml(summary.model || '')}</strong> &nbsp; Harness: ${escHtml(summary.harness || '')}</div>`;

    for (const [bench, bsum] of Object.entries(summary.benchmarks || {})) {
      const acc = bsum.accuracy;
      const accCls = acc >= 0.7 ? 'acc-high' : acc >= 0.4 ? 'acc-mid' : 'acc-low';
      const correct = (bsum.grades?.CORRECT || 0) + (bsum.grades?.PASS || 0);
      const incorrect = (bsum.grades?.INCORRECT || 0) + (bsum.grades?.FAIL || 0);
      const dead = (bsum.grades?.AGENT_DEAD || 0);
      const deadStr = dead ? ` &nbsp; <span style="color:var(--red)" title="agent died (infra)">☠${dead}</span>` : '';

      html += `<div style="margin-bottom:20px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span class="bench-name">${escHtml(bench)}</span>
          <span class="${accCls}" style="font-weight:600">${(acc*100).toFixed(1)}% &nbsp; <span style="color:var(--green)">+${correct}</span> <span style="color:var(--red)">-${incorrect}</span>${deadStr} &nbsp; n=${bsum.total}</span>
        </div>`;

      // Category breakdown
      const cats = bsum.categories || {};
      if (Object.keys(cats).length > 0) {
        html += `<table class="cat-table" style="margin-bottom:8px">`;
        for (const [cat, cdata] of Object.entries(cats).sort()) {
          const cacc = cdata.accuracy;
          const ccls = cacc >= 0.7 ? 'cat-acc-high' : cacc >= 0.4 ? 'cat-acc-mid' : 'cat-acc-low';
          const cc = (cdata.grades?.CORRECT || 0) + (cdata.grades?.PASS || 0);
          const ci = (cdata.grades?.INCORRECT || 0) + (cdata.grades?.FAIL || 0);
          html += `<tr><td>${escHtml(cat)}</td><td class="${ccls}">${(cacc*100).toFixed(1)}%</td><td style="color:var(--green)">+${cc}</td><td style="color:var(--red)">-${ci}</td><td>n=${cdata.n}</td><td></td></tr>`;
        }
        html += `</table>`;
      }

      // Task list
      const tasks = tasksByBench[bench] || [];
      if (tasks.length) {
        const detId = `td-${bench}`;
        html += `<button class="cat-expand-btn" onclick="toggleCats('${detId}')">▸ tasks (${tasks.length})</button>
          <div id="${detId}" style="display:none;margin-top:8px">`;
        for (const t of tasks) {
          const isOk = t.grade === 'CORRECT' || t.grade === 'PASS';
          const isErr = t.grade === 'INCORRECT' || t.grade === 'FAIL';
          const gradeColor = isOk ? 'var(--green)' : isErr ? 'var(--red)' : 'var(--yellow)';
          const tid = `tdet-${bench}-${t.task_id}`.replace(/[^a-zA-Z0-9-]/g, '_');
          html += `<div style="margin-bottom:6px;border:1px solid var(--border);border-radius:4px;overflow:hidden">
            <div style="display:flex;align-items:center;gap:8px;padding:5px 10px;background:var(--surface);cursor:pointer" onclick="toggleHistTask('${tid}')">
              <span style="color:${gradeColor};font-weight:700;font-size:10px;width:70px">${escHtml(t.grade)}</span>
              <span style="flex:1;font-size:11px;color:var(--text)">${escHtml(t.task_id)}</span>
              <span id="${tid}-tog" style="font-size:10px;color:var(--muted)">▸</span>
            </div>
            <div id="${tid}" style="display:none;background:var(--bg)"></div>
          </div>`;
          _histTaskSteps.set(tid, { runId: runId, taskId: t.task_id, explanation: t.explanation || '', judge: t.judge_output || '' });
        }
        html += `</div>`;
      }
      html += `</div>`;
    }
    html += `</div>`;
    detail.innerHTML = html;
  } catch(e) {
    detail.innerHTML = `<div class="btn-row" style="margin-bottom:12px"><button class="btn btn-ghost" onclick="navigate('#/history')">← All Runs</button></div><div class="card"><span style="color:var(--red)">Error: ${escHtml(e.message)}</span></div>`;
  }
}

function backToHistoryList() {
  document.getElementById('run-detail').classList.add('hidden');
  document.getElementById('history-list-wrap').classList.remove('hidden');
  document.getElementById('history-back-row').classList.remove('hidden');
}

async function toggleHistTask(tid) {
  const body = document.getElementById(tid);
  const tog = document.getElementById(tid + '-tog');
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? 'block' : 'none';
  if (tog) tog.textContent = open ? '▾' : '▸';
  if (open && !body._rendered) {
    body._rendered = true;
    await renderHistTaskSteps(tid, body);
  }
}

async function renderHistTaskSteps(tid, container) {
  const data = _histTaskSteps.get(tid) || {};
  const explanation = data.explanation || '';
  const judgeOut = data.judge || '';

  // Lazy-load steps from server
  let steps = [];
  if (data.runId && data.taskId) {
    try {
      const r = await fetch(`/api/runs/${encodeURIComponent(data.runId)}/tasks/${encodeURIComponent(data.taskId)}/steps`);
      steps = await r.json();
    } catch(e) { steps = []; }
  }

  const logEl = document.createElement('div');
  logEl.className = 'task-log';
  logEl.style.cssText = 'max-height:600px;overflow-y:auto;padding:6px 0';

  if (steps.length) {
    for (const s of steps) {
      if (s.type === 'status') continue;
      let text = '';
      if (typeof s.content === 'object' && s.content !== null) {
        if (s.type === 'tool_call') {
          const args = JSON.stringify(s.content.args || {}, null, 2);
          text = `${s.content.name}(\\n${args}\\n)`;
        } else if (s.type === 'input') {
          text = formatInput(s.content);
        } else {
          text = JSON.stringify(s.content, null, 2);
        }
      } else {
        text = String(s.content || '');
      }
      logEl.appendChild(makeStepBlock(s.type, text, true));
    }
  }

  if (explanation || judgeOut) {
    const lines = [];
    if (explanation) lines.push(`Detail   ${explanation}`);
    if (judgeOut) lines.push(`Response ${judgeOut}`);
    logEl.appendChild(makeStepBlock('judge', lines.join('\\n'), false));
  }

  container.appendChild(logEl);
}

async function deleteRun(runId, btn) {
  btn.disabled = true;
  try {
    const r = await fetch(`/api/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' });
    const data = await r.json();
    if (data.ok) {
      btn.closest('.run-hist-item').remove();
      document.getElementById('run-detail').classList.add('hidden');
    } else {
      alert('Error: ' + (data.error || 'unknown'));
      btn.disabled = false;
    }
  } catch(e) {
    alert('Error: ' + e.message);
    btn.disabled = false;
  }
}

init();

// ── Analysis Model Settings Modal ──────────────────────────────────────────
async function openAnalysisModelSettings() {
  let modal = document.getElementById('analysis-model-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'analysis-model-modal';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:flex-start;justify-content:center;z-index:1000;overflow-y:auto;padding:24px 16px';
    const inp = 'width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;font-size:12px;padding:6px 10px;box-sizing:border-box';
    const sec = 'font-size:10px;font-weight:600;color:#484f58;text-transform:uppercase;letter-spacing:.06em;margin:14px 0 8px;padding-top:12px;border-top:1px solid #21262d';
    const lbl = 'font-size:11px;color:#8b949e;display:block;margin-bottom:3px';
    modal.innerHTML = `
      <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:20px 24px;width:460px;max-width:100%;margin:auto">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
          <span style="font-size:14px;font-weight:600;color:#e6edf3">AI Analysis Model</span>
          <button onclick="document.getElementById('analysis-model-modal').remove()" style="background:none;border:none;color:#8b949e;font-size:18px;cursor:pointer;line-height:1">✕</button>
        </div>
        <div style="font-size:11px;color:#484f58;margin-bottom:14px">Separate from benchmark model</div>

        <label style="display:block;margin-bottom:10px">
          <span style="${lbl}">Base URL</span>
          <input id="am-base-url" type="text" style="${inp}" placeholder="https://api.anthropic.com/v1">
        </label>
        <label style="display:block;margin-bottom:10px">
          <span style="${lbl}">API Key</span>
          <input id="am-api-key" type="text" autocomplete="off" autocorrect="off" spellcheck="false" style="${inp}" placeholder="sk-...">
        </label>
        <div style="display:grid;grid-template-columns:2fr 1fr;gap:10px;margin-bottom:0">
          <label>
            <span style="${lbl}">Model Name</span>
            <input id="am-model-name" type="text" style="${inp}" placeholder="claude-haiku-4-5-20251001">
          </label>
          <label>
            <span style="${lbl}">Temperature</span>
            <input id="am-temperature" type="number" min="0" max="1" step="0.05" style="${inp}" placeholder="0.3">
          </label>
        </div>

        <div style="${sec}">Samples</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
          <label>
            <span style="${lbl}">Bad samples (max)</span>
            <input id="am-max-bad" type="number" min="1" max="20" style="${inp}">
          </label>
          <label>
            <span style="${lbl}">Good samples (max)</span>
            <input id="am-max-good" type="number" min="0" max="10" style="${inp}">
          </label>
        </div>

        <div style="${sec}">Step truncation (chars)</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
          <label>
            <span style="${lbl}">[TASK]</span>
            <input id="am-limit-task" type="number" min="100" style="${inp}">
          </label>
          <label>
            <span style="${lbl}">[THINKING]</span>
            <input id="am-limit-thinking" type="number" min="50" style="${inp}">
          </label>
          <label>
            <span style="${lbl}">[TOOL_CALL]</span>
            <input id="am-limit-tool-call" type="number" min="50" style="${inp}">
          </label>
          <label>
            <span style="${lbl}">[TOOL_RESULT]</span>
            <input id="am-limit-tool-result" type="number" min="50" style="${inp}">
          </label>
          <label>
            <span style="${lbl}">[OUTPUT]</span>
            <input id="am-limit-output" type="number" min="50" style="${inp}">
          </label>
        </div>

        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">
          <button onclick="document.getElementById('analysis-model-modal').remove()" style="background:#21262d;border:1px solid #30363d;border-radius:6px;color:#8b949e;cursor:pointer;font-size:12px;padding:7px 16px">Cancel</button>
          <button onclick="saveAnalysisModel()" style="background:#238636;border:1px solid #2ea043;border-radius:6px;color:#fff;cursor:pointer;font-size:12px;font-weight:600;padding:7px 16px">Save</button>
        </div>
        <div id="am-status" style="font-size:11px;margin-top:8px;text-align:right;min-height:14px"></div>
      </div>`;
    document.body.appendChild(modal);
  }
  // Load current settings
  try {
    const r = await fetch('/api/analysis-model');
    if (r.ok) {
      const d = await r.json();
      document.getElementById('am-base-url').value = d.base_url || '';
      document.getElementById('am-api-key').value = d.api_key || '';
      document.getElementById('am-model-name').value = d.model_name || '';
      document.getElementById('am-temperature').value = d.temperature ?? 0.3;
      document.getElementById('am-max-bad').value = d.max_bad_samples ?? 3;
      document.getElementById('am-max-good').value = d.max_good_samples ?? 2;
      document.getElementById('am-limit-task').value = d.limit_task ?? 600;
      document.getElementById('am-limit-thinking').value = d.limit_thinking ?? 350;
      document.getElementById('am-limit-tool-call').value = d.limit_tool_call ?? 200;
      document.getElementById('am-limit-tool-result').value = d.limit_tool_result ?? 100;
      document.getElementById('am-limit-output').value = d.limit_output ?? 400;
    }
  } catch(e) {}
}

async function saveAnalysisModel() {
  const _int = id => parseInt(document.getElementById(id).value) || 0;
  const body = {
    base_url:          document.getElementById('am-base-url').value.trim(),
    api_key:           document.getElementById('am-api-key').value.trim(),
    model_name:        document.getElementById('am-model-name').value.trim(),
    temperature:       parseFloat(document.getElementById('am-temperature').value) || 0.3,
    max_bad_samples:   _int('am-max-bad'),
    max_good_samples:  _int('am-max-good'),
    limit_task:        _int('am-limit-task'),
    limit_thinking:    _int('am-limit-thinking'),
    limit_tool_call:   _int('am-limit-tool-call'),
    limit_tool_result: _int('am-limit-tool-result'),
    limit_output:      _int('am-limit-output'),
  };
  const status = document.getElementById('am-status');
  try {
    const r = await fetch('/api/analysis-model', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (r.ok) {
      status.style.color = '#3fb950';
      status.textContent = 'Сохранено';
      setTimeout(() => document.getElementById('analysis-model-modal')?.remove(), 800);
    } else {
      status.style.color = '#f85149';
      status.textContent = 'Ошибка сохранения';
    }
  } catch(e) {
    status.style.color = '#f85149';
    status.textContent = 'Ошибка: ' + e.message;
  }
}
</script>
</body>
</html>
"""

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Connect DB inside uvicorn's event loop so asyncpg pool works correctly
    if _db is not None:
        await _db.connect()
    yield
    if _db is not None:
        await _db.close()


app = FastAPI(lifespan=_lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    from framework.benchmarks._discovery import discover_all
    from framework.benchmarks.base import _BENCHMARK_REGISTRY
    discover_all()
    benchmarks_js = json.dumps(sorted(_BENCHMARK_REGISTRY.keys()))
    return HTML.replace("__BENCHMARKS_PLACEHOLDER__", benchmarks_js)


@app.get("/api/harnesses")
async def get_harnesses():
    """Return list of available harness types (filenames in framework/harnesses/)."""
    import importlib.util
    harnesses_dir = Path(__file__).parent.parent / "harnesses"
    types = []
    for f in sorted(harnesses_dir.glob("*.py")):
        if f.stem.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f.stem, f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            import inspect
            from framework.harnesses.base import Harness
            for _, obj in inspect.getmembers(mod, inspect.isclass):
                if obj is not Harness and issubclass(obj, Harness) and getattr(obj, "type", None):
                    types.append(obj.type)
        except Exception:
            pass
    return JSONResponse(sorted(set(types)))


@app.get("/api/config")
async def get_config():
    # Serve the RAW config (no env-expansion) so the form shows ${ENV} exactly
    # as written in the file. Combined with the raw-based save_config, this makes
    # the edit round-trip lossless: untouched ${OPENAI_API_KEY} stays a placeholder
    # instead of being baked into the file as a literal secret. Symmetric with
    # save_config. Falls back to the validated config if no source file is set.
    if _current_config_path and Path(_current_config_path).exists():
        return JSONResponse(yaml.safe_load(Path(_current_config_path).read_text()) or {})
    return JSONResponse(_current_cfg.model_dump())


_CONFIGS_DIR = Path("configs")


@app.get("/api/configs")
async def list_configs():
    """List *.yaml config presets and the currently-loaded one (for the picker)."""
    configs = sorted(f.name for f in _CONFIGS_DIR.glob("*.yaml")) if _CONFIGS_DIR.exists() else []
    current = Path(_current_config_path).name if _current_config_path else ""
    return JSONResponse({"configs": configs, "current": current})


@app.post("/api/load-config")
async def load_config(payload: dict):
    """Switch the active run-param preset. Infra (database) stays the deployment
    one — only managed sections come from the file."""
    global _current_cfg, _current_config_path
    name = payload.get("name", "")

    if not name:
        # "New (defaults)" — reset the active preset to a fresh RunConfig.
        _current_cfg = RunConfig()
        _current_cfg.database = _deployment_db
        _current_config_path = ""
        return JSONResponse(_current_cfg.model_dump())

    if "/" in name or "\\" in name or ".." in name or not name.endswith(".yaml"):
        return JSONResponse({"ok": False, "error": "invalid config name"}, status_code=400)
    target = _CONFIGS_DIR / name
    if not target.exists():
        return JSONResponse({"ok": False, "error": "config not found"}, status_code=404)

    _current_cfg = RunConfig.from_yaml(target)
    _current_cfg.database = _deployment_db          # deployment DB wins, never the file's
    _current_config_path = str(target)
    # Return the RAW yaml (placeholders intact) for the form — symmetric with get_config.
    return JSONResponse(yaml.safe_load(target.read_text()) or {})


@app.post("/api/save-config")
async def save_config(payload: dict):
    cfg_data = payload.get("config", {})
    save_path = payload.get("path", "configs/my_run.yaml")
    try:
        target = Path(save_path)
        # Base for the merge is the RAW yaml (no env-expansion), so infra
        # sections — including ${ENV} placeholders and DB passwords — survive
        # exactly as written. Prefer the file being overwritten; fall back to
        # the config the server was started with so a Save-As inherits infra.
        base: dict = {}
        if target.exists():
            base = yaml.safe_load(target.read_text()) or {}
        elif _current_config_path and Path(_current_config_path).exists():
            base = yaml.safe_load(Path(_current_config_path).read_text()) or {}

        merged = dict(base)
        for key in UI_MANAGED_SECTIONS:        # overlay ONLY what the UI owns
            if key in cfg_data:
                merged[key] = cfg_data[key]
        merged.pop("run_id", None)             # never persist a runtime id

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.dump(merged, allow_unicode=True, sort_keys=False))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/run")
async def api_run(body: dict):
    global _run_task, _current_orchestrator
    # Stop any currently running task before starting a new one.
    if _current_orchestrator is not None:
        _current_orchestrator.stop()
    if _run_task and not _run_task.done():
        _run_task.cancel()
        try:
            await _run_task
        except (asyncio.CancelledError, Exception):
            pass
    cfg = _build_config(body)
    _run_task = asyncio.create_task(_do_run(cfg))
    from framework.orchestrator import _make_run_id
    # run_id will be set inside orchestrator; return a placeholder that matches
    return {"run_id": cfg.run_id or "(generating…)"}


@app.post("/api/stop")
async def api_stop():
    global _run_task, _current_orchestrator
    if _current_orchestrator is not None:
        _current_orchestrator.stop()
    if _run_task and not _run_task.done():
        _run_task.cancel()
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)


@app.get("/api/runs")
async def list_runs():
    if _db and _db._enabled:
        return await _db.fetch_runs_list()
    return []


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    if ".." in run_id or "/" in run_id or "\\" in run_id:
        return JSONResponse({"error": "invalid run_id"}, status_code=400)
    if _db and _db._enabled:
        data = await _db.fetch_run_summary(run_id)
        if data:
            return data
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/runs/{run_id}/tasks")
async def get_run_tasks(run_id: str):
    if ".." in run_id or "/" in run_id or "\\" in run_id:
        return JSONResponse({"error": "invalid run_id"}, status_code=400)
    if _db and _db._enabled:
        return await _db.fetch_run_tasks(run_id)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/runs/{run_id}/tasks/{task_id}/steps")
async def get_task_steps(run_id: str, task_id: str):
    """Lazy-load steps for one task."""
    if ".." in run_id or "/" in run_id or "\\" in run_id:
        return JSONResponse({"error": "invalid run_id"}, status_code=400)
    if _db and _db._enabled:
        return await _db.fetch_task_steps(run_id, task_id)
    return []


@app.post("/api/report")
async def generate_report_endpoint(body: dict):
    """Sync report (no analysis). Returns HTML immediately."""
    from framework.report import generate_report
    run_ids = body.get("run_ids", [])
    if not run_ids:
        return JSONResponse({"error": "no run_ids provided"}, status_code=400)
    html = await generate_report(run_ids, db=_db)
    return Response(
        content=html,
        media_type="text/html",
        headers={"Content-Disposition": 'attachment; filename="benchmark_report.html"'},
    )


@app.post("/api/report/analyze")
async def generate_report_with_analysis(body: dict):
    """Start async report+analysis task. Returns {task_id}. Progress streamed via WS."""
    import uuid
    run_ids = body.get("run_ids", [])
    if not run_ids:
        return JSONResponse({"error": "no run_ids provided"}, status_code=400)
    task_id = uuid.uuid4().hex[:12]
    asyncio.create_task(_do_report_analysis(task_id, run_ids))
    return {"task_id": task_id}


@app.get("/api/analysis-model")
async def get_analysis_model():
    """Return current analysis model settings (api_key masked)."""
    from framework.config import AnalysisModelConfig
    cfg: AnalysisModelConfig = _analysis_model_cfg or AnalysisModelConfig()
    return {
        "base_url":        cfg.base_url,
        "api_key":         cfg.api_key,
        "model_name":      cfg.model_name,
        "temperature":     cfg.temperature,
        "max_bad_samples":  cfg.max_bad_samples,
        "max_good_samples": cfg.max_good_samples,
        "limit_task":       cfg.limit_task,
        "limit_thinking":   cfg.limit_thinking,
        "limit_tool_call":  cfg.limit_tool_call,
        "limit_tool_result": cfg.limit_tool_result,
        "limit_output":     cfg.limit_output,
    }


@app.post("/api/analysis-model")
async def set_analysis_model(body: dict):
    """Update analysis model settings in memory and persist to config.yaml."""
    global _analysis_model_cfg
    from framework.config import AnalysisModelConfig
    current = _analysis_model_cfg or AnalysisModelConfig()
    api_key = body.get("api_key", current.api_key)
    def _int(key, default): return int(body.get(key, default))
    def _flt(key, default): return float(body.get(key, default))
    _analysis_model_cfg = AnalysisModelConfig(
        base_url=body.get("base_url", current.base_url),
        api_key=api_key,
        model_name=body.get("model_name", current.model_name),
        temperature=_flt("temperature", current.temperature),
        max_bad_samples=_int("max_bad_samples", current.max_bad_samples),
        max_good_samples=_int("max_good_samples", current.max_good_samples),
        limit_task=_int("limit_task", current.limit_task),
        limit_thinking=_int("limit_thinking", current.limit_thinking),
        limit_tool_call=_int("limit_tool_call", current.limit_tool_call),
        limit_tool_result=_int("limit_tool_result", current.limit_tool_result),
        limit_output=_int("limit_output", current.limit_output),
    )
    # Persist to config.yaml
    if _current_config_path:
        try:
            import yaml
            with open(_current_config_path) as f:
                data = yaml.safe_load(f) or {}
            data["analysis_model"] = {
                "base_url":          _analysis_model_cfg.base_url,
                "api_key":           _analysis_model_cfg.api_key,
                "model_name":        _analysis_model_cfg.model_name,
                "temperature":       _analysis_model_cfg.temperature,
                "max_bad_samples":   _analysis_model_cfg.max_bad_samples,
                "max_good_samples":  _analysis_model_cfg.max_good_samples,
                "limit_task":        _analysis_model_cfg.limit_task,
                "limit_thinking":    _analysis_model_cfg.limit_thinking,
                "limit_tool_call":   _analysis_model_cfg.limit_tool_call,
                "limit_tool_result": _analysis_model_cfg.limit_tool_result,
                "limit_output":      _analysis_model_cfg.limit_output,
            }
            with open(_current_config_path, "w") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True}


@app.get("/api/report/result/{task_id}")
async def get_report_result(task_id: str):
    """Download finished report HTML. Removes it from memory after download."""
    html_bytes = _report_results.pop(task_id, None)
    if html_bytes is None:
        return JSONResponse({"error": "not found or already downloaded"}, status_code=404)
    return Response(
        content=html_bytes,
        media_type="text/html",
        headers={"Content-Disposition": 'attachment; filename="benchmark_report.html"'},
    )


async def _do_report_analysis(task_id: str, run_ids: list[str]) -> None:
    """Background task: run AI analysis then build HTML, broadcast progress via WS."""
    from framework.report   import generate_report
    from framework.analyzer import analyze_runs

    async def progress(msg: str) -> None:
        await _broadcast({"event": "report_progress", "task_id": task_id, "message": msg})

    await progress("Collecting run data…")

    # Load summaries from DB
    summaries = []
    for rid in run_ids:
        if ".." in rid or "/" in rid or "\\" in rid:
            continue
        if _db and _db._enabled:
            s = await _db.fetch_run_summary(rid)
            if s:
                summaries.append(s)

    if not summaries:
        await _broadcast({"event": "report_error", "task_id": task_id, "message": "No valid runs found"})
        return

    # Run AI analysis
    analysis = await analyze_runs(summaries, db=_db, progress_cb=progress, analysis_model_cfg=_analysis_model_cfg)

    # Build HTML
    await progress("Rendering report…")
    html = await generate_report(run_ids, db=_db, analysis=analysis)
    _report_results[task_id] = html.encode()

    await _broadcast({"event": "report_done", "task_id": task_id})


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str):
    if ".." in run_id or "/" in run_id or "\\" in run_id:
        return JSONResponse({"ok": False, "error": "invalid run_id"}, status_code=400)
    if not (_db and _db._enabled):
        return JSONResponse({"ok": False, "error": "database not available"}, status_code=503)
    try:
        await _db.delete_run(run_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

# Single source of truth for the config split between what the Web UI owns and
# what is infrastructure. UI may read/write only UI_MANAGED_SECTIONS; INFRA
# sections are config-file-only — never overwritten on save, never replaced by
# defaults on run. `run_id` belongs to neither: it is runtime-only and must not
# be persisted into a config file (a stale id would trigger resume/collision).
UI_MANAGED_SECTIONS = ("model", "judge_model", "search", "harness", "benchmarks", "parallelism")
INFRA_SECTIONS = ("database", "docker", "swe_bench", "analysis_model")


def _build_config(body: dict) -> RunConfig:
    from framework.config import (
        ModelConfig, SearchConfig, HarnessConfig,
        BenchmarkConfig, ParallelismConfig,
    )
    model = ModelConfig(**body.get("model", {}))
    judge_raw = body.get("judge_model")
    judge_model = ModelConfig(**judge_raw) if judge_raw else None
    search = SearchConfig(**body.get("search", {}))
    parallelism_raw = body.get("parallelism", {})
    parallelism = ParallelismConfig(**parallelism_raw)

    benchmarks = []
    for b in body.get("benchmarks", []):
        bc = BenchmarkConfig(
            name=b["name"],
            limit=b.get("limit"),
            web_search=b.get("web_search", False),
            categories=b.get("categories", []),
            task_types=b.get("task_types", []),
        )
        benchmarks.append(bc)

    harness_raw = body.get("harness", {})
    harness = HarnessConfig(**harness_raw) if harness_raw else HarnessConfig()

    cfg = RunConfig(
        model=model,
        judge_model=judge_model,
        search=search,
        harness=harness,
        benchmarks=benchmarks,
        parallelism=parallelism,
        # Infra comes from the loaded config, not UI/defaults. analysis_model is
        # omitted on purpose — runs don't use it (it's served via /api/analysis-model).
        database=_current_cfg.database,
        docker=_current_cfg.docker,
        swe_bench=_current_cfg.swe_bench,
    )
    return cfg


async def _broadcast(event_data: dict) -> None:
    dead = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_text(json.dumps(event_data))
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


def _make_cb(loop: asyncio.AbstractEventLoop):
    def cb(event: str, **kw: Any) -> None:
        payload = {"event": event, **kw}
        asyncio.run_coroutine_threadsafe(_broadcast(payload), loop)
    return cb


def _safe_content(v: Any) -> Any:
    """Ensure value is JSON-serializable."""
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    if isinstance(v, dict):
        return {k: _safe_content(vv) for k, vv in v.items()}
    if isinstance(v, (list, tuple)):
        return [_safe_content(i) for i in v]
    return str(v)


async def _do_run(cfg: RunConfig) -> None:
    global _current_orchestrator
    from framework.orchestrator import Orchestrator

    def cb(event: str, **kw: Any) -> None:
        payload = {"event": event}
        for k, v in kw.items():
            payload[k] = _safe_content(v)
        asyncio.create_task(_broadcast(payload))

    orch = Orchestrator(cfg, progress_cb=cb)
    _current_orchestrator = orch
    # send run_id to clients
    await _broadcast({"event": "run_started", "run_id": orch.run_id})
    try:
        await orch.run()
        await _broadcast({"event": "run_done", "run_id": orch.run_id})
    except asyncio.CancelledError:
        await _broadcast({"event": "run_error", "error": "Cancelled"})
    except Exception as e:
        await _broadcast({"event": "run_error", "error": str(e)})


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def _find_free_port(preferred: int = 8765) -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", preferred))
            return preferred
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]


def start_web_ui(cfg: RunConfig, config_path: str = "", open_browser: bool = True) -> None:
    import logging as _logging
    # Suppress noisy framework loggers in console — they go to uvicorn access log only
    _logging.getLogger("framework").setLevel(_logging.ERROR)
    _logging.getLogger("framework.sandbox").setLevel(_logging.ERROR)

    global _current_cfg, _current_config_path, _db, _analysis_model_cfg, _deployment_db
    from framework.config import resolve_database
    _current_cfg = cfg
    _current_config_path = config_path

    # DB is a deployment property: env-override -> config -> none. Resolved once
    # and fixed for the service lifetime; the config picker never changes it, so
    # the web _db and the per-run Orchestrator DB always point at the same
    # database (consistent history).
    _deployment_db = resolve_database(cfg.database)
    _current_cfg.database = _deployment_db

    # Load analysis model settings from config
    from framework.config import AnalysisModelConfig
    _analysis_model_cfg = cfg.analysis_model if hasattr(cfg, "analysis_model") else AnalysisModelConfig()

    # Create DB instance — actual connection happens in lifespan (uvicorn's event loop)
    if _deployment_db:
        from framework.db import Database
        _db = Database(_deployment_db)

    import uvicorn

    port = _find_free_port(8765)
    url = f"http://localhost:{port}"

    if open_browser:
        import threading
        def _open_browser():
            import time
            time.sleep(0.8)
            webbrowser.open(url)

        threading.Thread(target=_open_browser, daemon=True).start()
    print(f"\n  Harness Testing Framework — Web UI\n  Open: {url}\n  (Ctrl+C to stop)\n")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
