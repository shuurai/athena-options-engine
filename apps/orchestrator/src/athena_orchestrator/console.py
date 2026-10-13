"""Local operator console. It is not published on the approve-page tunnel."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from html import escape, unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from athena_exec.accounts import trading_mode
from athena_exec.halt import active_reason
from athena_exec.ops import gateway_open
from athena_exec.slots import HKT, slot_state
from athena_orchestrator.cockpit import (
    footer,
    screen_bar,
    health_report,
    run_ops,
    save_slots,
    schedule,
    slot_windows,
    sleeve,
    watchlist,
)
from athena_settings import load_active
from athena_store import connect
from athena_orchestrator import pages
from athena_orchestrator.tasks import begin_fill, begin_positions, list_tasks
from athena_orchestrator.view import bind_view, current_view

_MARK = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" aria-hidden="true">
  <defs><linearGradient id="strangle" x1="8" y1="0" x2="56" y2="64" gradientUnits="userSpaceOnUse">
    <stop offset="0" stop-color="#2f6fed"/><stop offset="1" stop-color="#0e7490"/>
  </linearGradient></defs>
  <path fill="url(#strangle)" d="M0 0H64V52L52 64H0Z"/>
  <path fill="none" stroke="#fff" stroke-width="4" d="M14 34H50"/>
  <path fill="none" stroke="#22d3ee" stroke-width="4" d="M24 34L40 16"/>
  <path fill="none" stroke="#fff" stroke-width="4" d="M24 34L40 52"/>
</svg>"""
_MARK_HREF = " ".join(_MARK.split()).replace("#", "%23").replace('"', "'")

NAV_GROUPS = (
    ("View", ()),
    (
        "Core logic",
        (
            ("/", "Cockpit"),
            ("/drafts", "Drafts"),
            ("/phase", "NFR board"),
            ("/audit", "Audit"),
            ("/news", "News"),
            ("/universe", "Universe"),
            ("/history", "History"),
            ("/positions", "Positions"),
            ("/orders", "Orders"),
            ("/book", "Book"),
        ),
    ),
    (
        "Configs",
        (
            ("/accounts", "Accounts"),
            ("/simulation", "Simulation"),
            ("/orchestration", "Orchestration"),
            ("/risk", "Risk & Strategy"),
            ("/volatility", "Volatility"),
            ("/settings", "Settings"),
            ("/skill", "Skill"),
        ),
    ),
)

_EXTRA_CSS = """
html, body.console { height: 100%; overflow: hidden; }
.app { height: 100vh; min-height: 0; overflow: hidden; }
.nav { height: 100vh; min-height: 0; overflow: auto; }
.nav select.nav-view {
  width: calc(100% - 20px); margin: 0 10px 8px; padding: 6px 8px;
  font-family: var(--font-mono); font-size: 11px;
  background: #10141b; color: var(--site-text); border: 1px solid var(--site-border);
}
.main {
  height: 100vh; min-height: 0; overflow: hidden;
  display: flex; flex-direction: column; padding: 16px 24px;
}
.topbar { flex: none; }
.top-end { display: flex; align-items: flex-start; justify-content: flex-end; gap: 10px; }
.stage {
  flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column;
  gap: 14px; overflow: hidden;
}
.stage > .note, .stage > .profiles { flex: none; }
.stage > .book-stack { flex: 1 1 auto; min-height: 0; }
.book-stack > .card { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.book-heat { flex: 1 1 0; width: 100%; min-height: 0; display: block; overflow: hidden; }
.stage > .fill, .stage > form.fill, .stage > .split, .stage > .sim-page { flex: 1 1 auto; min-height: 0; }
.sim-page { display: flex; flex-direction: column; gap: 14px; }
.sim-facts { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px 16px; margin: 4px 0 14px; }
.sim-facts .k { font-size: 10px; letter-spacing: .06em; text-transform: uppercase; color: var(--site-text-4); font-family: var(--font-mono); }
.sim-facts .v { margin-top: 4px; font-family: var(--font-mono); font-size: 13px; color: var(--site-text); display: flex; align-items: center; gap: 8px; }
.sim-spin {
  width: 12px; height: 12px; flex: none; border-radius: 50%;
  border: 2px solid #2a3340; border-top-color: #6b96f2;
  animation: sim-spin .7s linear infinite;
}
@keyframes sim-spin { to { transform: rotate(360deg); } }
.sim-note { margin: 0 0 12px; color: var(--site-text-3); font-size: 12.5px; }
.sim-charts { flex: 1 1 auto; min-height: 0; }
.sim-progress { flex: none; }
.sim-idle {
  flex: 1 1 auto; min-height: 0;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px;
  color: var(--site-text-3);
}
.sim-idle svg { width: 96px; height: 96px; animation: sim-bob 3.6s ease-in-out infinite; }
.sim-idle p { margin: 0; font-size: 13px; }
@keyframes sim-bob {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-6px); }
}
.sim-now {
  position: absolute; top: 26px; bottom: 2px; width: 1px; margin-left: -0.5px;
  background: #6b96f2; pointer-events: none;
  box-shadow: 0 0 8px rgba(107, 150, 242, .55);
  animation: sim-now 2.8s ease-in-out infinite;
}
@keyframes sim-now {
  0%, 100% { opacity: .28; }
  50% { opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .sim-idle svg, .sim-spin, .sim-now { animation: none; }
  .sim-now { opacity: 1; }
}
.sim-row { margin-bottom: 12px; }
.sim-row h2 { justify-content: flex-start; }
.sim-window { display: inline-flex; align-items: center; gap: 6px; margin-left: 10px; }
.sim-box { position: relative; height: 480px; }
.sim-plot { position: absolute; inset: 8px 12px 8px; cursor: crosshair; }
.sim-plot svg { display: block; width: 100%; height: 100%; }
.sim-read { position: absolute; left: 8px; top: 4px; z-index: 2; font: 12px/1.3 var(--font-mono); color: var(--site-text-2); pointer-events: none; white-space: nowrap; }
.sim-read .up { color: #16c784; }
.sim-read .dn { color: #ea3943; }
.sim-read .mu { color: #8b95a3; }
.sim-plot.space { cursor: grab; }
.sim-plot.dragging { cursor: grabbing; }
.btn:disabled { opacity: .45; cursor: default; }
.stage > .share { flex: 1 1 0; min-height: 0; }
.stage > .card.fill { flex: 1.4 1 0; }
.share > .card { display: flex; flex-direction: column; min-height: 0; overflow: auto; }
.share .chart-h-lg, .share .chart-h-md { flex: 1 1 auto; height: auto; min-height: 72px; }
.stage > .split { overflow: auto; }
.stage > .banner { flex: none; margin: 0; }
.draft-hot { border-color: #2f4a7a; }
.draft-title { font-size: 17px; font-weight: 700; margin-bottom: 4px; }
.draft-sub { font-size: 12.5px; color: var(--site-text-3); margin: 0 0 12px; }
.draft-id { margin-left: auto; font-size: 10.5px; color: var(--site-text-4); }
.stage > .draft-cols {
  flex: 1 1 auto; min-height: 0; overflow: hidden;
  display: grid; grid-template-columns: 3fr 2fr; gap: 14px;
}
.draft-stack { min-height: 0; overflow: auto; display: flex; flex-direction: column; gap: 14px; }
.draft-stack > .card { flex: none; }
.draft-empty { min-height: 420px; display: flex; flex-direction: column; }
.draft-empty .sim-idle { flex: 1 1 auto; min-height: 420px; }
.draft-cols > .card.fill { min-height: 0; }
.card.fill, form.fill { display: flex; flex-direction: column; overflow: hidden; }
.card.fill > h2, .card.fill > .filters, .card.fill > form, form.fill > .row-actions { flex: none; }
.card.fill > .scroll-y, form.fill > .grid { flex: 1 1 auto; min-height: 0; overflow: auto; }
.feed-sentinel { height: 1px; }
.stage > form.grid.fill {
  display: grid; overflow: hidden; align-items: stretch;
  grid-template-rows: minmax(0, 1fr);
}
.stage > form.grid.fill > .card { min-height: 0; overflow: auto; }
form.fill > .settings-cols {
  flex: 1 1 auto; min-height: 0; overflow: hidden;
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px;
}
.settings-col { min-height: 0; display: flex; flex-direction: column; gap: 14px; overflow: hidden; }
.settings-col > .card { flex: none; height: auto; overflow: visible; }
.settings-col > .card > .scroll-y { overflow: visible; }
.settings-col > .card.fill { flex: 1 1 auto; min-height: 0; overflow: hidden; }
.settings-col > .card.fill > .scroll-y { overflow: auto; }
.card.danger { border-color: #7f1d1d; background: #2a1212; }
.card.danger h2 { color: #fca5a5; }
.btn.danger { background: #7f1d1d; color: #fff; border-color: #7f1d1d; }
.sim-find { position: relative; margin: 4px 0 10px; }
.sim-menu {
  position: absolute; z-index: 8; left: 0; right: 0; top: calc(100% + 4px);
  max-height: 180px; overflow: auto;
  background: var(--site-surface); border: 1px solid var(--site-border-strong);
}
.sim-menu button {
  display: flex; width: 100%; gap: 8px; align-items: baseline;
  background: transparent; border: 0; color: inherit; text-align: left;
  padding: 7px 10px; cursor: pointer; font: inherit;
}
.sim-menu button:hover, .sim-menu button.on { background: #1c2430; }
.sim-menu .name { color: var(--site-text-3); font-size: 12px; font-weight: 400; }
.sim-picks { display: flex; flex-wrap: wrap; gap: 6px; max-height: 168px; overflow: auto; }
.sim-pick {
  display: inline-flex; align-items: center; gap: 2px;
  padding: 2px 2px 2px 8px; border: 1px solid var(--site-border-strong); font-size: 12px;
}
.sim-x {
  width: 18px; height: 18px; display: inline-flex; align-items: center; justify-content: center;
  background: transparent; border: 0; color: var(--site-text-4); cursor: pointer; padding: 0;
}
.sim-x:hover { color: #ea3943; }
.sim-x svg { width: 10px; height: 10px; }
.sim-empty { margin: 0; color: var(--site-text-3); font-size: 12px; }
.settings-field { position: relative; display: flex; flex-direction: column; gap: 6px; margin: 8px 0; }
.settings-field > .tag { align-self: flex-start; white-space: nowrap; }
.settings-field > label, .toggle-row > div:first-child { display: inline-flex; align-items: center; gap: 6px; }
.toggle-row { position: relative; }
.toggle-row .help-pop, .settings-field .help-pop { top: calc(100% - 4px); left: 0; right: auto; width: min(280px, 100%); }
.preset-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.scroll-y thead th { position: sticky; top: 0; background: var(--site-surface); z-index: 1; }
.stage > .strategy-scroll { flex: 1 1 auto; min-height: 0; overflow: auto; display: flex; flex-direction: column; gap: 14px; }
.cycle-split {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px; align-items: stretch;
}
.cycle-split > .card { margin: 0; min-width: 0; }
.cycle-split > form.agent {
  min-height: 0; display: flex; flex-direction: column; overflow: hidden;
}
.cycle-split > form.agent > h2 { flex: none; }
.cycle-split > form.agent > .body {
  flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column;
}
.cycle-split .body > .role,
.cycle-split .body > button { flex: none; }
.cycle-split label.prompt {
  flex: 1 1 0; min-height: 0; display: flex; flex-direction: column; margin-bottom: 12px;
}
.cycle-split textarea.prompt {
  flex: 1 1 auto; min-height: 0; height: 0; margin-top: 6px; resize: none;
}
.flow h2 span { display: inline-flex; align-items: center; gap: 8px; }
.flow .help-pop { left: 16px; right: auto; top: 42px; width: min(360px, calc(100% - 32px)); }
.flow-diagram svg { display: block; width: 100%; height: auto; }
.agent-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; align-items: start; }
.agent .role { font-size: 12px; color: var(--site-text-3); margin: 0 0 12px; line-height: 1.45; }
.agent .body > button.btn { width: 100%; align-self: stretch; }
.agent.collapsed h2 { margin-bottom: 0; }
.agent.collapsed .body { display: none; }
button.fold {
  width: 22px; height: 22px; padding: 0; flex: none;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--site-border-strong); background: transparent;
  color: var(--site-text-3); letter-spacing: 0; text-transform: none; cursor: pointer;
}
button.fold svg { display: block; }
.agent.collapsed button.fold svg { transform: rotate(-90deg); }
label.prompt { display: block; margin: 0 0 12px; font-size: 10px; letter-spacing: .06em; text-transform: uppercase; color: var(--site-text-4); font-family: var(--font-mono); }
textarea.prompt {
  display: block; width: 100%; min-height: 140px; margin-top: 6px; padding: 10px;
  border: 1px solid var(--site-border-strong); background: #0a0c10; color: #9aa5b1;
  caret-color: #c3cad4; font: 12px/1.45 var(--font-mono); resize: vertical;
}
select.intent { width: 132px; margin: 0; padding: 4px 8px; font-size: 11px; }
#audit-filters button {
  padding: 6px 12px; font-size: 11px; font-weight: 600; font-family: var(--font-mono);
  background: transparent; border: 1px solid var(--site-border-strong); color: var(--site-text-3); cursor: pointer;
}
#audit-filters button.on { background: #101c33; border-color: var(--site-primary); color: #93c5fd; }
#audit-filters input { width: 180px; margin: 0; }
tr.hit { cursor: pointer; }
.err { color: var(--ath-down); }
.think { color: #93c5fd; }
.meta { display: grid; grid-template-columns: 120px 1fr; gap: 4px 10px; margin: 0 0 14px; font-size: 12px; }
.badges { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.meta span { color: var(--site-text-4); font-family: var(--font-mono); font-size: 10px; letter-spacing: .05em; text-transform: uppercase; }
.trail { display: flex; flex-direction: column; gap: 6px; margin: 0 0 14px; font-size: 12.5px; line-height: 1.4; }
.json-block .k { display: block; margin: 0 0 6px; font-size: 10px; letter-spacing: .06em; text-transform: uppercase; color: var(--site-text-4); font-family: var(--font-mono); }
.json {
  display: block; width: 100%; box-sizing: border-box; margin: 0;
  font: 11.5px/1.45 var(--font-mono); white-space: pre-wrap; word-break: break-word;
  color: var(--site-text); background: #0a0c10; border: 1px solid var(--site-border); padding: 12px;
}
@media (max-width: 900px) {
  .cycle-split, .agent-grid { grid-template-columns: 1fr; }
  .cycle-split > form.agent { min-height: auto; overflow: visible; display: block; }
  .cycle-split label.prompt { display: block; min-height: auto; }
  .cycle-split textarea.prompt { height: auto; min-height: 140px; resize: vertical; }
}
.pool-tgl {
  width: 34px; height: 18px; position: relative; padding: 0; cursor: pointer;
  background: var(--site-surface-2); border: 1px solid var(--site-border-strong);
}
.pool-tgl::after {
  content: ""; position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; background: var(--site-text-4);
}
.pool-tgl.on { background: #101c33; border-color: var(--site-primary); }
.pool-tgl.on::after { left: 18px; background: var(--site-primary); }
.pool-tgl.green.on { background: #0d1f16; border-color: #16c784; }
.pool-tgl.green.on::after { background: #16c784; }
.sym-line { display: flex; align-items: center; gap: 8px; }
.pool-spin { visibility: hidden; flex: none; }
tr.pool-busy .pool-spin { visibility: visible; }
tr.pool-busy button, tr.pool-busy select, tr.pool-busy input { pointer-events: none; }
.sym-name { font-size: 10.5px; color: var(--site-text-4); }
.chg-up { color: var(--ath-up); }
.chg-down { color: var(--ath-down); }
.filters { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; flex: none; }
.audit-table-head { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 12px; }
.audit-table-head > .filters { flex: 1 1 auto; margin-bottom: 0; }
.card.fill > .audit-table-head { flex: none; }
.filters a, .filters button {
  padding: 6px 12px; font-size: 11px; font-weight: 600; font-family: var(--font-mono);
  background: transparent; border: 1px solid var(--site-border-strong); color: var(--site-text-3); cursor: pointer;
}
.filters a:hover, .filters button:hover { border-color: var(--site-primary); color: var(--site-primary); }
.filters a.on, .filters button.on { background: #101c33; border-color: var(--site-primary); color: #93c5fd; }
.filters input { width: 160px; margin: 0; }
.pnl-pos { color: var(--ath-up); font-family: var(--font-mono); }
.pnl-neg { color: var(--ath-down); font-family: var(--font-mono); }
tr[data-cmd="history-row"] { cursor: pointer; }
.card h2 button.more { background: none; border: 0; padding: 0; font: inherit; }
.nav .mark { background: none; width: 26px; height: 26px; color: inherit; font-size: inherit; }
.nav .mark svg { width: 26px; height: 26px; display: block; }
.chart-h-lg { position: relative; height: 180px; overflow: hidden; padding: 0; }
.chart-plot { position: absolute; inset: 12px 10px 16px; }
.chart-plot svg { display: block; width: 100%; height: 100%; }
.slot-note { margin: 0 0 12px; color: var(--site-text-3); font-size: 12px; line-height: 1.45; }
.slot-note.edit-note { margin: 18px 0 8px; padding-top: 14px; line-height: 1.5; }
.pool-filters { align-items: center; }
.pool-filters .input { width: 220px; margin: 0; padding: 7px 10px; }
.pool-filters select.input { width: 210px; }
button.sort {
  display: inline-flex; align-items: center; gap: 6px; background: none; border: 0; padding: 0; margin: 0;
  color: inherit; font: inherit; letter-spacing: inherit; text-transform: inherit; cursor: pointer;
}
button.sort:hover { color: var(--site-text-2); }
button.sort .sort-ico { display: inline-flex; }
button.sort svg { display: block; }
button.sort .up, button.sort .down { fill: currentColor; opacity: .28; }
button.sort[data-dir="asc"] { color: #93c5fd; }
button.sort[data-dir="asc"] .up { opacity: 1; }
button.sort[data-dir="desc"] { color: #93c5fd; }
button.sort[data-dir="desc"] .down { opacity: 1; }
.pool-actions { white-space: nowrap; text-align: right; }
.pool-actions .btn + .btn, .pool-actions form { margin-left: 6px; }
.trade-box label { display: block; margin: 12px 0 0; font-size: 11px; color: var(--site-text-3); }
.trade-box .input { margin-top: 6px; }
.trade-box .seg { margin-top: 8px; }
.trade-box .row-actions { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
.trade-box .row-actions .btn { width: 100%; }
.trade-box .row-actions + .slot-note { padding-top: 16px; margin-bottom: 0; }
.trade-reason {
  margin-top: 16px; padding: 14px 16px;
  border: 1px dashed #8a6a2f; background: #16130c;
}
.trade-reason[hidden] { display: none; }
.trade-reason .k {
  display: block; margin: 0 0 8px; font-size: 10px; letter-spacing: .08em;
  text-transform: uppercase; color: #d4b06a; font-family: var(--font-mono);
}
.trade-reason p { margin: 0; font-size: 12.5px; line-height: 1.55; color: var(--site-text); }
.trade-reason.err { border-color: #8f3d3d; background: #160e0e; }
.trade-reason.err .k { color: #e08a8a; }
.pool-actions form { display: inline; }
.card-lead { margin: 0 0 12px; font-size: 12.5px; color: var(--site-text-3); line-height: 1.5; }
.slot-row { display: grid; grid-template-columns: 108px 1fr 16px 1fr 32px; gap: 8px; align-items: center; margin-bottom: 8px; }
.slot-row select, .slot-row input { width: 100%; margin: 0; }
.slot-row span { text-align: center; color: var(--site-text-4); font-size: 12px; }
.slot-row .btn { padding: 6px 0; }
.slot-preview { margin: 14px 0 4px; }
.slot-preview .note { margin: 0; }
.chart-legend { display: flex; gap: 14px; font-size: 10px; color: var(--site-text-4); font-family: var(--font-mono); margin-top: 8px; }
.chart-legend span { display: inline-flex; align-items: center; gap: 5px; }
.chart-legend i { width: 10px; height: 2px; display: inline-block; }
.draft-link { color: var(--site-primary); font-weight: 600; }
.month-row { display: flex; justify-content: space-between; font-size: 10px; color: var(--site-text-4); font-family: var(--font-mono); margin-top: 6px; }
.cell-empty { color: var(--site-text-4); font-family: var(--font-mono); }
.chart-empty { color: var(--site-text-4); font-family: var(--font-mono); font-size: 12px; padding: 28px 8px; }
.health-title { display: inline-flex; align-items: center; gap: 8px; }
.health-light { width: 8px; height: 8px; border-radius: 50%; flex: none; background: #16c784; box-shadow: 0 0 0 3px #16c78433; }
.health-light.warn { background: #f7931a; box-shadow: 0 0 0 3px #f7931a33; }
.health-light.down { background: #ea3943; box-shadow: 0 0 0 3px #ea394333; }
.health-meter { display: inline-flex; align-items: flex-end; gap: 2px; height: 12px; }
.health-meter i { width: 3px; background: #1e2933; display: block; }
.health-meter i:nth-child(1) { height: 4px; }
.health-meter i:nth-child(2) { height: 6px; }
.health-meter i:nth-child(3) { height: 8px; }
.health-meter i:nth-child(4) { height: 10px; }
.health-meter i:nth-child(5) { height: 12px; }
.health-meter.ok i.on { background: #16c784; }
.health-meter.warn i.on { background: #f7931a; }
.health-meter.down i.on { background: #ea3943; }
.health-age { font-weight: 500; letter-spacing: 0; text-transform: none; color: var(--site-text-3); }
label { display: block; margin: 8px 0; color: var(--site-text-3); font-size: 12px; }
input, select { width: 100%; margin-top: 4px; padding: 8px; border: 1px solid var(--site-border); background: transparent; color: inherit; font: inherit; }
.note { color: var(--ath-halt); }
.badge-row { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
button.ok { background: #15803d; border-color: transparent; color: #fff; }
.chart-h-md { position: relative; height: 140px; overflow: hidden; padding: 0; }
.phase-charts .chart-h-md { height: 210px; }
table.data tbody tr.phase-pick { cursor: pointer; }
table.data tbody tr.phase-pick.selected { background: #101c33; box-shadow: inset 3px 0 0 #6b96f2; }
table.data tbody tr.phase-pick.selected td { color: #f2f4f7; }
table.data tbody tr.phase-pick.selected b { color: #93c5fd; }
table.data tbody tr.phase-pick.selected:hover {
  background: color-mix(in srgb, var(--site-primary) 14%, transparent);
}
.pulse-px .px-chg, .band .px-chg { display: block; font-size: 11px; font-weight: 600; }
.news-when { white-space: nowrap; color: var(--site-text-3); }
.news-title { color: var(--site-primary); }
.px-tick { animation: px-tick .7s ease; }
@keyframes px-tick { 0% { color: #93c5fd; } 100% { color: inherit; } }
.input { width: 100%; padding: 9px 11px; border: 1px solid var(--site-border-strong); font: 12.5px var(--font-mono); background: #0a0c10; color: var(--site-text); }
.sim-date { position: relative; margin-top: 4px; }
.sim-date .k { font-size: 10px; letter-spacing: .06em; text-transform: uppercase; color: var(--site-text-4); font-family: var(--font-mono); }
.sim-date-face { display: flex; align-items: center; justify-content: space-between; width: 100%; margin-top: 6px; padding: 9px 11px; border: 1px solid var(--site-border-strong); background: #0a0c10; color: var(--site-text); font: 13px var(--font-mono); pointer-events: none; }
.sim-date-face svg { width: 16px; height: 16px; color: #93c5fd; }
.sim-date input[type="date"] { position: absolute; left: 0; right: 0; bottom: 0; height: 38px; opacity: 0; cursor: pointer; }
.grid.cols-2 > .span-2 { grid-column: 1 / -1; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 16px; }
.form-grid.one { grid-template-columns: 1fr; }
.form-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.form-grid label { display: flex; align-items: center; gap: 6px; font-size: 10px; color: var(--site-text-4); text-transform: uppercase; letter-spacing: .05em; font-family: var(--font-mono); }
.form-grid > div { position: relative; }
.form-grid .help-pop { top: 22px; left: 0; right: auto; width: min(280px, 100%); }
.profiles { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
.risk-cols { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; align-items: start; }
.risk-span-5 { grid-column: span 5; }
.risk-span-2 { grid-column: span 2; min-width: 0; }
.risk-span-1 { grid-column: span 1; min-width: 0; }
.risk-stack { display: flex; flex-direction: column; gap: 14px; }
.risk-stack [data-reset-grade] { width: 100%; }
.risk-span-1 h2 { flex-wrap: wrap; align-items: flex-start; }
@media (max-width: 1100px) {
  .risk-cols { grid-template-columns: 1fr; }
  .risk-span-5, .risk-span-2, .risk-span-1 { grid-column: auto; }
  .form-grid.three { grid-template-columns: 1fr; }
}
.profile { min-width: 0; text-align: left; cursor: pointer; color: inherit; font: inherit; background: var(--site-canvas); border: 1px solid var(--site-border); padding: 12px; }
.profile.on { border-color: var(--site-primary); background: #101c33; }
.profile .name { font-weight: 700; font-size: 13px; }
.stat .value.tone-defensive { color: #4ade80; }
.stat .value.tone-standard { color: #93c5fd; }
.stat .value.tone-income, .stat .value.tone-harvest { color: #67e8f9; }
.stat .value.tone-aggressive { color: #fbbf24; }
.stat .value.tone-test { color: #c4b5fd; }
.stat .value.tone-custom { color: #fb7185; }
.profile .desc { margin-top: 6px; font-size: 12px; color: var(--site-text-3); }
.seg { display: flex; gap: 6px; flex-wrap: wrap; }
.seg.stack { flex-direction: column; align-items: stretch; gap: 8px; }
.seg button { padding: 8px 12px; border: 1px solid var(--site-border-strong); background: transparent; font-size: 11.5px; font-weight: 600; color: var(--site-text-3); font-family: var(--font-mono); }
.seg.stack button { text-align: left; padding: 10px 12px; cursor: pointer; }
.seg button.on { background: #101c33; border-color: var(--site-primary); color: #93c5fd; }
.seg button:disabled { cursor: default; }
.card { position: relative; }
.card h2 span { display: inline-flex; align-items: center; gap: 10px; }
button.help-q { width: 18px; height: 18px; padding: 0; flex: none; letter-spacing: 0; text-transform: none; border: 1px solid var(--site-border-strong); background: transparent; color: var(--site-text-3); font-size: 11px; line-height: 16px; cursor: pointer; }
.ath-panel .diff { white-space: pre-wrap; vertical-align: top; }
.help-pop { position: fixed; z-index: 30; right: auto; top: auto; left: 0; width: min(280px, calc(100vw - 24px)); padding: 10px 12px; background: #10141a; border: 1px solid var(--site-border-strong); color: var(--site-text-3); font-size: 12.5px; line-height: 1.45; font-weight: 400; letter-spacing: 0; text-transform: none; font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif); }
.tgl { margin: 0 4px 0 0; padding: 4px 8px; font-size: 10px; font-family: var(--font-mono); background: transparent; border: 1px solid var(--site-border-strong); color: var(--site-text-3); }
.tgl.on { background: #101c33; border-color: var(--site-primary); color: #93c5fd; }
.acct { border: 1px solid var(--site-border); background: var(--site-surface); padding: 16px; position: relative; }
.acct.active { border-color: var(--site-primary); box-shadow: inset 0 0 0 1px #2f6fed55; }
.acct .a-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.acct .a-name { font-size: 15px; font-weight: 700; }
.acct .a-id { display: flex; align-items: center; gap: 6px; font-family: var(--font-mono); font-size: 11px; color: var(--site-text-4); margin-top: 2px; }
.acct .a-id .copy-plain svg, .ath-panel .field .copy-plain svg { width: 16px; height: 16px; }
.ath-panel .field .v:has(.copy-plain) { display: flex; align-items: center; gap: 6px; }
.acct .a-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; margin-top: 14px; }
.acct .a-stat .l { font-size: 10px; color: var(--site-text-4); font-family: var(--font-mono); text-transform: uppercase; letter-spacing: .05em; }
.acct .a-stat .v { font-size: 15px; font-weight: 700; font-family: var(--font-mono); margin-top: 2px; }
.acct .row-actions { margin-top: 14px; }
.acct .row-actions .btn[disabled] { flex: 1; }
button.status-tgl { padding: 0; cursor: pointer; appearance: none; }
button.icon-btn { width: 32px; height: 32px; padding: 0; flex: none; }
button.icon-btn svg { width: 15px; height: 15px; display: block; }
button.icon-btn.busy svg { animation: ath-spin .7s linear infinite; }
button.btn.busy { position: relative; color: transparent; }
button.btn.busy::after {
  content: ""; position: absolute; left: 50%; top: 50%; width: 12px; height: 12px; margin: -7px 0 0 -7px;
  border: 2px solid #333d49; border-top-color: #6b96f2; border-radius: 50%;
  animation: ath-spin .7s linear infinite;
}
button.btn.primary.busy:disabled { opacity: 1; }
button.btn.primary.busy::after { border-color: rgba(255,255,255,.28); border-top-color: #fff; }
@keyframes ath-spin { to { transform: rotate(360deg); } }
.nav .foot { display: flex; flex-direction: column; align-items: stretch; gap: 6px; font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif); }
.nav .foot .chip, .nav .foot .cycle-row { align-self: flex-start; max-width: 100%; }
.nav .foot .chip.stretch { align-self: stretch; width: 100%; box-sizing: border-box; }
.chip .sq { width: 6px; height: 6px; background: currentColor; flex: none; }
.cycle-clock {
  width: 100%; box-sizing: border-box; text-align: center;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  font-size: 54px; font-weight: 700; letter-spacing: 0; line-height: 1;
  color: #93c5fd; padding: 10px 2px 12px;
  border-radius: 8px;
  background: rgba(107, 150, 242, .16);
  border: 1px solid rgba(107, 150, 242, .35);
}
.cycle-clock.busy {
  display: flex; justify-content: center; align-items: center; min-height: 76px;
}
.cycle-clock.busy span {
  display: inline-block; width: 1em; font-size: 1ch; text-align: center; line-height: 1; overflow: hidden;
}
.cycle-row { display: flex; align-items: center; gap: 6px; font-family: var(--font-mono); font-size: 10.5px; color: var(--site-text-3); }
.cycle-mark { width: 8px; height: 8px; border: 1.5px solid #6b7c90; border-radius: 50%; flex: none; }
.cycle-mark.ath-spin { width: 10px; height: 10px; margin: 0; border-width: 1.5px; }
.cycle-bar { width: 100%; height: 3px; background: #1c2430; }
.cycle-bar i { display: block; height: 100%; background: #6b96f2; }
.screen-bar { position: fixed; left: 0; right: 0; top: 0; z-index: 30; height: 3px; background: #1c2430; pointer-events: none; }
.screen-bar i { display: block; height: 100%; background: #6b96f2; }
.nav .foot [data-cycle="up"] { font-family: var(--font-mono); font-size: 10.5px; }
.chip { text-transform: none; letter-spacing: .02em; }
.stage .card h2, .stage .stat .label, .stage table.data th { text-transform: none; letter-spacing: .02em; }
.health-card h2, .health-card .field .k { text-transform: uppercase; letter-spacing: .1em; }
.stage .tag, .ath-panel .tag { text-transform: none; letter-spacing: .02em; }
.sleeve-meta, .badge-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.stat-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.stat-head .label { margin: 0; }
.acct-id { display: flex; align-items: center; gap: 4px; flex: none; font-family: var(--font-mono); font-size: 11px; color: var(--site-text-3); }
.stat-side { flex: none; font-family: var(--font-mono); font-size: 13px; font-weight: 700; color: var(--site-text-2); }
.copy-plain { display: inline-flex; padding: 0; margin: 0; border: 0; background: none; color: inherit; cursor: pointer; line-height: 0; }
.copy-plain svg { width: 13px; height: 13px; display: block; }
.copy-plain:hover { color: var(--site-primary); }
.key-row { display: flex; align-items: center; gap: 8px; }
.key-row .input { flex: 1; min-width: 0; }
.key-plain { display: inline-flex; padding: 0; margin: 0; border: 0; background: none; color: inherit; cursor: pointer; line-height: 0; }
.key-plain svg { width: 16px; height: 16px; display: block; }
.key-plain:hover { color: var(--site-primary); }
.skill-doc h3 { margin: 18px 0 6px; font-size: 14px; display: flex; align-items: center; gap: 8px; }
.skill-call { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; }
.skill-call .mono { color: var(--site-text-2); font-size: 12.5px; }
.skill-doc p, .skill-doc li { color: var(--site-text-2); }
.skill-doc pre { white-space: pre-wrap; }
.skill-doc .json .jk { color: #93c5fd; }
.skill-doc .json .js { color: #9dcea0; }
.skill-doc .json .jn { color: #e8c07a; }
.skill-doc .json .jb { color: #d2b4f0; }
.skill-doc .json .jp { color: #6b7684; }
.card.stat .meta { white-space: pre-line; }
.stat .meta.up { color: var(--ath-up); }
.stat .meta.down { color: var(--ath-down); }
.ath-panel { box-shadow: -24px 0 60px rgba(0,0,0,.45); }
.ath-panel .p-head b { font-size: 13px; letter-spacing: .04em; font-family: var(--font-mono); text-transform: none; font-weight: 600; }
.ath-panel .p-body { font-size: 12.5px; color: #c3cad4; }
.ath-panel .field .k { text-transform: none; letter-spacing: .02em; font-size: 11px; }
.ath-panel table.data th { text-transform: none; letter-spacing: .02em; }
.cycle-strip { border: 1px solid var(--site-border); background: var(--site-surface); padding: 10px 12px; margin: 0; flex: none; }
.cycle-strip-head { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 12px; color: var(--site-text-2); }
.cycle-strip [data-strip=clock] { color: #fbbf24; font-weight: 700; }
.cycle-strip [data-strip=activity] {
  flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
  color: #93c5fd; font-weight: 500;
}
.cycle-roll .stream { font-family: var(--font-mono); color: #93c5fd; }
.cycle-roll .stream .st { color: #6b96f2; }
.cycle-fold {
  margin-left: auto; width: 22px; height: 22px; padding: 0; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--site-border-strong); background: transparent; color: var(--site-text-3);
}
.cycle-fold svg { display: block; }
.cycle-strip.compact .cycle-fold svg { transform: rotate(180deg); }
.cycle-strip.compact .cycle-roll { display: none; }
.cycle-strip .cycle-bar { margin-top: 8px; }
.cycle-roll { max-height: 96px; margin-top: 8px; }
.cycle-roll div { display: flex; gap: 10px; font-size: 11.5px; line-height: 1.45; color: var(--site-text-3); }
.cycle-roll .st { width: 58px; flex: none; color: var(--site-text-4); font-family: var(--font-mono); }
.cycle-roll .on { color: #93c5fd; }
.cycle-roll .skipped { color: var(--site-text-4); }
.status-tgl { display: inline-block; width: 34px; height: 18px; position: relative; background: var(--site-surface-2); border: 1px solid var(--site-border-strong); }
.status-tgl::after { content: ""; position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; background: var(--site-text-4); }
.status-tgl.on { background: #101c33; border-color: var(--site-primary); }
.status-tgl.on::after { left: 18px; background: var(--site-primary); }
details summary { cursor: pointer; color: var(--site-text-3); }
.toggle-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 11px 0; border-bottom: 1px solid var(--site-border); }
.toggle-row:last-child { border-bottom: 0; }
.toggle-end { display: flex; align-items: center; gap: 8px; }
.toggle-end input[type=checkbox] { width: auto; margin: 0; }
input:focus,
input:focus-visible,
textarea:focus,
textarea:focus-visible,
select:focus,
select:focus-visible {
  outline: none;
  box-shadow: none;
}
input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]):hover,
textarea:hover,
select:hover {
  border-color: #3a4350;
}
input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]):focus,
input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]):focus-visible,
textarea:focus,
textarea:focus-visible,
select:focus,
select:focus-visible {
  border-color: rgba(107, 150, 242, 0.38);
}
"""


def _styles() -> str:
    root = _design_root()
    tokens = (root / "tokens" / "tokens.css").read_text(encoding="utf-8")
    console = (root / "shared" / "console.css").read_text(encoding="utf-8")
    console = console.replace('@import url("../tokens/tokens.css");', "")
    return tokens + console + _EXTRA_CSS


def _design_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "design" / "tokens" / "tokens.css").is_file():
            return parent / "design"
    return Path("design")


def start_console_server(database_url: str, port: int, allowlist: set[str]) -> ThreadingHTTPServer:
    from athena_orchestrator.pulse import configure

    configure(database_url)
    class Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:
            path = urlparse(self.path).path
            if path.startswith("/api/v1"):
                self._send_api(204, "")
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self) -> None:
            bind_view(self.headers.get("Cookie"))
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path.startswith("/api/v1"):
                with connect(database_url) as conn:
                    from athena_orchestrator.developer import handle

                    status, body = handle(conn, "GET", parsed.path, query, b"", self.headers)
                self._send_api(status, body)
                return
            if parsed.path == "/api/orders/events":
                self._send_events(_account())
                return
            if parsed.path == "/api/tasks":
                self._send(200, json.dumps({"tasks": list_tasks()}), "json")
                return
            if parsed.path == "/api/cycle":
                from athena_orchestrator.runtime import cycle_view

                self._send(200, json.dumps(cycle_view()), "json")
                return
            if parsed.path == "/api/pulse":
                with connect(database_url) as conn:
                    from athena_orchestrator.pulse import snapshot

                    account = _account()
                    self._send(200, json.dumps(snapshot(conn, account)), "json")
                return
            if parsed.path == "/api/simulation":
                with connect(database_url) as conn:
                    from athena_orchestrator.sim import progress

                    self._send(200, json.dumps(progress(conn, _account())), "json")
                return
            with connect(database_url) as conn:
                status, body, kind = _get(conn, parsed.path, query)
            self._send(status, body, kind)

        def do_POST(self) -> None:
            bind_view(self.headers.get("Cookie"))
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            if path.startswith("/api/v1"):
                with connect(database_url) as conn:
                    from athena_orchestrator.developer import handle

                    status, body = handle(conn, "POST", path, {}, raw, self.headers)
                self._send_api(status, body)
                return
            form = parse_qs(raw.decode("utf-8"))
            with connect(database_url) as conn:
                from athena_settings.resolve import live_allowlist

                live = live_allowlist(conn)
                actor = next(iter(live), "console")
                if path == "/api/tasks":
                    status, body, kind = _post_tasks(conn, form, actor, database_url)
                else:
                    status, body, kind = _post(conn, path, form, actor, live)
            self._send(status, body, kind)

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(self, status: int, body: str, kind: str = "html") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            if kind == "json":
                content = "application/json"
            elif kind == "csv":
                content = "text/csv; charset=utf-8"
                self.send_header("Content-Disposition", "attachment; filename=history.csv")
            else:
                content = "text/html; charset=utf-8"
            self.send_header("Content-Type", content)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_events(self, account: str) -> None:
            from queue import Empty

            from athena_exec.order_watch import subscribe, unsubscribe

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            listener = subscribe(account)
            try:
                while True:
                    try:
                        order_id = listener.get(timeout=15)
                    except Empty:
                        self.wfile.write(b":\n\n")
                        self.wfile.flush()
                        continue
                    with connect(database_url) as conn:
                        from athena_orchestrator.pages import order_event

                        payload = order_event(conn, order_id)
                    if payload is None or payload["account"] != account:
                        continue
                    line = "data: " + json.dumps(payload) + "\n\n"
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return
            finally:
                unsubscribe(account, listener)

        def _send_api(self, status: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Api-Key")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def _post_ops(conn, form: dict, actor: str, allowlist: set[str]) -> tuple[int, str, str]:
    from athena_orchestrator.tasks import begin_ops

    account = _account()
    command = (form.get("command") or [""])[0].strip()
    if command == "health":
        if not account:
            return 400, json.dumps({"ok": False, "message": "No account is configured."}), "json"
        try:
            message = run_ops(conn, account, actor, allowlist or {actor}, command)
            report = health_report(conn)
        except (PermissionError, ValueError) as exc:
            return 400, json.dumps({"ok": False, "message": str(exc)}), "json"
        return 200, json.dumps({"ok": True, "message": message, "report": report}), "json"
    if command in {"backup", "halt", "resume"}:
        if not account:
            return 400, json.dumps({"ok": False, "message": "No account is configured."}), "json"
        try:
            task = begin_ops(database_url_from(conn), account, actor, allowlist or {actor}, command)
        except ValueError as exc:
            return 400, json.dumps({"ok": False, "message": str(exc)}), "json"
        return 200, json.dumps({"ok": True, "task": task}), "json"
    try:
        message = run_ops(conn, account or "", actor, allowlist or {actor}, command)
    except (PermissionError, ValueError) as exc:
        return 400, json.dumps({"ok": False, "message": str(exc)}), "json"
    return 200, json.dumps({"ok": True, "message": message}), "json"


def database_url_from(conn) -> str:
    return os.environ.get("DATABASE_URL", "")


def _post_slots(conn, form: dict, actor: str, allowlist: set[str]) -> tuple[int, str, str]:
    account = _account()
    if not account:
        return 400, json.dumps({"ok": False, "message": "No account is configured."}), "json"
    if allowlist and actor not in allowlist:
        return 400, json.dumps({"ok": False, "message": "actor is not on the allowlist"}), "json"
    try:
        message = save_slots(
            conn,
            account,
            actor,
            (form.get("athena_slots") or [""])[0],
            (form.get("mobile_slots") or [""])[0],
        )
    except ValueError as exc:
        return 400, json.dumps({"ok": False, "message": str(exc)}), "json"
    return 200, json.dumps({"ok": True, "message": message}), "json"


def _post_tasks(conn, form: dict, actor: str, database_url: str) -> tuple[int, str, str]:
    account = _account()
    if not account:
        return 400, json.dumps({"ok": False, "message": "No account is configured."}), "json"
    action = (form.get("action") or [""])[0]
    try:
        if action == "positions":
            task = begin_positions(database_url, account)
        elif action == "simulate":
            from athena_orchestrator.tasks import begin_simulate

            task = begin_simulate(
                database_url,
                account,
                (form.get("start") or [""])[0],
                (form.get("months") or [""])[0],
            )
        elif action == "sim-stop":
            from athena_orchestrator.sim import stop

            return 200, json.dumps({"ok": True, "message": stop(conn, account)}), "json"
        elif action == "sim-halt":
            from athena_orchestrator.sim import halt

            return 200, json.dumps({"ok": True, "message": halt(conn, account)}), "json"
        elif action == "sim-resume":
            from athena_orchestrator.sim import resume

            return 200, json.dumps({"ok": True, "message": resume(conn, account)}), "json"
        elif action == "sim-reset":
            from athena_orchestrator.sim import reset

            return 200, json.dumps({"ok": True, "message": reset(conn, account)}), "json"
        elif action == "flush":
            from athena_orchestrator.sim import flush

            return 200, json.dumps({
                "ok": True,
                "message": flush(conn, account, (form.get("phrase") or [""])[0]),
            }), "json"
        elif action == "liquidate":
            from athena_orchestrator.tasks import begin_liquidate

            task = begin_liquidate(database_url, account)
        else:
            task = begin_fill(
                conn,
                database_url,
                account,
                actor,
                (form.get("symbol") or [""])[0],
                action,
                (form.get("market") or [""])[0],
            )
    except ValueError as exc:
        return 400, json.dumps({"ok": False, "message": str(exc)}), "json"
    return 200, json.dumps({"ok": True, "task": task}), "json"


def _get(conn, path: str, query: dict | None = None) -> tuple[int, str, str]:
    if path == "/api/health":
        report = health_report(conn)
        report["replay"] = _replay_text(conn, _account())
        return 200, json.dumps(report), "json"
    if path == "/":
        return 200, _cockpit(conn), "html"
    built = pages.build(conn, path, query or {})
    if built is None:
        return 404, _layout(path, "Missing", "<p>That page is not in the console.</p>", [], conn=conn), "html"
    return _finish(conn, path, built)


def _post(conn, path: str, form: dict, actor: str, allowlist: set[str]) -> tuple[int, str, str]:
    if path == "/api/ops":
        return _post_ops(conn, form, actor, allowlist)
    if path == "/api/slots":
        return _post_slots(conn, form, actor, allowlist)
    built = pages.submit(conn, path, form, actor, allowlist)
    if built is None:
        return 404, _layout(path, "Missing", "<p>That page is not in the console.</p>", [], conn=conn), "html"
    return _finish(conn, path, built)


_NOTE = re.compile(r'^<p class="note">(.*?)</p>')


def _finish(conn, path: str, built: dict) -> tuple[int, str, str]:
    if built["kind"] != "html":
        return built["status"], built["body"], built["kind"]
    chips = _chips(conn) + built["chips"]
    body, flash = _pull_flash(built["body"], built["status"])
    return built["status"], _layout(path, built["title"], flash + body, chips, built["lead"], built.get("actions", ""), conn), "html"


def _pull_flash(body: str, status: int) -> tuple[str, str]:
    """Action text becomes a toast. It does not stay in the page."""
    match = _NOTE.match(body)
    if not match:
        return body, ""
    text = unescape(match.group(1))
    title, kind = _toast_title(text, status < 400)
    flash = (
        f'<div id="ath-flash" hidden data-title="{escape(title, quote=True)}" '
        f'data-sub="{escape(text, quote=True)}" data-kind="{kind}"></div>'
    )
    return body[match.end():], flash


def _toast_title(text: str, ok: bool) -> tuple[str, str]:
    if not ok:
        return "Not saved", "err"
    if text.startswith("Approved"):
        return "Approved", "ok"
    if text.startswith("Rejected"):
        return "Rejected", "ok"
    if text.startswith("Amended"):
        return "Amended", "ok"
    if text.startswith("Marked"):
        return "Marked", "warn"
    if text.startswith("Stored"):
        return "Stored", "ok"
    if text.startswith("Added"):
        return "Added", "ok"
    if text.startswith("Open the Telegram"):
        return "Link", "ok"
    if "already" in text.lower():
        return "Already stored", "warn"
    return "Saved", "ok"


def _nav_badges(conn) -> dict[str, tuple[int | str, bool, str]]:
    """Counts of rows that arrived today. A zero stays off the menu."""
    account = _account()
    if not account or conn is None:
        return {}
    start = datetime.now(HKT).replace(hour=0, minute=0, second=0, microsecond=0)
    drafts = conn.execute(
        "SELECT count(*) FROM drafts WHERE account_id = %s AND status = 'pending'",
        (account,),
    ).fetchone()[0]
    news = conn.execute(
        "SELECT count(*) FROM news_events WHERE ts >= %s AND NOT dismissed",
        (start,),
    ).fetchone()[0]
    fills = conn.execute(
        "SELECT count(*) FROM fills WHERE account_id = %s AND created_at >= %s",
        (account, start),
    ).fetchone()[0]
    calls = conn.execute(
        "SELECT count(*) FROM ai_calls WHERE account_id = %s AND called_at >= %s",
        (account, start),
    ).fetchone()[0]
    decisions = conn.execute(
        "SELECT count(*) FROM decision_trails WHERE account_id = %s AND logged_at >= %s",
        (account, start),
    ).fetchone()[0]
    opened = conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (symbol, sec_type, expiry, contract_right, strike)
                   symbol, sec_type, expiry, contract_right, strike, quantity
            FROM positions
            WHERE account_id = %s
            ORDER BY symbol, sec_type, expiry, contract_right, strike, snapshot_at DESC, id DESC
        ),
        firsts AS (
            SELECT symbol, sec_type, expiry, contract_right, strike, min(snapshot_at) AS first_at
            FROM positions
            WHERE account_id = %s
            GROUP BY symbol, sec_type, expiry, contract_right, strike
        )
        SELECT count(*)
        FROM latest
        JOIN firsts
          ON latest.symbol = firsts.symbol
         AND latest.sec_type IS NOT DISTINCT FROM firsts.sec_type
         AND latest.expiry IS NOT DISTINCT FROM firsts.expiry
         AND latest.contract_right IS NOT DISTINCT FROM firsts.contract_right
         AND latest.strike IS NOT DISTINCT FROM firsts.strike
        WHERE latest.quantity <> 0 AND firsts.first_at >= %s
        """,
        (account, account, start),
    ).fetchone()[0]
    found: dict[str, tuple[int | str, bool, str]] = {
        "/drafts": (int(drafts), False, "awaiting"),
        "/news": (int(news), True, "new today"),
        "/history": (int(fills), False, "new today"),
        "/audit": (int(calls) + int(decisions), False, "today"),
        "/positions": (int(opened), False, "new today"),
    }
    from athena_orchestrator.sim import progress

    replay = progress(conn, account)
    if replay["status"] in {"active", "halted"} and int(replay["total"]) > 0:
        day = int(replay["day"])
        total = int(replay["total"])
        found["/simulation"] = (
            f"{day}/{total}",
            replay["status"] == "halted",
            f"Day {day} of {total}",
        )
    return {href: item for href, item in found.items() if item[0]}


def _layout(
    path: str,
    title: str,
    body: str,
    chips: list[tuple[str, str]],
    lead: str = "",
    actions: str = "",
    conn=None,
) -> str:
    badges = _nav_badges(conn)
    links = []
    for group, items in _nav_entries():
        links.append(f'<div class="nav-group">{escape(group)}</div>')
        if group == "View":
            links.append(_view_switch(conn))
        for href, label in items:
            current = " class=\"active\"" if href == path else ""
            badge = badges.get(href)
            mark = ""
            if badge:
                count, warn, caption = badge
                kind = "nav-badge warn" if warn else "nav-badge"
                if isinstance(count, str):
                    tip = caption
                    shown = count
                else:
                    tip = f"{count} {caption}"
                    shown = str(count)
                mark = (
                    f'<span class="{kind}" title="{escape(tip, quote=True)}">{escape(shown)}</span>'
                )
            links.append(f"<a{current} href=\"{href}\">{escape(label)}{mark}</a>")
    chip_html = "".join(
        f"<span class=\"chip {kind}\"><span class=\"dot\"></span>{escape(text)}</span>" for kind, text in chips
    )
    lead_html = f"<p>{escape(lead)}</p>" if lead else ""
    script = _CLIENT_JS
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Athena · {escape(title)}</title>
<link rel="icon" href="data:image/svg+xml,{_MARK_HREF}">
<style>{_styles()}</style>
</head><body class="console"><div class="app"><aside class="nav">
<div class="brand"><div class="mark">{_MARK}</div><div><b>Athena</b><span>Options Engine</span></div></div>
{''.join(links)}
<div class="foot">{footer()}</div>
</aside><main class="main"><div class="topbar"><div><h1>{escape(title)}</h1>{lead_html}</div>
<div class="top-end"><div class="chips">{chip_html}</div>{actions}</div></div><div class="stage">{body}</div></main></div>{screen_bar()}{script}</body></html>"""


def _chips(conn) -> list[tuple[str, str]]:
    account = _account()
    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    gateway = ("ok", "gateway up") if gateway_open(host, port, timeout=0.4) else ("danger", "gateway down")
    mode = trading_mode()
    mode_chip = ("warn", mode) if mode == "paper" else ("danger", mode)
    chips = [gateway, ("", "mode"), mode_chip]
    if not account:
        return chips + [("", "no account")]
    settings = load_active(conn, account)
    now = datetime.now(HKT)
    state = slot_state(now, settings.ib)
    window = _current_window(now, settings.ib.athena_slots, settings.ib.mobile_slots, state)
    if state == "athena":
        chips.insert(1, ("info", "athena"))
        chips.insert(2, ("", window))
    elif state == "mobile":
        chips.insert(1, ("warn", "mobile"))
        chips.insert(2, ("", window))
    else:
        chips.insert(1, ("", "idle"))
    chips.append(("", settings.privilege.replace("_", " ")))
    if active_reason(conn, account):
        chips.append(("danger", "halted"))
    return chips


def _current_window(now: datetime, athena_slots: str, mobile_slots: str, state: str) -> str:
    for row in schedule(now, athena_slots, mobile_slots):
        if state == "athena" and row["status"] == "active now":
            return row["window"]
        if state == "mobile" and row["status"] == "reserved" and row["owner"] == "mobile":
            return row["window"]
    return state


def _nav_entries() -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    """Menu groups in order. Simulation is listed only in paper mode."""
    paper = trading_mode() == "paper"
    shown = []
    for group, items in NAV_GROUPS:
        kept = tuple((href, label) for href, label in items if href != "/simulation" or paper)
        shown.append((group, kept))
    return shown


def _account() -> str:
    return current_view()


def _view_switch(conn) -> str:
    """Account picker for this mode. An id outside the list cannot become the view."""
    from athena_exec.accounts import active_accounts

    accounts = active_accounts()
    if not accounts:
        return ""
    current = current_view()
    labels: dict[str, str] = {}
    if conn is not None and current:
        try:
            settings = load_active(conn, current)
        except RuntimeError:
            settings = None
        if settings is not None:
            labels = {
                entry.account_id: entry.label
                for entry in settings.extra_accounts
                if entry.label and entry.label != entry.account_id
            }
    options = []
    for account_id in accounts:
        selected = " selected" if account_id == current else ""
        label = labels.get(account_id, "")
        text = f"{label} · {account_id}" if label else account_id
        options.append(
            f'<option value="{escape(account_id, quote=True)}"{selected}>{escape(text)}</option>'
        )
    return (
        f'<select class="nav-view" data-view-pick aria-label="Account">{"".join(options)}</select>'
    )


def _cockpit(conn) -> str:
    account = _account()
    chips = _chips(conn)
    if not account:
        return _layout("/", "Cockpit", "<div class=\"card\"><p>No account is configured for this mode.</p></div>", chips, conn=conn)
    settings = load_active(conn, account)
    now = datetime.now(HKT)
    facts = sleeve(conn, account, now)
    slots = schedule(now, settings.ib.athena_slots, settings.ib.mobile_slots)
    health = health_report(conn)
    from athena_orchestrator.pulse import enabled_symbols, note_view

    names = enabled_symbols(settings)
    note_view(names, account)
    rows = watchlist(conn, account, names, settings.strategy.ivr_min_samples, now.date())
    halted = active_reason(conn, account)
    payload = json.dumps({"nlv": facts["nlv"], "months": facts["months"], "pnl": facts["pnl"]}).replace("<", "\\u003c")
    body = _cockpit_body(
        facts, slots, health, rows, settings, halted, _sim_actions(conn, account), _replay_text(conn, account)
    )
    body += f'<script type="application/json" id="cockpit-data">{payload}</script>'
    return _layout(
        "/", "cockpit", body, chips,
        "sleeve marks, today's slots, and the watchlist for this account.",
        conn=conn,
    )


def _replay_text(conn, account: str) -> str:
    """The open replay, as one health row. Empty when nothing is in progress."""
    if not account or trading_mode() != "paper":
        return ""
    from athena_orchestrator.sim import progress

    info = progress(conn, account)
    if info["status"] not in {"downloading", "active", "halted"}:
        return ""
    if info["status"] == "downloading" or int(info["total"]) <= 0:
        return "Downloading"
    day = f"Day {int(info['day'])} of {int(info['total'])}"
    when = info["cursor_label"]
    if info["status"] == "halted":
        return ", ".join(part for part in ("Halted", day, when) if part)
    return ", ".join(part for part in (day, when) if part)


def _sim_actions(conn, account: str) -> str:
    if trading_mode() != "paper":
        return ""
    from athena_orchestrator.sim import view

    if view(conn, account) is None:
        return '<button class="btn primary" type="button" data-cmd="simulate">Simulate</button>'
    return '<button class="btn" type="button" disabled>Simulate</button>'


def _cockpit_body(
    facts: dict,
    slots: list[dict],
    health: dict,
    rows: list[dict],
    settings,
    halted: str | None,
    sim: str,
    replay: str,
) -> str:
    copy_svg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="12" height="12" rx="1"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/></svg>'
    ttl = f'<span class="tag">ttl {escape(facts["ttl"])}</span> ' if facts["ttl"] else ""
    slot_rows = "".join(
        "<tr>"
        f"<td class=\"mono\">{escape(row['window'])}</td>"
        f"<td>{escape(row['owner'])}</td>"
        f"<td><span class=\"tag {escape(row['tag'])}\">{escape(row['status'])}</span></td>"
        "</tr>"
        for row in slots
    )
    bars = "".join("<i class=\"on\"></i>" if index < health["ok"] else "<i></i>" for index in range(5))
    fields = "".join(
        f"<div class=\"field\"><div class=\"k\">{escape(item['name'])}</div><div class=\"v\" data-health=\"{escape(item['name'])}\">{escape(item['text'])}</div></div>"
        for item in health["checks"]
    )
    halt_label = "Resume" if halted else "Halt trading"
    halt_cmd = "resume" if halted else "halt"
    halt_class = "btn ghost" if halted else "btn danger"
    nlv_box = '<div class="chart-box chart-h-lg"><div class="chart-plot" id="chartNlv"></div></div>'
    pnl_box = '<div class="chart-box chart-h-lg"><div class="chart-plot" id="chartPnl"></div></div>'
    labels = "".join(f"<span>{escape(label)}</span>" for label in facts["nlv_labels"])
    pnl_labels = "".join(f"<span>{escape(item['name'])}</span>" for item in facts["months"])
    return f"""
<div class="cycle-strip" id="cycleStrip">
  <div class="cycle-strip-head">
    <span class="cycle-mark" data-strip="mark"></span>
    <span data-strip="title">Cycle</span>
    <span data-strip="meta"></span>
    <span data-strip="activity"></span>
    <span data-strip="clock"></span>
    <button class="cycle-fold" type="button" data-cmd="cycle-fold" aria-label="Collapse cycle">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 14l6-6 6 6"/></svg>
    </button>
  </div>
  <div class="cycle-bar"><i data-strip="bar"></i></div>
  <div class="cycle-roll scroll-y" data-strip="roll"></div>
</div>
<div class="grid cols-4">
  <div class="card stat"><div class="stat-head"><div class="label">sleeve nlv <span class="tag">{escape(facts["currency"])}</span></div>
    <div class="acct-id"><span>{escape(facts["account"])}</span><button class="copy-plain" type="button" data-copy="{escape(facts["account"], quote=True)}" title="Copy">{copy_svg}</button></div></div>
    <div class="value" data-sleeve="nav">{escape(facts["nav"])}</div>
    <div class="meta"><span data-sleeve="change">{escape(facts["change"])}</span></div></div>
  <div class="card stat"><div class="label">premium collected <span class="tag">{escape(facts["currency"])}</span> <span class="tag">6mo</span></div><div class="value" data-sleeve="premium">{escape(facts["premium"])}</div>
    <div class="meta" data-sleeve="premium_meta">{escape(facts["premium_meta"])}</div></div>
  <div class="card stat"><div class="stat-head"><div class="label">cash reserve <span class="tag">{escape(facts["currency"])}</span></div>
    <div class="stat-side" data-sleeve="cash_pct">{escape(facts["cash_pct"])}</div></div>
    <div class="value" data-sleeve="cash">{escape(facts["cash"])}</div>
    <div class="meta" data-sleeve="cash_meta">{escape(facts["cash_meta"])}</div></div>
  <div class="card stat"><div class="label">pending drafts</div><div class="value" data-sleeve="pending" style="color:var(--site-primary)">{facts["pending"]}</div>
    <div class="meta">{ttl}<a href="/drafts" style="color:var(--site-primary)">open queue →</a></div></div>
</div>
<div class="grid cols-2 share">
  <div class="card"><h2><span>sleeve nlv <span class="tag">stored weeks</span></span> <button class="more" data-cmd="nlv" type="button">full view →</button></h2>
    {nlv_box}<div class="month-row">{labels}</div></div>
  <div class="card"><h2>monthly premium vs net cash <button class="more" data-cmd="pnl" type="button">full view →</button></h2>
    {pnl_box}
    <div class="month-row">{pnl_labels}</div>
    <div class="chart-legend"><span><i style="background:#16c784"></i>up month</span><span><i style="background:#ea3943"></i>down month</span></div></div>
</div>
<div class="grid cols-2 share">
  <div class="card"><h2><span>ib session schedule <span class="tag">today</span></span> <button class="more" data-cmd="slots" type="button">edit slots →</button></h2>
    <table class="data"><thead><tr><th>window (hkt)</th><th>owner</th><th>status</th></tr></thead><tbody>{slot_rows}</tbody></table></div>
  <div class="card health-card" style="display:flex;flex-direction:column">
    <h2><span class="health-title">System health <span class="health-light {escape(health["state"])}" id="healthLight"></span>
      <span class="health-meter {escape(health["state"])}" id="healthMeter">{bars}</span>
      <span class="health-age" id="healthAge">{health["ok"]}/5</span></span>
      <button class="more" data-cmd="ops" type="button">ops panel →</button></h2>
    <div id="healthBody" style="flex:1">{fields}{"" if not replay else f'<div class="field"><div class="k">Replay</div><div class="v" data-health="Replay">{escape(replay)}</div></div>'}</div>
    <div class="row-actions" style="margin-top:12px">
      <button class="btn ghost icon-btn" data-cmd="health" type="button" aria-label="Refresh health"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-2.6-6.3"/><path d="M21 3v6h-6"/></svg></button>
      <button class="btn ghost" data-cmd="backup" type="button">Backup now</button>
      {sim}
      <button class="{halt_class}" data-cmd="{halt_cmd}" type="button">{halt_label}</button>
    </div>
  </div>
</div>
<div class="card fill"><h2>watchlist pulse <a class="more" href="/phase">nfr board →</a></h2>
  <div class="scroll-y"><table class="data"><thead><tr>
    <th>symbol</th><th>price</th><th>52w</th><th>ivr</th><th>phase</th><th>tags</th><th>side</th><th>strike</th><th>dte</th><th>status</th>
  </tr></thead><tbody>{_watch_html(rows)}</tbody></table></div></div>
<template id="slot-editor">
  <p class="slot-note">weekdays, hong kong time. each row is one window. gaps between them stay idle.</p>
  <div class="slot-rows"></div>
  <button class="btn ghost" data-cmd="add-slot" type="button">add window</button>
  <div class="slot-preview"></div>
  <p class="slot-note">saving writes a new settings version. privilege stays.</p>
  <div class="row-actions" style="margin-top:14px"><button class="btn primary" data-cmd="save-slots" type="button">save slots</button></div>
</template>
<script type="application/json" id="slot-windows">{json.dumps(slot_windows(settings.ib.athena_slots, settings.ib.mobile_slots)).replace("<", "\\u003c")}</script>
"""


def _watch_html(rows: list[dict]) -> str:
    body = []
    for row in rows:
        spark = f'<span class="ivr-spark" data-iv="{escape(json.dumps(row["iv"]), quote=True)}" style="display:inline-block;width:110px;vertical-align:middle"></span>'
        body.append(
            "<tr>"
            f"<td><b>{escape(row['symbol'])}</b></td>"
            f"<td class=\"mono pulse-px\" data-symbol=\"{escape(row['symbol'], quote=True)}\">"
            f"<span class=\"px\">{escape(row['price'])}</span> "
            f"<span class=\"px-chg {escape(row['tone'])}\">{escape(row['change'])}</span></td>"
            f"{_band_cell(row['band'], row['high_pct'], row['low_pct'])}"
            f"<td>{spark} <span class=\"mono\" style=\"font-size:11px;color:var(--site-text-3)\">{escape(row['rank'])}</span></td>"
            f"<td><span class=\"tag {escape(row['phase_class'])}\">{escape(row['phase'])}</span></td>"
            f"<td>{_badges(row['tags'])}</td>"
            f"<td>{_badges(row['side'])}</td>"
            f"<td>{_strikes(row['strikes'])}</td>"
            f"<td>{_dash_cell(row['dte'])}</td>"
            f"<td>{_badges(row['status'])}</td>"
            "</tr>"
        )
    return "".join(body)


def _band_cell(band: str, high_pct: str, low_pct: str) -> str:
    if not high_pct:
        return f'<td class="mono band"><span class="px">{escape(band)}</span></td>'
    if not low_pct:
        gap = escape(high_pct)
    else:
        gap = (
            f'<span class="{_pct_tone(high_pct)}">{escape(high_pct)}</span>/'
            f'<span class="{_pct_tone(low_pct)}">{escape(low_pct)}</span>'
        )
    return (
        '<td class="mono band">'
        f'<span class="px">{escape(band)}</span> '
        f'<span class="px-chg">{gap}</span></td>'
    )


def _pct_tone(text: str) -> str:
    if text.startswith("+"):
        return "chg-up"
    if text.startswith("-"):
        return "chg-down"
    return ""


def _badges(items: list[dict]) -> str:
    if not items:
        return '<span class="cell-empty">—</span>'
    return '<span class="badge-row">' + "".join(
        f"<span class=\"tag {escape(item['cls'])}\">{escape(item['t'])}</span>" for item in items
    ) + "</span>"


def _strikes(values: list[str]) -> str:
    if not values:
        return '<span class="cell-empty">—</span>'
    return " ".join(f"<span class=\"mono\">{escape(value)}</span>" for value in values)


def _dash_cell(value: str) -> str:
    if not value:
        return '<span class="cell-empty">—</span>'
    return f'<span class="mono">{escape(value)}</span>'


_CLIENT_JS = r"""
<script>
(function () {
  const css = `
  .ath-dock { position: fixed; right: 16px; bottom: 16px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; width: min(320px, calc(100vw - 32px)); }
  .ath-toasts, .ath-tasks { display: flex; flex-direction: column; gap: 8px; }
  .ath-toast, .ath-task { background: #10141a; border: 1px solid #333d49; color: #f2f4f7; padding: 11px 14px; font: 12.5px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; display: flex; gap: 10px; }
  .ath-task.error { border-color: #7f1d1d; }
  .ath-task .t-sub { color: #9aa5b1; font-size: 11.5px; margin-top: 2px; white-space: pre-wrap; }
  .ath-task .task-x { margin-left: auto; background: none; border: 0; color: #9aa5b1; cursor: pointer; }
  .ath-spin { width: 14px; height: 14px; margin-top: 2px; border: 2px solid #333d49; border-top-color: #6b96f2; border-radius: 50%; animation: ath-spin .7s linear infinite; flex: none; }
  @keyframes ath-spin { to { transform: rotate(360deg); } }
  .ath-toast .t-sub { color: #9aa5b1; font-size: 11.5px; margin-top: 2px; white-space: pre-wrap; }
  .ath-overlay { position: fixed; inset: 0; z-index: 9000; background: rgba(4,6,9,.66); display: none; }
  .ath-overlay.open { display: block; }
  .ath-panel { position: fixed; top: 0; right: 0; bottom: 0; z-index: 9001; width: min(460px, 94vw); background: #0d1117; border-left: 1px solid #333d49; color: #f2f4f7; transform: translateX(102%); transition: transform .24s; display: flex; flex-direction: column; }
  .ath-panel.wide { width: min(880px, 94vw); }
  .ath-panel.open { transform: none; }
  .ath-panel .p-head, .ath-modal .m-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid #232a33; }
  .ath-panel .p-body, .ath-modal .m-body { padding: 16px; overflow-y: auto; flex: 1; }
  .ath-modal { position: fixed; z-index: 9002; left: 50%; top: 18%; transform: translateX(-50%); width: min(420px, calc(100vw - 32px)); background: #10141a; border: 1px solid #333d49; color: #f2f4f7; }
  .ath-modal .m-foot { padding: 12px 16px; border-top: 1px solid #232a33; display: flex; gap: 8px; justify-content: flex-end; }
  .m-x { background: none; border: 1px solid #333d49; color: #9aa5b1; width: 26px; height: 26px; cursor: pointer; }
  `;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  function dock() {
    let root = document.querySelector(".ath-dock");
    if (!root) {
      root = document.createElement("div");
      root.className = "ath-dock";
      root.innerHTML = '<div class="ath-toasts"></div><div class="ath-tasks"></div>';
      document.body.appendChild(root);
    }
    return root;
  }
  function maskCenter(value) {
    const count = value.length;
    if (count < 2) return value;
    const hidden = Math.floor(count / 2);
    const front = Math.floor((count - hidden) / 2);
    const back = count - hidden - front;
    return value.slice(0, front) + "•".repeat(hidden) + value.slice(count - back);
  }
  function toast(title, sub, kind) {
    const root = dock().querySelector(".ath-toasts");
    const el = document.createElement("div");
    el.className = "ath-toast " + (kind || "info");
    el.innerHTML = "<div><b></b><div class='t-sub'></div></div>";
    el.querySelector("b").textContent = title;
    el.querySelector(".t-sub").textContent = sub || "";
    root.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }
  function remember(title, sub, kind) {
    sessionStorage.setItem("ath-flash", JSON.stringify({ title: title, sub: sub || "", kind: kind || "ok" }));
  }
  function showFlashes() {
    const raw = sessionStorage.getItem("ath-flash");
    if (raw) {
      sessionStorage.removeItem("ath-flash");
      try {
        const item = JSON.parse(raw);
        toast(item.title, item.sub, item.kind);
      } catch (err) {}
    }
    const flash = document.getElementById("ath-flash");
    if (flash) toast(flash.getAttribute("data-title") || "Saved", flash.getAttribute("data-sub") || "", flash.getAttribute("data-kind") || "ok");
  }
  let flushTimer = null;
  function closeAll() {
    if (flushTimer) {
      clearInterval(flushTimer);
      flushTimer = null;
    }
    document.querySelectorAll(".ath-panel, .ath-modal, .ath-overlay").forEach((node) => node.remove());
  }
  function overlay() {
    closeAll();
    const node = document.createElement("div");
    node.className = "ath-overlay open";
    node.addEventListener("click", closeAll);
    document.body.appendChild(node);
  }
  function panel(title, body, wide) {
    overlay();
    const node = document.createElement("div");
    node.className = wide ? "ath-panel wide" : "ath-panel";
    node.innerHTML = '<div class="p-head"><b></b><button class="m-x" type="button">×</button></div><div class="p-body"></div>';
    node.querySelector("b").textContent = title;
    node.querySelector(".p-body").appendChild(body);
    node.querySelector(".m-x").addEventListener("click", closeAll);
    document.body.appendChild(node);
    requestAnimationFrame(() => node.classList.add("open"));
  }
  function modal(title, body, footer) {
    overlay();
    const node = document.createElement("div");
    node.className = "ath-modal";
    node.innerHTML = '<div class="m-head"><b></b><button class="m-x" type="button">×</button></div><div class="m-body"></div><div class="m-foot"></div>';
    node.querySelector("b").textContent = title;
    node.querySelector(".m-body").appendChild(body);
    node.querySelector(".m-x").addEventListener("click", closeAll);
    footer.forEach((button) => node.querySelector(".m-foot").appendChild(button));
    document.body.appendChild(node);
  }
  function confirmBox(title, text, onYes, label) {
    overlay();
    const node = document.createElement("div");
    node.className = "ath-modal";
    node.innerHTML = '<div class="m-head"><b></b><button class="m-x" type="button">×</button></div><div class="m-body"></div><div class="m-foot"></div>';
    node.querySelector("b").textContent = title;
    node.querySelector(".m-body").textContent = text;
    node.querySelector(".m-x").addEventListener("click", closeAll);
    const cancel = document.createElement("button");
    cancel.className = "btn ghost";
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", closeAll);
    const ok = document.createElement("button");
    ok.className = "btn danger";
    ok.type = "button";
    ok.textContent = label || "Confirm";
    ok.addEventListener("click", () => { closeAll(); onYes(); });
    node.querySelector(".m-foot").append(cancel, ok);
    document.body.appendChild(node);
  }
  function linePath(values, w, h, floor, ceiling) {
    const min = floor == null ? Math.min.apply(null, values) : floor;
    const max = ceiling == null ? Math.max.apply(null, values) : ceiling;
    const span = max - min || 1;
    const step = (w - 12) / (values.length - 1);
    return values.map((v, i) => {
      const x = 6 + i * step;
      const y = h - 14 - ((v - min) / span) * (h - 28);
      return (i ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
  }
  function sparkline(el, values, color, fill) {
    if (!values || values.length < 2) return;
    const w = 560, h = Math.max(el.clientHeight || 26, 26);
    const raw = el.getAttribute("data-threshold");
    let floor = null, ceiling = null, guide = "";
    if (raw !== null && raw !== "") {
      const level = Number(raw);
      floor = Math.min.apply(null, values.concat([level]));
      ceiling = Math.max.apply(null, values.concat([level]));
      const span = ceiling - floor || 1;
      const y = h - 14 - ((level - floor) / span) * (h - 28);
      guide = '<line x1="6" y1="' + y.toFixed(1) + '" x2="' + (w - 6) + '" y2="' + y.toFixed(1) + '" stroke="#f7931a" stroke-width="1" stroke-dasharray="4 4"></line>';
    }
    const d = linePath(values, w, h, floor, ceiling);
    const area = fill ? '<path d="' + d + "L" + (w - 6) + "," + (h - 14) + "L6," + (h - 14) + 'Z" fill="' + color + '22"></path>' : "";
    el.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' + area + '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="1.6"></path>' + guide + "</svg>";
  }
  function bars(el, values) {
    if (!values || !values.length) return;
    const w = 560, h = el.clientHeight || 180;
    const max = Math.max.apply(null, values.map((v) => Math.abs(v))) || 1;
    const bw = (w - 8) / values.length;
    const mid = h / 2;
    const rects = values.map((v, i) => {
      const bh = v === 0 ? 3 : Math.abs(v) / max * (h / 2 - 8);
      const y = v >= 0 ? mid - bh : mid;
      const color = v > 0 ? "#16c784" : v < 0 ? "#ea3943" : "#333d49";
      return '<rect x="' + (4 + i * bw + 2) + '" y="' + y + '" width="' + Math.max(bw - 4, 2) + '" height="' + Math.max(bh, 1) + '" fill="' + color + '"></rect>';
    }).join("");
    el.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' + rects + "</svg>";
  }
  const dataNode = document.getElementById("cockpit-data");
  const data = dataNode ? JSON.parse(dataNode.textContent) : { nlv: [], months: [], pnl: [] };
  const nlv = document.getElementById("chartNlv");
  if (nlv) sparkline(nlv, data.nlv, "#6b96f2", true);
  const pnl = document.getElementById("chartPnl");
  if (pnl) bars(pnl, data.pnl);
  document.querySelectorAll(".ivr-spark").forEach((el) => {
    sparkline(el, JSON.parse(el.getAttribute("data-iv") || "[]"), "#22d3ee", false);
  });
  document.querySelectorAll("[data-bars]").forEach((el) => {
    bars(el, JSON.parse(el.getAttribute("data-bars") || "[]"));
  });
  document.addEventListener("submit", (event) => {
    const text = event.target.getAttribute("data-confirm");
    if (text && !window.confirm(text)) event.preventDefault();
  });
  function paintPoolCount(count) {
    document.querySelectorAll("[data-pool-count]").forEach((node) => {
      node.textContent = count + " symbols";
    });
    document.querySelectorAll(".top-end .chip").forEach((node) => {
      node.childNodes.forEach((child) => {
        if (child.nodeType === 3 && child.textContent.indexOf("IN POOL") !== -1) {
          child.textContent = count + " IN POOL";
        }
      });
    });
  }
  function placePoolRow(symbol, html) {
    const current = document.querySelector('#pool-table tr[data-symbol="' + CSS.escape(symbol) + '"]');
    const wrap = document.createElement("tbody");
    wrap.innerHTML = html.trim();
    const next = wrap.querySelector("tr");
    if (!next) return;
    if (current) current.replaceWith(next);
    else {
      const body = document.querySelector("#pool-table tbody");
      const empty = document.getElementById("pool-empty");
      if (!body) return;
      if (empty) body.insertBefore(next, empty);
      else body.appendChild(next);
    }
    document.dispatchEvent(new Event("ath-feed"));
  }
  function lockPoolRow(row, locked) {
    if (!row) return;
    row.classList.toggle("pool-busy", locked);
    row.setAttribute("aria-busy", locked ? "true" : "false");
    row.querySelectorAll("button, select, input").forEach((node) => { node.disabled = locked; });
  }
  function postUniverse(form) {
    const body = new URLSearchParams(new FormData(form));
    body.set("partial", "row");
    const symbol = (body.get("symbol") || "").toUpperCase();
    const select = form.querySelector("select.intent");
    const row = form.closest("tr");
    lockPoolRow(row, true);
    fetch("/universe", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body
    }).then((res) => res.json().then((data) => ({ ok: res.ok, data: data }))).then((result) => {
      const data = result.data || {};
      if (!result.ok || !data.ok) {
        if (select && select.dataset.prev != null) select.value = select.dataset.prev;
        toast("Not saved", data.message || "Try again", "err");
        return;
      }
      if (data.removed) {
        const row = document.querySelector('#pool-table tr[data-symbol="' + CSS.escape(symbol) + '"]');
        if (row) row.remove();
        document.dispatchEvent(new Event("ath-feed"));
      } else if (data.html) {
        placePoolRow(symbol, data.html);
      }
      if (data.count != null) paintPoolCount(data.count);
      if (form.closest(".ath-panel, .ath-modal")) closeAll();
      toast("Saved", data.message || "", "ok");
    }).catch(() => {
      if (select && select.dataset.prev != null) select.value = select.dataset.prev;
      toast("Not saved", "The console did not answer.", "err");
    }).finally(() => {
      if (row && row.isConnected) lockPoolRow(row, false);
    });
  }
  function refreshPoolRow(symbol) {
    if (!symbol) return;
    fetch("/universe?partial=symbol&symbol=" + encodeURIComponent(symbol))
      .then((res) => res.json())
      .then((data) => {
        if (data.html) placePoolRow(symbol, data.html);
        if (data.count != null) paintPoolCount(data.count);
      }).catch(() => {});
  }
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form || form.tagName !== "FORM" || event.defaultPrevented) return;
    if ((form.getAttribute("action") || "") !== "/universe") return;
    event.preventDefault();
    postUniverse(form);
  });
  document.addEventListener("focusin", (event) => {
    const node = event.target.closest && event.target.closest("select.intent");
    if (node) node.dataset.prev = node.value;
  });
  const settingsForm = document.getElementById("settings-form");
  if (settingsForm) {
    let saving = false;
    let again = false;
    function saveSettings() {
      if (saving) {
        again = true;
        return;
      }
      saving = true;
      const credential = settingsForm.querySelector("[name=credential_token]");
      const body = new URLSearchParams(new FormData(settingsForm));
      if (credential && credential.dataset.edited !== "1") body.delete("credential_token");
      fetch(settingsForm.getAttribute("action") || "/settings", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body
      }).then((res) => res.text().then((html) => ({ ok: res.ok, html }))).then((result) => {
        const flash = readFlash(result.html);
        const sub = flash ? flash.sub : "";
        if (!result.ok) {
          if (sub !== "no change") toast("Not saved", sub, "err");
          return;
        }
        settingsForm.querySelectorAll("input[type=password]").forEach((input) => {
          if (!input.value || input.readOnly) return;
          const row = input.closest("label") && input.closest("label").previousElementSibling;
          const tag = row && row.querySelector(".tag");
          if (tag) {
            tag.textContent = "SET";
            tag.className = "tag green";
          }
          input.value = "";
        });
        if (credential && credential.dataset.edited === "1") {
          const provider = settingsForm.querySelector("[name=ai_provider]");
          const masksNode = document.getElementById("credential-masks");
          const masks = masksNode ? JSON.parse(masksNode.textContent) : {};
          const typed = credential.value.trim();
          const masked = typed && !typed.includes("•") ? maskCenter(typed) : typed;
          if (provider) masks[provider.value] = masked;
          if (masksNode) masksNode.textContent = JSON.stringify(masks);
          credential.value = masked;
          credential.dataset.edited = "";
        }
        toast("Saved", sub, "ok");
      }).catch(() => {
        toast("Not saved", "The console did not answer.", "err");
      }).finally(() => {
        saving = false;
        if (again) {
          again = false;
          saveSettings();
        }
      });
    }
    function syncAiProvider() {
      const provider = settingsForm.querySelector("[name=ai_provider]");
      const model = settingsForm.querySelector("[name=model]");
      const catalog = document.getElementById("ai-models");
      const block = document.getElementById("credential-block");
      const help = document.getElementById("credential-help");
      const masksNode = document.getElementById("credential-masks");
      const credential = settingsForm.querySelector("[name=credential_token]");
      if (!provider || !model || !catalog) return;
      const lists = JSON.parse(catalog.textContent);
      const names = lists[provider.value] || [];
      const current = model.value;
      model.replaceChildren();
      names.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        if (name === current) option.selected = true;
        model.appendChild(option);
      });
      if (names.length && !names.includes(model.value)) model.value = names[0];
      if (block) block.hidden = provider.value === "DSH";
      const envs = {
        DEEPSEEK: "DEEPSEEK_TOKEN",
        OPENROUTER: "OPENROUTER_API_KEY",
        CLAUDE: "ANTHROPIC_API_KEY",
        KIMI: "MOONSHOT_API_KEY"
      };
      if (help) {
        const env = envs[provider.value];
        help.textContent = env
          ? "Clear the field to remove the token. With nothing saved, Athena uses " + env + "."
          : "";
      }
      if (credential && masksNode) {
        const masks = JSON.parse(masksNode.textContent);
        credential.value = masks[provider.value] || "";
        credential.dataset.edited = "";
      }
    }
    const credentialInput = settingsForm.querySelector("[name=credential_token]");
    if (credentialInput) {
      credentialInput.addEventListener("input", () => {
        credentialInput.dataset.edited = "1";
      });
    }
    settingsForm.addEventListener("change", (event) => {
      const target = event.target;
      if (!target || !target.name) return;
      if (target.name === "ai_provider") syncAiProvider();
      if (target.type === "checkbox") {
        const tag = target.parentElement && target.parentElement.querySelector(".tag");
        if (tag) {
          tag.textContent = target.checked ? "ON" : "OFF";
          tag.className = target.checked ? "tag green" : "tag";
        }
      }
      saveSettings();
    });
    settingsForm.addEventListener("submit", (event) => {
      if (event.submitter && event.submitter.hasAttribute("data-explicit")) return;
      event.preventDefault();
      saveSettings();
    });
    const simFind = document.getElementById("sim-find");
    const simMenu = settingsForm.querySelector(".sim-menu");
    const simPicks = document.getElementById("sim-picks");
    const simUniverseNode = document.getElementById("sim-universe");
    if (simFind && simMenu && simPicks && simUniverseNode) {
      let universe = [];
      try { universe = JSON.parse(simUniverseNode.textContent || "[]"); } catch (err) { universe = []; }
      function pickedSymbols() {
        return Array.from(simPicks.querySelectorAll("input[name=sim_symbol]")).map((input) => input.value);
      }
      function paintEmpty() {
        let note = simPicks.querySelector(".sim-empty");
        if (pickedSymbols().length) {
          if (note) note.remove();
          return;
        }
        if (!note) {
          note = document.createElement("p");
          note.className = "sim-empty";
          note.textContent = "None selected.";
          simPicks.appendChild(note);
        }
      }
      function closeMenu() {
        simMenu.hidden = true;
        simMenu.innerHTML = "";
      }
      function paintMenu() {
        const query = simFind.value.trim().toUpperCase();
        const taken = new Set(pickedSymbols());
        const matches = universe.filter((item) => {
          if (taken.has(item.symbol)) return false;
          if (!query) return true;
          return item.symbol.indexOf(query) !== -1 || (item.name || "").toUpperCase().indexOf(query) !== -1;
        }).slice(0, 8);
        simMenu.innerHTML = "";
        if (!matches.length) {
          simMenu.hidden = true;
          return;
        }
        matches.forEach((item) => {
          const button = document.createElement("button");
          button.type = "button";
          const label = document.createElement("b");
          label.textContent = item.symbol;
          button.appendChild(label);
          if (item.name) {
            const name = document.createElement("span");
            name.className = "name";
            name.textContent = item.name;
            button.appendChild(name);
          }
          button.addEventListener("mousedown", (event) => {
            event.preventDefault();
            addSymbol(item.symbol);
          });
          simMenu.appendChild(button);
        });
        simMenu.hidden = false;
      }
      function addSymbol(symbol) {
        if (pickedSymbols().indexOf(symbol) !== -1) return;
        const chip = document.createElement("span");
        chip.className = "sim-pick";
        chip.innerHTML = "<b></b><button class=\"sim-x\" type=\"button\"><svg viewBox=\"0 0 12 12\" aria-hidden=\"true\"><path d=\"M3 3l6 6M9 3L3 9\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.4\"/></svg></button><input type=\"hidden\" name=\"sim_symbol\">";
        chip.querySelector("b").textContent = symbol;
        chip.querySelector("button").setAttribute("aria-label", "Remove " + symbol);
        chip.querySelector("input").value = symbol;
        const note = simPicks.querySelector(".sim-empty");
        if (note) note.remove();
        simPicks.appendChild(chip);
        simFind.value = "";
        closeMenu();
        saveSettings();
      }
      simFind.addEventListener("input", paintMenu);
      simFind.addEventListener("focus", paintMenu);
      simFind.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeMenu();
        if (event.key === "Enter") {
          event.preventDefault();
          const first = simMenu.querySelector("button");
          if (first) first.dispatchEvent(new MouseEvent("mousedown"));
        }
      });
      simPicks.addEventListener("click", (event) => {
        const button = event.target.closest(".sim-x");
        if (!button) return;
        const chip = button.closest(".sim-pick");
        if (chip) chip.remove();
        paintEmpty();
        closeMenu();
        saveSettings();
      });
      document.addEventListener("click", (event) => {
        if (!event.target.closest(".sim-find")) closeMenu();
      });
    }
  }
  const riskForm = document.getElementById("risk-form");
  if (riskForm) {
    let saving = false;
    let again = false;
    function saveRisk() {
      if (saving) {
        again = true;
        return;
      }
      saving = true;
      const body = new URLSearchParams(new FormData(riskForm));
      fetch(riskForm.getAttribute("action") || "/risk", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body
      }).then((res) => res.text().then((html) => ({ ok: res.ok, html }))).then((result) => {
        const flash = readFlash(result.html);
        const sub = flash ? flash.sub : "";
        if (!result.ok) {
          if (sub !== "no change") toast("Not saved", sub, "err");
          return;
        }
        const version = /version (\d+)/.exec(sub);
        if (version) {
          document.querySelectorAll(".chips .chip").forEach((chip) => {
            const label = chip.childNodes[chip.childNodes.length - 1];
            if (label && /^V\d+$/.test(label.textContent || "")) label.textContent = "V" + version[1];
          });
        }
        const picked = document.querySelector("[data-limits].on .name");
        if (picked) {
          const name = picked.textContent.trim().toUpperCase();
          document.querySelectorAll(".chips .chip").forEach((chip) => {
            const label = chip.childNodes[chip.childNodes.length - 1];
            if (label && /^(DEFENSIVE|STANDARD|INCOME|HARVEST|AGGRESSIVE|TEST|PAPER|CUSTOM)$/.test(label.textContent || "")) label.textContent = name;
          });
        }
        toast("Saved", sub, "ok");
      }).catch(() => {
        toast("Not saved", "The console did not answer.", "err");
      }).finally(() => {
        saving = false;
        if (again) {
          again = false;
          saveRisk();
        }
      });
    }
    riskForm.addEventListener("focusin", (event) => {
      const input = event.target;
      if (input && input.dataset && input.dataset.preview) input.dataset.was = input.value;
    });
    riskForm.addEventListener("change", (event) => {
      if (!event.target || !event.target.name) return;
      if (event.target.dataset && event.target.dataset.preview) {
        const input = event.target;
        const previous = input.dataset.was != null ? input.dataset.was : input.defaultValue;
        if (input.value === previous) return;
        const body = new URLSearchParams(new FormData(riskForm));
        body.set("action", "preview");
        body.set("preview_field", input.name);
        fetch(riskForm.getAttribute("action") || "/risk", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body
        }).then((res) => res.json()).then((data) => {
          if (!data.ok) {
            input.value = previous;
            toast("Not saved", data.text || "Preview failed", "err");
            return;
          }
          if (window.confirm(data.text)) {
            input.dataset.was = input.value;
            saveRisk();
          } else {
            input.value = previous;
          }
        }).catch(() => {
          input.value = previous;
          toast("Not saved", "The console did not answer.", "err");
        });
        return;
      }
      saveRisk();
    });
    riskForm.addEventListener("submit", (event) => {
      event.preventDefault();
      saveRisk();
    });
  }
  function applyGrade(btn) {
    const parts = (btn.getAttribute("data-limits") || "").split(",");
    ["max_symbol", "cash", "halt", "contracts", "min_rv", "min_dte", "position", "max_orders", "margin", "blackout"].forEach((name, index) => {
      const input = document.querySelector("#risk-form [name=" + name + "]");
      if (input) input.value = parts[index];
    });
    const extra = btn.getAttribute("data-grade");
    if (extra) {
      const knobs = JSON.parse(extra);
      Object.keys(knobs).forEach((name) => {
        const input = document.querySelector("#risk-form [name=" + name + "]");
        if (input) input.value = knobs[name];
      });
    }
    document.querySelectorAll("[data-limits]").forEach((node) => node.classList.remove("on"));
    btn.classList.add("on");
    const first = document.querySelector("#risk-form [name=max_symbol]");
    if (first) first.dispatchEvent(new Event("change", { bubbles: true }));
  }
  document.querySelectorAll("[data-limits]").forEach((btn) => {
    btn.addEventListener("click", () => applyGrade(btn));
  });
  document.querySelectorAll("[data-reset-grade]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const picked = document.querySelector("[data-limits].on");
      if (!picked) {
        toast("No grade", "Select a grade first.", "err");
        return;
      }
      applyGrade(picked);
    });
  });
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body }).then((res) => res.json());
  }
  function watchTask(id) {
    const timer = setInterval(() => {
      fetch("/api/tasks").then((res) => res.json()).then((body) => {
        const task = (body.tasks || []).find((item) => item.id === id);
        if (!task || task.status === "running") return;
        clearInterval(timer);
        if (task.status === "done") {
          remember(task.title || "Done", task.detail || "", "ok");
          location.reload();
          return;
        }
        toast(task.title || "Failed", task.detail || "", "err");
      }).catch(() => {});
    }, 1000);
  }
  const opsLabels = {
    backup: ["Backing up…", "Backup now"],
    halt: ["Halting…", "Halt trading"],
    resume: ["Resuming…", "Resume"]
  };
  function opsButtons(cmd) {
    return [...document.querySelectorAll("[data-cmd]")].filter((button) => button.getAttribute("data-cmd") === cmd);
  }
  function setOpsBusy(cmd, busy) {
    const pair = opsLabels[cmd];
    opsButtons(cmd).forEach((button) => {
      if (button.classList.contains("icon-btn")) {
        button.disabled = busy;
        button.classList.toggle("busy", busy);
        return;
      }
      if (busy) {
        button.disabled = true;
        if (pair) button.textContent = pair[0];
      } else if (!pair || button.textContent === pair[0]) {
        button.disabled = false;
        if (pair) button.textContent = pair[1];
      }
    });
  }
  function postCmd(cmd) {
    if (opsButtons(cmd).some((button) => button.disabled)) return;
    setOpsBusy(cmd, true);
    if (cmd === "health") {
      post("/api/ops", "command=" + encodeURIComponent(cmd)).then((result) => {
        setOpsBusy(cmd, false);
        if (!result.ok) {
          toast("Not done", result.message || "", "err");
          return;
        }
        toast("Health", result.message || "", "ok");
        if (result.report) paintHealth(result.report);
      }).catch(() => {
        setOpsBusy(cmd, false);
        toast("Not done", "The request did not finish.", "err");
      });
      return;
    }
    const background = ["backup", "halt", "resume"];
    if (background.indexOf(cmd) !== -1) {
      post("/api/ops", "command=" + encodeURIComponent(cmd)).then((result) => {
        if (!result.ok) {
          setOpsBusy(cmd, false);
          toast("Not done", result.message || "", "err");
        }
      }).catch(() => {
        setOpsBusy(cmd, false);
        toast("Not done", "The request did not finish.", "err");
      });
      return;
    }
    post("/api/ops", "command=" + encodeURIComponent(cmd)).then((result) => {
      setOpsBusy(cmd, false);
      const title = result.ok ? "Done" : "Not done";
      const kind = result.ok ? "ok" : "err";
      toast(title, result.message || "", kind);
    }).catch(() => {
      setOpsBusy(cmd, false);
      toast("Not done", "The request did not finish.", "err");
    });
  }
  function slotRow(item) {
    const row = document.createElement("div");
    row.className = "slot-row";
    row.innerHTML =
      '<select name="owner"><option>Athena</option><option>Mobile</option></select>' +
      '<input type="time" name="start" step="60">' +
      '<span>to</span>' +
      '<input type="time" name="end" step="60">' +
      '<button class="btn ghost" type="button" data-cmd="remove-slot" aria-label="Remove window">×</button>';
    row.querySelector("[name=owner]").value = item.owner || "Athena";
    row.querySelector("[name=start]").value = item.start || "";
    row.querySelector("[name=end]").value = item.end || "";
    row.querySelectorAll("select, input").forEach((field) => {
      field.addEventListener("change", () => refreshSlotPreview(row.closest(".ath-panel")));
    });
    return row;
  }
  function packSlots(root) {
    const rows = [...root.querySelectorAll(".slot-row")].map((row) => ({
      owner: row.querySelector("[name=owner]").value,
      start: row.querySelector("[name=start]").value,
      end: row.querySelector("[name=end]").value
    }));
    if (!rows.length) return { error: "Add one Athena window and one Mobile window." };
    const minutes = (value) => {
      const parts = value.split(":");
      return Number(parts[0]) * 60 + Number(parts[1]);
    };
    const pieces = [];
    for (const row of rows) {
      if (!row.start || !row.end) return { error: "Each window needs a start and an end." };
      if (row.start === row.end) return { error: row.owner + " " + row.start + " needs a different end." };
      const start = minutes(row.start);
      const end = minutes(row.end);
      const spans = start < end ? [[start, end]] : [[start, 1440], [0, end]];
      spans.forEach((span) => pieces.push({ start: span[0], end: span[1], owner: row.owner }));
    }
    pieces.sort((a, b) => a.start - b.start || a.end - b.end);
    for (let i = 1; i < pieces.length; i++) {
      if (pieces[i].start < pieces[i - 1].end) {
        return { error: pieces[i - 1].owner + " and " + pieces[i].owner + " overlap. Windows may meet, not cross." };
      }
    }
    const ordered = rows.slice().sort((a, b) => a.start.localeCompare(b.start));
    if (!rows.some((row) => row.owner === "Athena") || !rows.some((row) => row.owner === "Mobile")) {
      return { error: "Keep one Athena window and one Mobile window." };
    }
    const pack = (owner) => "HKT " + rows.filter((row) => row.owner === owner).map((row) => row.start + "-" + row.end).join(", ");
    const days = [];
    ordered.forEach((row, index) => {
      if (index && ordered[index - 1].end < row.start) {
        days.push({ window: ordered[index - 1].end + "–" + row.start, owner: "Idle" });
      }
      days.push({ window: row.start + "–" + row.end, owner: row.owner });
    });
    return { athena: pack("Athena"), mobile: pack("Mobile"), days: days };
  }
  function refreshSlotPreview(root) {
    if (!root) return;
    const node = root.querySelector(".slot-preview");
    if (!node) return;
    const packed = packSlots(root);
    if (packed.error) {
      node.innerHTML = '<p class="note">' + packed.error + "</p>";
      return;
    }
    node.innerHTML = '<table class="data"><thead><tr><th>Window (HKT)</th><th>Owner</th></tr></thead><tbody>' +
      packed.days.map((row) => "<tr><td class=\"mono\">" + row.window + "</td><td>" + row.owner + "</td></tr>").join("") +
      "</tbody></table>";
  }
  let feedGen = 0;
  let feedBusy = false;
  function pullFeed(box, replace) {
    if (!box) return Promise.resolve();
    if (!replace && (feedBusy || box.getAttribute("data-more") !== "1")) return Promise.resolve();
    const gen = ++feedGen;
    feedBusy = true;
    const offset = replace ? 0 : Number(box.getAttribute("data-offset") || "0");
    const path = box.getAttribute("data-feed") || "";
    const extra = box.getAttribute("data-query") || "";
    const join = path.indexOf("?") >= 0 ? "&" : "?";
    return fetch(path + join + "offset=" + offset + "&partial=rows" + (extra ? "&" + extra : ""))
      .then((res) => res.json())
      .then((payload) => {
        if (gen !== feedGen) return;
        const body = box.querySelector("tbody");
        if (!body) return;
        const html = payload.html || "";
        if (replace) {
          body.innerHTML = html;
          box.scrollTop = 0;
          if (box.id === "orders-feed") box.removeAttribute("data-tail");
        } else if (html) {
          body.insertAdjacentHTML("beforeend", html);
          if (box.id === "orders-feed") box.setAttribute("data-tail", "1");
        }
        box.setAttribute("data-offset", String(payload.next || 0));
        box.setAttribute("data-more", html && payload.more ? "1" : "0");
      })
      .catch(() => {})
      .finally(() => {
        if (gen !== feedGen) return;
        feedBusy = false;
        document.dispatchEvent(new Event("ath-feed"));
      });
  }
  document.addEventListener("scroll", () => {
    document.querySelectorAll(".help-pop").forEach((node) => node.remove());
  }, true);
  const viewPick = document.querySelector("[data-view-pick]");
  if (viewPick) {
    viewPick.addEventListener("change", () => {
      const accountId = viewPick.value;
      if (!accountId) return;
      document.cookie = "athena_view=" + encodeURIComponent(accountId) + "; Path=/; Max-Age=31536000; SameSite=Lax";
      location.reload();
    });
  }
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".help-q") && !event.target.closest(".help-pop")) {
      document.querySelectorAll(".help-pop").forEach((node) => node.remove());
    }
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      navigator.clipboard.writeText(copy.getAttribute("data-copy") || "");
      toast("Copied", copy.getAttribute("data-copy"), "ok");
      return;
    }
    const btn = event.target.closest("[data-cmd]");
    if (!btn) return;
    const cmd = btn.getAttribute("data-cmd");
    if (cmd === "reveal-key") {
      const root = btn.closest(".key-row") || btn.parentElement;
      const node = root.querySelector("input, [data-secret]");
      if (!node) return;
      if (node.tagName === "INPUT") {
        node.type = node.type === "password" ? "text" : "password";
      } else {
        const hidden = node.getAttribute("data-hidden") !== "0";
        const secret = node.getAttribute("data-secret") || "";
        node.textContent = hidden ? secret : (node.getAttribute("data-mask") || maskCenter(secret));
        node.setAttribute("data-hidden", hidden ? "0" : "1");
      }
      return;
    }
    if (cmd === "refresh-list") {
      const box = document.querySelector("[data-feed]");
      if (!box) return;
      btn.classList.add("busy");
      pullFeed(box, true).finally(() => btn.classList.remove("busy"));
      return;
    }
    if (cmd === "simulate") {
      const body = document.createElement("div");
      body.innerHTML = '<div class="preset-row"></div><div class="sim-date"><div class="k">Start date</div><div class="sim-date-face"><span id="sim-label"></span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="16" rx="1"/><path d="M3 10h18M8 3v4M16 3v4"/></svg></div><input type="date" id="sim-start"></div>';
      const presets = body.querySelector(".preset-row");
      const start = body.querySelector("#sim-start");
      const label = body.querySelector("#sim-label");
      const monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
      let months = "1";
      const localDate = (date) => {
        const month = String(date.getMonth() + 1).padStart(2, "0");
        const day = String(date.getDate()).padStart(2, "0");
        return date.getFullYear() + "-" + month + "-" + day;
      };
      const paintDate = () => {
        const parts = (start.value || "").split("-");
        if (parts.length !== 3) {
          label.textContent = "Choose a date";
          return;
        }
        label.textContent = Number(parts[2]) + " " + monthNames[Number(parts[1]) - 1] + " " + parts[0];
      };
      const applyMonths = (count, choice) => {
        months = String(count);
        presets.querySelectorAll("button").forEach((node) => node.classList.remove("primary"));
        choice.classList.add("primary");
        const date = new Date();
        date.setMonth(date.getMonth() - count);
        start.value = localDate(date);
        paintDate();
      };
      start.max = localDate(new Date());
      start.addEventListener("change", paintDate);
      start.addEventListener("input", paintDate);
      ["1 month", "2 months", "3 months"].forEach((label, index) => {
        const choice = document.createElement("button");
        choice.className = "btn";
        choice.type = "button";
        choice.textContent = label;
        choice.addEventListener("click", () => applyMonths(index + 1, choice));
        presets.appendChild(choice);
        if (index === 0) applyMonths(1, choice);
      });
      const go = document.createElement("button");
      go.className = "btn primary";
      go.type = "button";
      go.textContent = "Start";
      go.addEventListener("click", () => {
        if (!start.value) {
          toast("Start date", "Choose a date.", "err");
          return;
        }
        go.disabled = true;
        post("/api/tasks", "action=simulate&months=" + months + "&start=" + encodeURIComponent(start.value))
          .then((result) => {
            if (!result.ok) throw new Error(result.message || "Could not start.");
            closeAll();
            const task = result.task || {};
            toast("Simulation", task.detail || "Downloading history", "ok");
            if (task.id) watchTask(task.id);
          })
          .catch((err) => {
            go.disabled = false;
            toast("Simulation", err.message || "Could not start.", "err");
          });
      });
      const cancel = document.createElement("button");
      cancel.className = "btn ghost";
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeAll);
      modal("Simulate", body, [cancel, go]);
      return;
    }
    if (cmd === "sim-stop") {
      post("/api/tasks", "action=sim-stop").then((result) => {
        if (!result.ok) throw new Error(result.message || "Could not stop.");
        remember("Simulation", result.message || "Stopped", "ok");
        location.reload();
      }).catch((err) => toast("Simulation", err.message || "Could not stop.", "err"));
      return;
    }
    if (cmd === "sim-halt" || cmd === "sim-resume") {
      const action = cmd === "sim-halt" ? "sim-halt" : "sim-resume";
      post("/api/tasks", "action=" + action).then((result) => {
        if (!result.ok) throw new Error(result.message || "Could not update the simulation.");
        remember("Simulation", result.message || "Updated", "ok");
        location.reload();
      }).catch((err) => toast("Simulation", err.message || "Could not update the simulation.", "err"));
      return;
    }
    if (cmd === "sim-reset") {
      confirmBox(
        "Reset simulation",
        "This deletes the replay and its bars, and clears simulated drafts, orders, fills, positions, and audit rows. Real rows stay.",
        () => {
          post("/api/tasks", "action=sim-reset").then((result) => {
            if (!result.ok) throw new Error(result.message || "Could not reset.");
            remember("Simulation", result.message || "Cleared", "ok");
            location.reload();
          }).catch((err) => toast("Simulation", err.message || "Could not reset.", "err"));
        },
        "Reset"
      );
      return;
    }
    if (cmd === "liquidate") {
      confirmBox("Liquidate All", "Delete simulated positions and send one limit close for each real position.", () => {
        post("/api/tasks", "action=liquidate").then((result) => {
          if (!result.ok) throw new Error(result.message || "Could not close.");
          const task = result.task || {};
          toast("Liquidate", task.detail || "Working", "ok");
          if (task.id) watchTask(task.id);
        }).catch((err) => toast("Liquidate", err.message || "Could not close.", "err"));
      }, "Liquidate All");
      return;
    }
    if (cmd === "flush") {
      const body = document.createElement("div");
      body.innerHTML = '<p>This wipes drafts, orders, fills, positions, and audit for this account. Settings and the watchlist stay.</p><label>Type I CONFIRM<input class="input" id="flush-phrase" autocomplete="off"></label>';
      const phrase = body.querySelector("#flush-phrase");
      const go = document.createElement("button");
      go.className = "btn danger";
      go.type = "button";
      go.textContent = "Flush All";
      const cancel = document.createElement("button");
      cancel.className = "btn ghost";
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeAll);
      go.addEventListener("click", () => {
        if (flushTimer) return;
        if (phrase.value !== "I CONFIRM") {
          toast("Flush", "Type I CONFIRM.", "err");
          return;
        }
        let left = 30;
        phrase.disabled = true;
        go.textContent = left + "s";
        flushTimer = setInterval(() => {
          left -= 1;
          if (left > 0) {
            go.textContent = left + "s";
            return;
          }
          clearInterval(flushTimer);
          flushTimer = null;
          post("/api/tasks", "action=flush&phrase=" + encodeURIComponent("I CONFIRM"))
            .then((result) => {
              if (!result.ok) throw new Error(result.message || "Could not flush.");
              closeAll();
              remember("Flush", result.message || "Cleared", "ok");
              location.reload();
            })
            .catch((err) => {
              closeAll();
              toast("Flush", err.message || "Could not flush.", "err");
            });
        }, 1000);
      });
      modal("Flush All", body, [cancel, go]);
      return;
    }
    if (cmd === "cycle-fold") {
      const strip = document.getElementById("cycleStrip");
      if (!strip) return;
      const compact = !strip.classList.contains("compact");
      strip.classList.toggle("compact", compact);
      sessionStorage.setItem("ath-cycle-fold", compact ? "1" : "0");
      btn.setAttribute("aria-label", compact ? "Expand cycle" : "Collapse cycle");
      return;
    }
    if (cmd === "position-row") {
      const wrap = document.createElement("div");
      [
        ["Symbol", "data-symbol"],
        ["Type", "data-kind"],
        ["Quantity", "data-qty"],
        ["Average", "data-avg"],
        ["Mark", "data-mark"],
        ["Value", "data-value"],
        ["Open P&L", "data-pnl"],
        ["Realized P&L", "data-realized"],
        ["Expiry", "data-expiry"],
        ["DTE", "data-dte"],
        ["Strike", "data-strike"],
        ["Currency", "data-ccy"],
        ["Multiplier", "data-mult"],
        ["Synced", "data-synced"]
      ].forEach((pair) => {
        const row = document.createElement("div");
        row.className = "field";
        const key = document.createElement("div");
        key.className = "k";
        key.textContent = pair[0];
        const value = document.createElement("div");
        value.className = "v";
        const raw = btn.getAttribute(pair[1]) || "";
        value.textContent = raw === "" || raw === "None" ? "—" : raw;
        row.appendChild(key);
        row.appendChild(value);
        wrap.appendChild(row);
      });
      const close = document.createElement("button");
      close.className = "btn ghost";
      close.type = "button";
      close.textContent = "Close";
      close.addEventListener("click", closeAll);
      modal(btn.getAttribute("data-symbol") || "Position", wrap, [close]);
      return;
    }
    if (cmd === "history-row") {
      const wrap = document.createElement("div");
      [["Phase", "data-phase"], ["Status", "data-status"], ["P&L", "data-pnl"], ["Approved by", "data-by"], ["Reason", "data-reason"]].forEach((pair) => {
        const row = document.createElement("div");
        row.className = "field";
        const key = document.createElement("div");
        key.className = "k";
        key.textContent = pair[0];
        const value = document.createElement("div");
        value.className = "v";
        value.textContent = btn.getAttribute(pair[1]) || "—";
        row.appendChild(key);
        row.appendChild(value);
        wrap.appendChild(row);
      });
      const close = document.createElement("button");
      close.className = "btn ghost";
      close.type = "button";
      close.textContent = "Close";
      close.addEventListener("click", closeAll);
      modal(btn.getAttribute("data-title") || "History", wrap, [close]);
      return;
    }
    if (cmd === "privilege") {
      const wrap = document.createElement("div");
      wrap.className = "card-lead";
      wrap.style.margin = "0";
      wrap.textContent = btn.getAttribute("data-help") || "";
      const close = document.createElement("button");
      close.className = "btn ghost";
      close.type = "button";
      close.textContent = "Close";
      close.addEventListener("click", closeAll);
      modal(btn.getAttribute("data-title") || "Privilege", wrap, [close]);
      return;
    }
    if (cmd === "fold-agent") {
      const card = btn.closest(".agent");
      if (!card) return;
      const closed = card.classList.toggle("collapsed");
      btn.setAttribute("aria-expanded", closed ? "false" : "true");
      btn.setAttribute("aria-label", (closed ? "Expand " : "Collapse ") + card.querySelector("h2 span").textContent);
      return;
    }
    function jsonBlock(title, value) {
      const block = document.createElement("div");
      block.className = "json-block";
      const label = document.createElement("div");
      label.className = "k";
      label.textContent = title;
      const pre = document.createElement("pre");
      pre.className = "json";
      pre.textContent = JSON.stringify(value || {}, null, 2);
      block.appendChild(label);
      block.appendChild(pre);
      return block;
    }
    if (cmd === "audit-row") {
      let row = {};
      try { row = JSON.parse(btn.getAttribute("data-json") || "{}"); } catch (err) { return; }
      const body = document.createElement("div");
      if (row.kind === "decision") {
        const meta = document.createElement("div");
        meta.className = "meta";
        [
          ["Time", row.time], ["Source", row.source], ["Symbol", row.symbol],
          ["Outcome", row.status], ["Summary", row.result], ["Phase", row.phase],
          ["Intent", row.intent], ["Long", row.long], ["Flags", row.flags], ["Tags", row.tags],
          ["Book", row.simulated ? "Simulation" : ""]
        ].forEach((pair) => {
          if (!pair[1]) return;
          const k = document.createElement("span");
          k.textContent = pair[0];
          const v = document.createElement("div");
          if (pair[0] === "Summary" && row.badges && row.badges.length) {
            v.className = "badges";
            row.badges.forEach((badge) => {
              if (!badge.text) return;
              const tag = document.createElement("span");
              tag.className = badge.kind ? "tag " + badge.kind : "tag";
              tag.textContent = badge.text;
              v.appendChild(tag);
            });
          } else {
            v.textContent = pair[1];
          }
          meta.appendChild(k);
          meta.appendChild(v);
        });
        const trail = document.createElement("div");
        trail.className = "trail";
        (row.steps || []).forEach((step) => {
          const line = document.createElement("div");
          const name = step.tactic ? step.tactic + " " : "";
          const id = step.draft_id ? " " + step.draft_id : "";
          line.textContent = name + (step.outcome || "") + ". " + (step.detail || "") + id;
          trail.appendChild(line);
        });
        body.appendChild(meta);
        body.appendChild(trail);
        if (row.thinking_text) {
          const block = document.createElement("div");
          block.className = "json-block";
          block.innerHTML = '<div class="k">Thinking</div>';
          const pre = document.createElement("pre");
          pre.className = "json";
          pre.textContent = row.thinking_text;
          block.appendChild(pre);
          body.appendChild(block);
        }
        panel("Decision " + (row.symbol || ""), body, true);
        return;
      }
      const meta = document.createElement("div");
      meta.className = "meta";
      [
        ["Time", row.time], ["Agent", row.agent], ["Symbol", row.symbol],
        ["Model", row.model], ["Provider", row.provider], ["Thinking", row.thinking],
        ["Latency", row.latency], ["Est. tokens", row.tokens], ["Status", row.status],
        ["Book", row.simulated ? "Simulation" : ""]
      ].forEach((pair) => {
        if (!pair[1]) return;
        const k = document.createElement("span");
        k.textContent = pair[0];
        const v = document.createElement("div");
        v.textContent = pair[1] || "";
        meta.appendChild(k);
        meta.appendChild(v);
      });
      body.appendChild(meta);
      body.appendChild(jsonBlock("JSON prompt", row.prompt || {}));
      body.appendChild(jsonBlock("JSON response", row.json || {}));
      panel((row.agent || "Call") + " " + (row.symbol || ""), body, true);
      return;
    }
    if (cmd === "help-q") {
      const key = btn.getAttribute("aria-label") || "";
      const open = document.querySelector(".help-pop");
      if (open && open.dataset.for === key) {
        open.remove();
        return;
      }
      document.querySelectorAll(".help-pop").forEach((node) => node.remove());
      const pop = document.createElement("div");
      pop.className = "help-pop";
      pop.dataset.for = key;
      pop.textContent = btn.getAttribute("data-help") || "";
      document.body.appendChild(pop);
      const rect = btn.getBoundingClientRect();
      const width = Math.min(280, window.innerWidth - 24);
      let left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
      pop.style.left = left + "px";
      pop.style.width = width + "px";
      pop.style.top = (rect.bottom + 6) + "px";
      const placed = pop.getBoundingClientRect();
      if (placed.bottom > window.innerHeight - 8) {
        pop.style.top = Math.max(8, rect.top - placed.height - 6) + "px";
      }
      return;
    }
    if (cmd === "view-account") {
      const accountId = btn.getAttribute("data-account");
      if (!accountId) return;
      document.cookie = "athena_view=" + encodeURIComponent(accountId) + "; Path=/; Max-Age=31536000; SameSite=Lax";
      location.reload();
      return;
    }
    if (cmd === "view-book") {
      const source = document.getElementById(btn.getAttribute("data-panel"));
      if (!source) return;
      const wrap = document.createElement("div");
      wrap.appendChild(source.content.cloneNode(true));
      panel(btn.getAttribute("data-title") || "Account", wrap, btn.hasAttribute("data-wide"));
      return;
    }
    if (cmd === "account-toggle") {
      const field = btn.getAttribute("data-field");
      const accountId = btn.getAttribute("data-account");
      btn.disabled = true;
      const body = new URLSearchParams({ action: "toggle", field: field, account_id: accountId });
      fetch("/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body,
      }).then((res) => res.json().then((data) => ({ ok: res.ok, data: data }))).then((res) => {
        btn.disabled = false;
        if (!res.ok) {
          toast("Not saved", (res.data && res.data.error) || "Try again", "err");
          return;
        }
        btn.classList.toggle("on", !!res.data.on);
        btn.setAttribute("aria-pressed", res.data.on ? "true" : "false");
        const label = field === "sync" ? "Sync" : "Trading";
        toast(label + (res.data.on ? " on" : " off"), res.data.detail || "", res.data.on ? "ok" : "warn");
      }).catch(() => {
        btn.disabled = false;
        toast("Not saved", "The console did not answer.", "err");
      });
      return;
    }
    if (cmd === "edit-symbol") {
      const wrap = document.createElement("form");
      wrap.method = "post";
      wrap.action = "/universe";
      const symbol = btn.getAttribute("data-symbol") || "";
      function shown(label, value) {
        const row = document.createElement("div");
        row.className = "field";
        const key = document.createElement("div");
        key.className = "k";
        key.textContent = label;
        const text = document.createElement("div");
        text.className = "v";
        text.textContent = value || "—";
        row.appendChild(key);
        row.appendChild(text);
        return row;
      }
      function entry(label, name, value, placeholder) {
        const block = document.createElement("label");
        block.textContent = label;
        const input = document.createElement("input");
        input.className = "input";
        input.name = name;
        input.value = value || "";
        if (placeholder) input.placeholder = placeholder;
        block.appendChild(input);
        return block;
      }
      wrap.appendChild(shown("Price", btn.getAttribute("data-price")));
      wrap.appendChild(shown("1D %", btn.getAttribute("data-change")));
      wrap.appendChild(shown("IVR", btn.getAttribute("data-ivr")));
      wrap.appendChild(shown("Phase", btn.getAttribute("data-phase")));
      wrap.appendChild(entry("Name", "name", btn.getAttribute("data-name"), "Company name"));
      wrap.appendChild(entry("Market cap", "market_cap", btn.getAttribute("data-cap"), "5.31T"));
      wrap.appendChild(entry("Classifiers", "classifiers", btn.getAttribute("data-classifiers"), "AI_NARRATIVE, HIGH_BETA"));
      wrap.appendChild(entry("Thesis", "thesis", btn.getAttribute("data-thesis"), "Why this name is in the pool"));
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "symbol";
      hidden.value = symbol;
      wrap.appendChild(hidden);
      const actions = document.createElement("div");
      actions.className = "row-actions";
      actions.style.marginTop = "14px";
      const save = document.createElement("button");
      save.className = "btn primary";
      save.type = "submit";
      save.name = "action";
      save.value = "facts";
      save.textContent = "Save";
      actions.appendChild(save);
      wrap.appendChild(actions);
      const note = document.createElement("p");
      note.className = "slot-note edit-note";
      note.textContent = "Name and market cap are shared. Classifiers and thesis stay on this account. Price and IVR come from stored bars.";
      wrap.appendChild(note);
      panel("Edit " + symbol, wrap);
      return;
    }
    if (cmd === "trade-symbol") {
      const symbol = btn.getAttribute("data-symbol") || "";
      const accountId = btn.getAttribute("data-account") || "";
      const wrap = document.createElement("div");
      wrap.className = "trade-box";
      wrap.innerHTML = '<div class="k">Side</div><div class="seg" data-trade-side>' +
        '<button type="button" class="on" data-side="BUY">Buy</button>' +
        '<button type="button" data-side="SELL">Sell</button></div>' +
        '<label>Quantity<input class="input" name="quantity" inputmode="numeric" value="100"></label>' +
        '<div class="k" style="margin-top:12px">Order</div><div class="seg" data-trade-order>' +
        '<button type="button" data-order="MKT">Market</button>' +
        '<button type="button" class="on" data-order="LMT">Limit</button>' +
        '<button type="button" data-order="STP LMT">Stop limit</button></div>' +
        '<label data-trade-limit>Limit<input class="input" name="limit" inputmode="decimal"></label>' +
        '<label data-trade-stop hidden>Stop<input class="input" name="stop" inputmode="decimal"></label>' +
        '<div class="row-actions">' +
        '<button class="btn" type="button" data-trade-auto>Auto Set Price</button>' +
        '<button class="btn primary" type="button" data-trade-send>Place order</button></div>' +
        '<p class="slot-note">Sends the order to IB for this account. Risk caps are not applied.</p>' +
        '<div class="trade-reason" data-trade-reason hidden><div class="k">Reasoning</div><p></p></div>';
      const sideBox = wrap.querySelector("[data-trade-side]");
      const orderBox = wrap.querySelector("[data-trade-order]");
      const limitRow = wrap.querySelector("[data-trade-limit]");
      const stopRow = wrap.querySelector("[data-trade-stop]");
      const reasonCard = wrap.querySelector("[data-trade-reason]");
      const reason = reasonCard.querySelector("p");
      const reasonLabel = reasonCard.querySelector(".k");
      const auto = wrap.querySelector("[data-trade-auto]");
      function showReason(text, failed) {
        const message = text || "";
        reason.textContent = message;
        reasonCard.hidden = !message;
        reasonCard.classList.toggle("err", Boolean(message && failed));
        reasonLabel.textContent = failed ? "Notice" : "Reasoning";
      }
      function pick(box, attr, value) {
        box.querySelectorAll("button").forEach((node) => {
          node.classList.toggle("on", node.getAttribute(attr) === value);
        });
      }
      function side() {
        const on = sideBox.querySelector("button.on");
        return on ? on.getAttribute("data-side") : "BUY";
      }
      function orderType() {
        const on = orderBox.querySelector("button.on");
        return on ? on.getAttribute("data-order") : "LMT";
      }
      function paintOrder() {
        const kind = orderType();
        limitRow.hidden = kind === "MKT";
        stopRow.hidden = kind !== "STP LMT";
        auto.disabled = kind === "MKT";
      }
      sideBox.addEventListener("click", (event) => {
        const choice = event.target.closest("button");
        if (!choice) return;
        pick(sideBox, "data-side", choice.getAttribute("data-side"));
      });
      orderBox.addEventListener("click", (event) => {
        const choice = event.target.closest("button");
        if (!choice) return;
        pick(orderBox, "data-order", choice.getAttribute("data-order"));
        paintOrder();
      });
      paintOrder();
      auto.addEventListener("click", () => {
        if (auto.disabled) return;
        auto.disabled = true;
        auto.textContent = "Setting…";
        showReason("");
        const body = new URLSearchParams({
          action: "price",
          symbol: symbol,
          last: btn.getAttribute("data-price") || "",
          side: side(),
          order: orderType()
        });
        fetch("/universe", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body
        }).then((res) => res.json().then((data) => ({ ok: res.ok, data: data }))).then((result) => {
          const data = result.data || {};
          if (!result.ok || !data.ok) {
            showReason(data.message || "No price", true);
            return;
          }
          if (data.limit) wrap.querySelector("[name=limit]").value = data.limit;
          if (orderType() === "STP LMT" && data.stop) wrap.querySelector("[name=stop]").value = data.stop;
          showReason(data.reason || "", false);
        }).catch(() => {
          showReason("The console did not answer.", true);
        }).finally(() => {
          auto.disabled = orderType() === "MKT";
          auto.textContent = "Auto Set Price";
        });
      });
      wrap.querySelector("[data-trade-send]").addEventListener("click", () => {
        const kind = orderType();
        const qty = (wrap.querySelector("[name=quantity]").value || "").trim();
        const limit = (wrap.querySelector("[name=limit]").value || "").trim();
        const stop = (wrap.querySelector("[name=stop]").value || "").trim();
        let price = "at market";
        if (kind === "LMT") price = "at limit " + (limit || "—");
        if (kind === "STP LMT") price = "at stop " + (stop || "—") + " limit " + (limit || "—");
        const text = side() + " " + (qty || "—") + " " + symbol + " " + price + " on " + (accountId || "this account") + "?";
        confirmBox("Place order", text, () => {
          const body = new URLSearchParams({
            action: "trade",
            symbol: symbol,
            side: side(),
            quantity: qty,
            order: kind,
            limit: limit,
            stop: stop
          });
          fetch("/universe", {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: body
          }).then((res) => res.json().then((data) => ({ ok: res.ok, data: data }))).then((result) => {
            const data = result.data || {};
            if (!result.ok || !data.ok) {
              toast("Not sent", data.message || "Try again", "err");
              return;
            }
            const task = data.task || {};
            if (task.id) watchTask(task.id);
            toast("Sending", data.message || "", "ok");
          }).catch(() => {
            toast("Not sent", "The console did not answer.", "err");
          });
        }, "Send");
      });
      panel("Trade " + symbol, wrap);
      return;
    }
    if (cmd === "draft-amend" || cmd === "draft-reject") {
      const card = btn.closest(".card");
      const holder = card && card.querySelector("." + cmd);
      const form = holder && holder.querySelector("form");
      if (!form) return;
      const wrap = document.createElement("div");
      wrap.appendChild(form);
      const putBack = () => holder.appendChild(form);
      const cancel = document.createElement("button");
      cancel.className = "btn ghost";
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => { putBack(); closeAll(); });
      modal(cmd === "draft-amend" ? "Amend" : "Reject", wrap, [cancel]);
      document.querySelector(".ath-modal .m-x").addEventListener("click", putBack);
      document.querySelector(".ath-overlay").addEventListener("click", putBack, true);
      return;
    }
    if (cmd === "manual-ingest") {
      const wrap = document.createElement("form");
      wrap.id = "manual-ingest-form";
      wrap.method = "post";
      wrap.action = "/news";
      wrap.innerHTML = '<input class="input mono" name="url" type="url" placeholder="https://…" required autocomplete="off">' +
        '<input type="hidden" name="action" value="ingest_url">' +
        '<p class="slot-note" style="margin:10px 0 0">Athena pulls the page. DSH writes the symbol, headline, type, and sentiment.</p>';
      const cancel = document.createElement("button");
      cancel.className = "btn ghost";
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeAll);
      const ok = document.createElement("button");
      ok.className = "btn primary";
      ok.type = "submit";
      ok.setAttribute("form", "manual-ingest-form");
      ok.textContent = "Ingest";
      wrap.addEventListener("submit", () => {
        ok.disabled = true;
        ok.textContent = "Pulling…";
      });
      modal("Manual ingest", wrap, [cancel, ok]);
      wrap.querySelector("input").focus();
      return;
    }
    if (cmd === "seed-defaults") {
      btn.disabled = true;
      fetch("/universe", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "action=seed"
      }).then((res) => {
        if (!res.ok) throw new Error("seed");
        location.reload();
      }).catch(() => {
        btn.disabled = false;
        toast("Not seeded", "The console did not answer.", "err");
      });
      return;
    }
    if (cmd === "add-symbol") {
      const wrap = document.createElement("form");
      wrap.id = "add-symbol-form";
      wrap.method = "post";
      wrap.action = "/universe";
      wrap.innerHTML = '<input class="input mono" name="symbol" placeholder="Ticker" required autocomplete="off">' +
        '<select class="input" name="market" aria-label="Market" style="margin-top:8px">' +
        '<option value="us">US</option><option value="hk">HK</option><option value="ca">CA</option></select>' +
        '<input type="hidden" name="action" value="add">' +
        '<p class="slot-note" style="margin:10px 0 0">Athena looks up the name and size, syncs a price once, and DSH picks the classifier badges.</p>';
      const marketPick = wrap.querySelector("[name=market]");
      let marketTouched = false;
      marketPick.addEventListener("change", () => { marketTouched = true; });
      wrap.querySelector("[name=symbol]").addEventListener("input", (event) => {
        if (marketTouched) return;
        marketPick.value = /^\d+$/.test((event.target.value || "").trim()) ? "hk" : "us";
      });
      const cancel = document.createElement("button");
      cancel.className = "btn ghost";
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeAll);
      const ok = document.createElement("button");
      ok.className = "btn primary";
      ok.type = "submit";
      ok.setAttribute("form", "add-symbol-form");
      ok.textContent = "Add and fill";
      wrap.addEventListener("submit", (event) => {
        event.preventDefault();
        ok.disabled = true;
        ok.classList.add("busy");
        const symbol = wrap.querySelector("[name=symbol]").value.toUpperCase();
        const market = wrap.querySelector("[name=market]").value;
        post("/api/tasks", "action=add&symbol=" + encodeURIComponent(symbol) + "&market=" + encodeURIComponent(market)).then((result) => {
          if (!result.ok) {
            ok.disabled = false;
            ok.classList.remove("busy");
            toast("Not filled", result.message || "", "err");
            return;
          }
          closeAll();
        }).catch(() => {
          ok.disabled = false;
          ok.classList.remove("busy");
          toast("Not filled", "The console did not answer.", "err");
        });
      });
      const symbolInput = wrap.querySelector("[name=symbol]");
      symbolInput.addEventListener("input", () => {
        const start = symbolInput.selectionStart;
        const end = symbolInput.selectionEnd;
        symbolInput.value = symbolInput.value.toUpperCase();
        symbolInput.setSelectionRange(start, end);
      });
      modal("Add symbol", wrap, [cancel, ok]);
      symbolInput.focus();
      return;
    }
    if (cmd === "add-account") {
      const wrap = document.createElement("div");
      wrap.innerHTML = '<input class="input mono" name="account_id" placeholder="U1234567-XXX" style="margin-bottom:10px">' +
        '<input class="input" name="label" placeholder="Label (e.g. Income sleeve)">' +
        '<p class="slot-note" style="margin:10px 0 0">Any IB account id works. Starts in paper mode, sync off until you enable.</p>';
      const cancel = document.createElement("button");
      cancel.className = "btn ghost";
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeAll);
      const ok = document.createElement("button");
      ok.className = "btn primary";
      ok.type = "button";
      ok.textContent = "Add account";
      ok.addEventListener("click", () => {
        const accountId = wrap.querySelector("[name=account_id]").value.trim();
        const label = wrap.querySelector("[name=label]").value.trim();
        if (!accountId) {
          toast("Account id is required", "", "err");
          return;
        }
        ok.disabled = true;
        const body = new URLSearchParams({ action: "add", account_id: accountId, label: label });
        fetch("/accounts", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body,
        }).then((res) => res.text().then((text) => ({ ok: res.ok, text: text }))).then((res) => {
          const flash = readFlash(res.text);
          if (!res.ok) {
            ok.disabled = false;
            toast(flash ? flash.title : "Not added", flash ? flash.sub : "Check the account id", "err");
            return;
          }
          if (flash) remember(flash.title, flash.sub, flash.kind);
          location.href = "/accounts";
        }).catch(() => {
          ok.disabled = false;
          toast("Not added", "The console did not answer.", "err");
        });
      });
      modal("Add IB account", wrap, [cancel, ok]);
      wrap.querySelector("input").focus();
      return;
    }
    if (cmd === "halt") {
      confirmBox("Halt trading?", "Blocks new drafts on this account until you resume.", () => postCmd("halt"), "Halt");
      return;
    }
    if (cmd === "nlv" || cmd === "pnl") {
      const body = document.createElement("div");
        body.innerHTML = "<table class='data'><thead><tr><th>month</th><th>premium</th><th>trades</th><th>up</th></tr></thead><tbody>" +
        data.months.map((m) => "<tr><td>" + m.name + "</td><td class='mono'>" + m.premium + "</td><td>" + m.trades + "</td><td>" + (m.win === null ? "—" : m.win ? "yes" : "no") + "</td></tr>").join("") +
        "</tbody></table>";
      if (cmd === "nlv") {
        const chart = document.createElement("div");
        chart.className = "chart-box";
        chart.style.height = "180px";
        body.prepend(chart);
        panel("nlv", body);
        sparkline(chart, data.nlv, "#6b96f2", true);
      } else {
        const chart = document.createElement("div");
        chart.className = "chart-box";
        chart.style.height = "180px";
        body.prepend(chart);
        panel("premium vs net cash", body);
        bars(chart, data.pnl);
      }
      return;
    }
    if (cmd === "slots") {
      const body = document.getElementById("slot-editor").content.cloneNode(true);
      const wrap = document.createElement("div");
      wrap.appendChild(body);
      panel("ib slot editor", wrap);
      const stored = JSON.parse(document.getElementById("slot-windows").textContent);
      const list = document.querySelector(".ath-panel .slot-rows");
      stored.forEach((item) => list.appendChild(slotRow(item)));
      refreshSlotPreview(document.querySelector(".ath-panel"));
      return;
    }
    if (cmd === "add-slot") {
      const list = btn.closest(".ath-panel").querySelector(".slot-rows");
      const last = list.querySelector(".slot-row:last-child [name=end]");
      const start = last && last.value ? last.value : "09:30";
      list.appendChild(slotRow({ owner: "Athena", start: start, end: "" }));
      refreshSlotPreview(btn.closest(".ath-panel"));
      return;
    }
    if (cmd === "remove-slot") {
      const panelNode = btn.closest(".ath-panel");
      btn.closest(".slot-row").remove();
      refreshSlotPreview(panelNode);
      return;
    }
    if (cmd === "save-slots") {
      const root = btn.closest(".ath-panel");
      const packed = packSlots(root);
      if (packed.error) {
        toast("Not saved", packed.error, "err");
        return;
      }
      const body = new URLSearchParams({
        athena_slots: packed.athena,
        mobile_slots: packed.mobile
      });
      post("/api/slots", body).then((result) => {
        const title = result.ok ? "Saved" : "Not saved";
        const kind = result.ok ? "ok" : "err";
        if (result.ok) {
          remember(title, result.message || "", kind);
          location.reload();
          return;
        }
        toast(title, result.message || "", kind);
      });
      return;
    }
    if (cmd === "sync-positions") {
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = "Syncing…";
      post("/api/tasks", "action=positions").then((result) => {
        if (!result.ok) {
          btn.disabled = false;
          btn.textContent = "Sync from IB";
          toast("Not done", result.message || "", "err");
        }
      }).catch(() => {
        btn.disabled = false;
        btn.textContent = "Sync from IB";
        toast("Not done", "The console did not answer.", "err");
      });
      return;
    }
    if (cmd === "sync-symbol") {
      const symbol = btn.getAttribute("data-symbol") || "";
      btn.disabled = true;
      btn.classList.add("busy");
      post("/api/tasks", "action=sync&symbol=" + encodeURIComponent(symbol)).then((result) => {
        if (!result.ok) {
          btn.disabled = false;
          btn.classList.remove("busy");
          toast("Not filled", result.message || "", "err");
        }
      }).catch(() => {
        btn.disabled = false;
        btn.classList.remove("busy");
        toast("Not filled", "The console did not answer.", "err");
      });
      return;
    }
    if (cmd === "ops") {
      const body = document.createElement("div");
      const commands = ["health", "gateway restart", "athena restart", "backup now"];
      const badges = commands.map((name) => "<span class='tag'>" + name + "</span>").join("");
      body.innerHTML = '<div class="field"><div class="k">allowlist</div><div class="v"><span class="badge-row">' + badges + "</span></div></div>" +
        '<div class="field"><div class="k">last restart</div><div class="v"><span class="badge-row"><span class="tag">athena</span><span class="mono">' + (cycleStarted || "—") + "</span></span></div></div>" +
        '<div class="row-actions" style="margin-top:14px">' +
        '<button class="btn ghost" data-cmd="gateway restart" type="button">restart gateway</button>' +
        '<button class="btn ghost" data-cmd="athena restart" type="button">restart athena</button>' +
        '<button class="btn ghost" data-cmd="backup" type="button">backup now</button></div>';
      panel("ops agent", body);
      return;
    }
    postCmd(cmd);
  });
  function paintHealth(report) {
    const light = document.getElementById("healthLight");
    const meter = document.getElementById("healthMeter");
    const age = document.getElementById("healthAge");
    if (!light) return;
    light.className = "health-light " + report.state;
    meter.className = "health-meter " + report.state;
    meter.querySelectorAll("i").forEach((bar, i) => { bar.className = i < report.ok ? "on" : ""; });
    const now = new Date();
    const p = (n) => String(n).padStart(2, "0");
    age.textContent = report.ok + "/5 · " + p(now.getHours()) + ":" + p(now.getMinutes()) + ":" + p(now.getSeconds());
    report.checks.forEach((item) => {
      const node = document.querySelector('[data-health="' + item.name + '"]');
      if (node) node.textContent = item.text;
    });
    paintReplay(report);
  }
  function paintReplay(report) {
    if (!report || !("replay" in report)) return;
    const body = document.getElementById("healthBody");
    if (!body) return;
    let node = body.querySelector('[data-health="Replay"]');
    if (!report.replay) {
      if (node && node.parentElement) node.parentElement.remove();
      return;
    }
    if (!node) {
      const field = document.createElement("div");
      field.className = "field";
      field.innerHTML = '<div class="k">Replay</div><div class="v" data-health="Replay"></div>';
      body.appendChild(field);
      node = field.querySelector("[data-health]");
    }
    node.textContent = report.replay;
  }
  setInterval(() => {
    fetch("/api/health").then((res) => res.json()).then(paintHealth).catch(() => {});
  }, 5000);
  if (location.hash === "#slots") {
    const open = document.querySelector("[data-cmd=slots]");
    if (open) open.click();
  }
  function readFlash(html) {
    const title = /id="ath-flash"[^>]*data-title="([^"]*)"/.exec(html);
    const sub = /data-sub="([^"]*)"/.exec(html);
    const kind = /data-kind="([^"]*)"/.exec(html);
    if (!title) return null;
    const node = document.createElement("textarea");
    node.innerHTML = title[1];
    const subNode = document.createElement("textarea");
    subNode.innerHTML = sub ? sub[1] : "";
    return { title: node.value, sub: subNode.value, kind: kind ? kind[1] : "ok" };
  }
  const hiddenTasks = new Set(JSON.parse(sessionStorage.getItem("ath-task-hide") || "[]"));
  const reloadedTasks = new Set(JSON.parse(sessionStorage.getItem("ath-task-reloaded") || "[]"));
  const toastedTasks = new Set(JSON.parse(sessionStorage.getItem("ath-task-toast") || "[]"));
  function refreshHealth() {
    fetch("/api/health").then((res) => res.json()).then((health) => {
      const light = document.getElementById("healthLight");
      const meter = document.getElementById("healthMeter");
      const age = document.getElementById("healthAge");
      if (!light || !meter || !age) return;
      light.className = "health-light " + health.state;
      meter.className = "health-meter " + health.state;
      meter.replaceChildren();
      for (let index = 0; index < 5; index += 1) {
        const bar = document.createElement("i");
        if (index < health.ok) bar.className = "on";
        meter.appendChild(bar);
      }
      age.textContent = health.ok + "/5";
      (health.checks || []).forEach((item) => {
        const node = document.querySelector('[data-health="' + item.name + '"]');
        if (node) node.textContent = item.text;
      });
      paintReplay(health);
    }).catch(() => {});
  }
  function paintTasks(tasks) {
    const root = dock().querySelector(".ath-tasks");
    const running = new Set(tasks.filter((task) => task.status === "running").map((task) => task.symbol));
    const busy = {
      backup: ["Backing up…", "Backup now"],
      halt: ["Halting…", "Halt trading"],
      resume: ["Resuming…", "Resume"]
    };
    const runningKinds = new Set(tasks.filter((task) => task.status === "running").map((task) => task.kind));
    Object.keys(busy).forEach((kind) => {
      document.querySelectorAll('[data-cmd="' + kind + '"]').forEach((button) => {
        if (runningKinds.has(kind)) {
          button.disabled = true;
          button.textContent = busy[kind][0];
        } else if (button.textContent === busy[kind][0]) {
          button.disabled = false;
          button.textContent = busy[kind][1];
        }
      });
    });
    document.querySelectorAll("[data-cmd=sync-positions]").forEach((button) => {
      if (runningKinds.has("positions")) {
        button.disabled = true;
        button.textContent = "Syncing…";
      } else if (button.textContent === "Syncing…") {
        button.disabled = false;
        button.textContent = "Sync from IB";
      }
    });
    document.querySelectorAll("[data-cmd=sync-symbol]").forEach((button) => {
      const symbol = button.getAttribute("data-symbol");
      const on = running.has(symbol);
      button.disabled = on;
      button.classList.toggle("busy", on);
    });
    root.innerHTML = "";
    tasks.forEach((task) => {
      if (hiddenTasks.has(task.id)) return;
      if (task.status !== "running" && !toastedTasks.has(task.id)) {
        toastedTasks.add(task.id);
        sessionStorage.setItem("ath-task-toast", JSON.stringify([...toastedTasks]));
        toast(task.status === "error" ? "Not done" : task.title, task.detail || "", task.status === "error" ? "err" : "ok");
        if (task.kind === "health") refreshHealth();
        if (task.kind === "halt" || task.kind === "resume") {
          setTimeout(() => location.reload(), 500);
        }
      }
      if (task.status !== "running" && (task.kind === "health" || task.kind === "backup" || task.kind === "halt" || task.kind === "resume")) return;
      if (task.status === "done" && task.kind === "fill" && location.pathname === "/universe" && !reloadedTasks.has(task.id)) {
        reloadedTasks.add(task.id);
        sessionStorage.setItem("ath-task-reloaded", JSON.stringify([...reloadedTasks]));
        refreshPoolRow(task.symbol);
        return;
      }
      const refreshPage = task.kind === "positions" && location.pathname === "/positions";
      if (task.status === "done" && refreshPage && !reloadedTasks.has(task.id)) {
        reloadedTasks.add(task.id);
        sessionStorage.setItem("ath-task-reloaded", JSON.stringify([...reloadedTasks]));
        location.reload();
        return;
      }
      const card = document.createElement("div");
      card.className = "ath-task" + (task.status === "error" ? " error" : "");
      const mark = document.createElement("div");
      if (task.status === "running") mark.className = "ath-spin";
      const copy = document.createElement("div");
      copy.innerHTML = "<b></b><div class='t-sub'></div>";
      copy.querySelector("b").textContent = task.title;
      copy.querySelector(".t-sub").textContent = task.detail || "";
      const close = document.createElement("button");
      close.className = "task-x";
      close.type = "button";
      close.textContent = "×";
      close.addEventListener("click", () => {
        hiddenTasks.add(task.id);
        sessionStorage.setItem("ath-task-hide", JSON.stringify([...hiddenTasks]));
        card.remove();
      });
      card.appendChild(mark);
      card.appendChild(copy);
      card.appendChild(close);
      root.appendChild(card);
    });
  }
  let cycleStarted = "";
  const huntGlyphs = ["|", "-", "/", "木", "才", "卜", "カ", "タ", "カ", "ナ"];
  const huntSlots = ["|", "-", "/", "木", "才"];
  let huntAt = 0;
  let huntTimer = 0;
  function huntGlyph() {
    return huntGlyphs[Math.floor(Math.random() * huntGlyphs.length)];
  }
  function paintHunt(clock) {
    huntSlots[huntAt] = huntGlyph();
    huntAt = (huntAt + 1) % huntSlots.length;
    clock.replaceChildren();
    huntSlots.forEach((glyph) => {
      const cell = document.createElement("span");
      cell.textContent = glyph;
      clock.appendChild(cell);
    });
  }
  function startHunt(clock) {
    if (huntTimer) return;
    clock.className = "cycle-clock busy";
    paintHunt(clock);
    huntTimer = setInterval(() => paintHunt(clock), 100);
  }
  function stopHunt() {
    if (!huntTimer) return;
    clearInterval(huntTimer);
    huntTimer = 0;
  }
  function paintCycle(view) {
    cycleStarted = view.started || "";
    const line = document.querySelector("[data-cycle=line]");
    const mark = document.querySelector("[data-cycle=mark]");
    const bar = document.querySelector("[data-cycle=bar]");
    const up = document.querySelector("[data-cycle=up]");
    const clock = document.querySelector("[data-cycle=clock]");
    if (!line || !mark || !bar || !up) return;
    const mins = String(Math.floor((view.remaining || 0) / 60)).padStart(2, "0");
    const secs = String((view.remaining || 0) % 60).padStart(2, "0");
    line.textContent = "cycle #" + view.cycle;
    if (view.state === "running") {
      mark.className = "cycle-mark ath-spin";
      if (clock) startHunt(clock);
    } else {
      mark.className = "cycle-mark";
      if (clock) {
        stopHunt();
        clock.className = "cycle-clock";
        clock.textContent = mins + ":" + secs;
      }
    }
    bar.style.width = Math.round((view.progress || 0) * 100) + "%";
    up.textContent = "uptime " + view.uptime;
    paintStrip(view);
    const orch = document.querySelector('[data-health="Orchestrator"]');
    if (orch) orch.textContent = "running · cycle #" + view.cycle + " · up " + view.uptime + " · loop " + view.loop + "s";
  }
  function paintStrip(view) {
    const title = document.querySelector("[data-strip=title]");
    const mark = document.querySelector("[data-strip=mark]");
    const meta = document.querySelector("[data-strip=meta]");
    const activity = document.querySelector("[data-strip=activity]");
    const clock = document.querySelector("[data-strip=clock]");
    const bar = document.querySelector("[data-strip=bar]");
    const roll = document.querySelector("[data-strip=roll]");
    if (!title || !mark || !meta || !clock || !bar || !roll) return;
    const mins = Math.floor((view.remaining || 0) / 60);
    const secs = String((view.remaining || 0) % 60).padStart(2, "0");
    const lines = view.activity || [];
    const latest = lines.length ? lines[lines.length - 1] : "";
    const runningItem = (view.roll || []).find((item) => item.status === "running");
    if (view.state === "running") {
      mark.className = "cycle-mark ath-spin";
      title.textContent = "step " + (view.index || 1) + "/8";
      meta.textContent = (runningItem && runningItem.detail) ? runningItem.detail : (view.step || "").toLowerCase();
      if (activity) activity.textContent = latest;
      clock.textContent = "";
    } else {
      mark.className = "cycle-mark";
      title.textContent = "Cycle " + view.cycle + " · waiting";
      meta.textContent = "next in";
      if (activity) activity.textContent = "";
      clock.textContent = mins + ":" + secs;
    }
    bar.style.width = Math.round((view.progress || 0) * 100) + "%";
    roll.replaceChildren();
    (view.roll || []).forEach((item) => {
      const row = document.createElement("div");
      if (item.status === "running") row.className = "on";
      if (item.status === "skipped") row.className = "skipped";
      const state = document.createElement("span");
      state.className = "st";
      state.textContent = item.status;
      const name = document.createElement("span");
      name.textContent = item.name;
      const detail = document.createElement("span");
      detail.textContent = item.detail || "";
      row.append(state, name, detail);
      roll.appendChild(row);
    });
    if (view.state === "running") {
      lines.slice(-7, -1).forEach((line) => {
        const row = document.createElement("div");
        row.className = "stream";
        const prompt = document.createElement("span");
        prompt.className = "st";
        prompt.textContent = ">";
        const text = document.createElement("span");
        text.textContent = line;
        row.append(prompt, text);
        roll.appendChild(row);
      });
      roll.scrollTop = roll.scrollHeight;
    }
  }
  function pollTasks() {
    fetch("/api/tasks").then((res) => res.json()).then((body) => paintTasks(body.tasks || [])).catch(() => {});
  }
  function pollCycle() {
    fetch("/api/cycle").then((res) => res.json()).then(paintCycle).catch(() => {});
  }
  document.querySelectorAll("tr.phase-pick").forEach((row) => {
    const open = () => { location.href = row.getAttribute("data-href"); };
    row.addEventListener("click", (event) => {
      if (event.target.closest("a, button")) return;
      open();
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  });
  const picked = document.querySelector("tr.phase-pick.selected");
  if (picked) picked.scrollIntoView({ block: "nearest" });
  function paintPulse(body) {
    (body.quotes || []).forEach((quote) => {
      const cell = document.querySelector('.pulse-px[data-symbol="' + quote.symbol + '"]');
      if (!cell) return;
      const price = cell.querySelector(".px");
      const change = cell.querySelector(".px-chg");
      if (price.textContent !== quote.price) {
        price.textContent = quote.price;
        price.classList.remove("px-tick");
        void price.offsetWidth;
        price.classList.add("px-tick");
      }
      change.textContent = quote.change;
      change.className = "px-chg" + (quote.tone ? " " + quote.tone : "");
    });
    const sleeve = body.sleeve || {};
    ["nav", "change", "premium", "premium_meta", "cash", "cash_pct", "cash_meta", "pending"].forEach((key) => {
      const node = document.querySelector('[data-sleeve="' + key + '"]');
      if (node && sleeve[key] != null) node.textContent = String(sleeve[key]);
    });
  }
  document.addEventListener("change", (event) => {
    const node = event.target.closest("select.intent");
    if (node && node.form) postUniverse(node.form);
  });
  const audit = document.getElementById("audit-table");
  if (audit) {
    const keyword = document.getElementById("audit-keyword");
    const filters = document.getElementById("audit-filters");
    let agent = "all";
    const applyAudit = () => {
      const needle = (keyword.value || "").trim().toLowerCase();
      audit.querySelectorAll("tbody tr.hit").forEach((row) => {
        let data = {};
        try { data = JSON.parse(row.getAttribute("data-json") || "{}"); } catch (err) {}
        const book = data.simulated ? " simulation" : "";
        const hay = ((data.agent || "") + " " + (data.symbol || "") + " " + (data.status || "") + " " + (data.model || "") + " " + (data.result || "") + book).toLowerCase();
        row.hidden = (agent !== "all" && data.agent !== agent) || (needle && !hay.includes(needle));
      });
    };
    keyword.addEventListener("input", applyAudit);
    document.addEventListener("ath-feed", applyAudit);
    filters.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      agent = button.getAttribute("data-agent");
      filters.querySelectorAll("button").forEach((node) => node.classList.remove("on"));
      button.classList.add("on");
      applyAudit();
    });
  }
  const pool = document.getElementById("pool-table");
  if (pool) {
    const keyword = document.getElementById("pool-keyword");
    const classifier = document.getElementById("pool-classifier");
    let sortKey = "";
    let sortDir = "asc";
    const poolRows = () => [...pool.querySelectorAll("tbody tr[data-symbol]")];
    const poolValue = (row) => {
      if (sortKey === "symbol") return (row.getAttribute("data-symbol") || "").toUpperCase();
      const raw = row.getAttribute(sortKey === "price" ? "data-price" : "data-cap");
      if (!raw) return null;
      const num = Number(raw);
      return Number.isFinite(num) ? num : null;
    };
    const applyPool = () => {
      const needle = (keyword.value || "").trim().toLowerCase();
      const code = classifier.value;
      const empty = document.getElementById("pool-empty");
      const list = poolRows();
      if (sortKey) {
        list.sort((a, b) => {
          const av = poolValue(a);
          const bv = poolValue(b);
          if (av == null && bv == null) return 0;
          if (av == null) return 1;
          if (bv == null) return -1;
          const order = typeof av === "string" ? av.localeCompare(bv) : av - bv;
          return sortDir === "asc" ? order : -order;
        });
      }
      const body = pool.querySelector("tbody");
      if (empty && body) list.forEach((row) => body.insertBefore(row, empty));
      let shown = 0;
      list.forEach((row) => {
        const hay = ((row.getAttribute("data-symbol") || "") + " " + (row.getAttribute("data-name") || "")).toLowerCase();
        const codes = (row.getAttribute("data-classifiers") || "").split(",").filter(Boolean);
        const hit = (!needle || hay.includes(needle)) && (!code || codes.includes(code));
        row.hidden = !hit;
        if (hit) shown += 1;
      });
      if (empty) empty.hidden = list.length === 0 || shown > 0;
    };
    keyword.addEventListener("input", applyPool);
    classifier.addEventListener("change", applyPool);
    document.addEventListener("ath-feed", applyPool);
    pool.querySelectorAll("button.sort").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.getAttribute("data-sort");
        sortDir = sortKey === key && sortDir === "asc" ? "desc" : "asc";
        sortKey = key;
        pool.querySelectorAll("button.sort").forEach((other) => other.removeAttribute("data-dir"));
        button.setAttribute("data-dir", sortDir);
        applyPool();
      });
    });
  }
  if (document.querySelector(".pulse-px")) {
    const pollPulse = () => fetch("/api/pulse").then((res) => res.json()).then(paintPulse).catch(() => {});
    pollPulse();
    setInterval(pollPulse, 3000);
  }
  const cycleStrip = document.getElementById("cycleStrip");
  if (cycleStrip && sessionStorage.getItem("ath-cycle-fold") === "1") cycleStrip.classList.add("compact");
  const feed = document.querySelector("[data-feed]");
  if (feed) {
    const sentinel = document.createElement("div");
    sentinel.className = "feed-sentinel";
    feed.appendChild(sentinel);
    const seen = () => {
      const box = feed.getBoundingClientRect();
      const mark = sentinel.getBoundingClientRect();
      return mark.top <= box.bottom + 80;
    };
    const pump = () => {
      if (feed.getAttribute("data-more") !== "1" || !seen()) return;
      pullFeed(feed, false);
    };
    if ("IntersectionObserver" in window) {
      const observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) pump();
      }, { root: feed, rootMargin: "80px" });
      observer.observe(sentinel);
    }
    document.addEventListener("ath-feed", pump);
    feed.addEventListener("scroll", pump, { passive: true });
  }
  function bindStatusFeed(filtersId, feedId, keywordId, exportId) {
    const filters = document.getElementById(filtersId);
    const box = document.getElementById(feedId);
    if (!filters || !box) return;
    const keyword = document.getElementById(keywordId);
    const exporter = exportId ? document.getElementById(exportId) : null;
    const current = filters.querySelector("button.on");
    let status = current ? (current.getAttribute("data-status") || "all") : "all";
    let wait = null;
    const apply = () => {
      const parts = [];
      if (status !== "all") parts.push("status=" + encodeURIComponent(status));
      const needle = keyword ? (keyword.value || "").trim() : "";
      if (needle) parts.push("q=" + encodeURIComponent(needle));
      const query = parts.join("&");
      box.setAttribute("data-query", query);
      if (exporter) {
        const base = exporter.getAttribute("data-base") || "/history.csv";
        exporter.href = base + (query ? "?" + query : "");
      }
      pullFeed(box, true);
    };
    if (keyword) {
      keyword.addEventListener("input", () => {
        clearTimeout(wait);
        wait = setTimeout(apply, 200);
      });
    }
    filters.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-status]");
      if (!button) return;
      status = button.getAttribute("data-status") || "all";
      filters.querySelectorAll("button").forEach((node) => node.classList.remove("on"));
      button.classList.add("on");
      apply();
    });
  }
  bindStatusFeed("decision-filters", "decision-feed", "decision-keyword");
  bindStatusFeed("history-filters", "history-feed", "history-keyword", "history-export");
  bindStatusFeed("orders-filters", "orders-feed", "");
  const ordersFeed = document.getElementById("orders-feed");
  if (ordersFeed && window.EventSource) {
    const ordersSource = new EventSource("/api/orders/events");
    ordersSource.onmessage = (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch (err) { return; }
      const box = document.getElementById("orders-feed");
      if (!box || !payload || !payload.id) return;
      const current = document.querySelector("#orders-filters button.on");
      const filter = current ? (current.getAttribute("data-status") || "all") : "all";
      const show = filter === "all" || filter === payload.bucket;
      const row = box.querySelector('tr[data-order="' + payload.id + '"]');
      if (row) {
        if (!show) row.remove();
        else row.outerHTML = payload.html || "";
        return;
      }
      if (!show || box.getAttribute("data-tail") === "1") return;
      const body = box.querySelector("tbody");
      if (!body || !payload.html) return;
      const empty = body.querySelector("td[colspan]");
      if (empty && empty.parentElement) empty.parentElement.remove();
      body.insertAdjacentHTML("afterbegin", payload.html);
    };
  }
  function simWindow(count, index) {
    const span = 60;
    const half = span / 2;
    if (count <= span || index < half) return 0;
    if (count - index - 1 < half) return Math.max(0, count - span);
    return index - half;
  }
  function paintSimText(info) {
    const status = document.getElementById("sim-status");
    if (!status) return;
    status.replaceChildren();
    if (info.status === "active") {
      const spin = document.createElement("span");
      spin.className = "sim-spin";
      spin.setAttribute("aria-hidden", "true");
      status.appendChild(spin);
    }
    status.appendChild(document.createTextNode(info.label || "None"));
    const day = document.getElementById("sim-day");
    const span = document.getElementById("sim-window");
    const step = document.getElementById("sim-step");
    const left = document.getElementById("sim-left");
    if (day) day.textContent = info.cursor_label || "Not started";
    if (span) span.textContent = (info.start_label || "—") + " – " + (info.end_label || "—");
    if (step) step.textContent = "Day " + info.day + " of " + info.total;
    if (left) left.textContent = info.left + " cycles left";
    const link = document.querySelector('.nav a[href="/simulation"]');
    if (!link) return;
    let badge = link.querySelector(".nav-badge");
    const on = (info.status === "active" || info.status === "halted") && info.total > 0;
    if (!on) {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = document.createElement("span");
      link.appendChild(badge);
    }
    badge.className = info.status === "halted" ? "nav-badge warn" : "nav-badge";
    badge.textContent = info.day + "/" + info.total;
    badge.title = "Day " + info.day + " of " + info.total;
  }
  function simStep(span) {
    const raw = span / 5;
    if (!(raw > 0)) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / pow;
    const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return nice * pow;
  }
  function simVol(value) {
    const n = Number(value) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(Math.round(n));
  }
  function simDay(iso) {
    return iso.slice(8, 10) + "/" + iso.slice(5, 7);
  }
  function paintSimPlot(el, dates, bars, cursor, start) {
    const width = Math.max(el.clientWidth, 320);
    const height = Math.max(el.clientHeight, 280);
    const slots = 60;
    const padL = 8;
    const padR = 58;
    const priceTop = 26;
    const volH = Math.max(64, Math.round(height * 0.16));
    const dateH = 16;
    const priceBottom = height - volH - dateH;
    const priceH = Math.max(priceBottom - priceTop, 40);
    const plotW = width - padL - padR;
    const slot = plotW / slots;
    const visible = [];
    for (let i = 0; i < slots; i++) {
      const at = start + i;
      visible.push(at < dates.length ? bars[at] : null);
    }
    let low = Infinity;
    let high = -Infinity;
    let maxVol = 0;
    visible.forEach((bar) => {
      if (!bar) return;
      low = Math.min(low, bar.l);
      high = Math.max(high, bar.h);
      maxVol = Math.max(maxVol, bar.v || 0);
    });
    let read = el.querySelector(".sim-read");
    if (!read) {
      read = document.createElement("div");
      read.className = "sim-read";
      el.appendChild(read);
    }
    let now = el.querySelector(".sim-now");
    if (!now) {
      now = document.createElement("div");
      now.className = "sim-now";
      now.setAttribute("aria-hidden", "true");
      el.appendChild(now);
    }
    if (!isFinite(low)) {
      const old = el.querySelector("svg");
      if (old) old.remove();
      read.textContent = "";
      now.hidden = true;
      return;
    }
    const mid = (high + low) / 2;
    const pad = Math.max((high - low) * 0.1, mid * 0.01);
    const top = high + pad;
    const floor = Math.max(low - pad, 0);
    const span = top - floor || 1;
    const yOf = (price) => priceTop + (top - price) / span * priceH;
    const volTop = priceBottom + dateH;
    const volBase = height - 2;
    const parts = [];
    const step = simStep(top - floor);
    for (let price = Math.ceil(floor / step) * step; price <= top + step * 0.01; price += step) {
      const y = yOf(price);
      if (y < priceTop - 1 || y > priceBottom + 1) continue;
      parts.push('<line x1="' + padL + '" y1="' + y.toFixed(1) + '" x2="' + (padL + plotW) + '" y2="' + y.toFixed(1) + '" stroke="#243044" stroke-width="1"></line>');
      parts.push('<text x="' + (width - padR + 6) + '" y="' + (y + 3).toFixed(1) + '" fill="#6b7684" font-size="10" font-family="ui-monospace, monospace">' + price.toFixed(2) + "</text>");
    }
    parts.push('<line x1="' + padL + '" y1="' + priceBottom + '" x2="' + (padL + plotW) + '" y2="' + priceBottom + '" stroke="#2a3340" stroke-width="1"></line>');
    const bw = Math.max(slot * 0.62, 2);
    let nowX = null;
    visible.forEach((bar, i) => {
      if (!bar) return;
      const x = padL + (i + 0.5) * slot;
      const date = dates[start + i];
      const played = cursor && date <= cursor;
      const color = !played ? "#7d8794" : (bar.c >= bar.o ? "#16c784" : "#ea3943");
      const fade = played ? "" : ' opacity="0.45"';
      const bodyTop = yOf(Math.max(bar.o, bar.c));
      const bodyH = Math.max(yOf(Math.min(bar.o, bar.c)) - bodyTop, 1);
      parts.push('<line x1="' + x.toFixed(1) + '" y1="' + yOf(bar.h).toFixed(1) + '" x2="' + x.toFixed(1) + '" y2="' + yOf(bar.l).toFixed(1) + '" stroke="' + color + '" stroke-width="1.2"' + fade + "></line>");
      parts.push('<rect x="' + (x - bw / 2).toFixed(1) + '" y="' + bodyTop.toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + bodyH.toFixed(1) + '" fill="' + color + '"' + fade + "></rect>");
      if (i % 2 === 0) {
        parts.push('<text x="' + x.toFixed(1) + '" y="' + (priceBottom + 12) + '" fill="#6b7684" font-size="10" text-anchor="middle" font-family="ui-monospace, monospace">' + simDay(date) + "</text>");
      }
      const vh = maxVol > 0 ? ((bar.v || 0) / maxVol) * (volBase - volTop - 2) : 0;
      parts.push('<rect x="' + (x - bw / 2).toFixed(1) + '" y="' + (volBase - vh).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + Math.max(vh, 1).toFixed(1) + '" fill="' + color + '"' + fade + "></rect>");
      if (cursor && date === cursor) nowX = x;
    });
    const hover = Number(el.dataset.hover);
    const hy = Number(el.dataset.hy);
    if (visible[hover]) {
      const x = padL + (hover + 0.5) * slot;
      parts.push('<line x1="' + x.toFixed(1) + '" y1="' + priceTop + '" x2="' + x.toFixed(1) + '" y2="' + volBase + '" stroke="#9aa6b2" stroke-width="1" stroke-dasharray="3 3"></line>');
      if (hy >= priceTop && hy <= priceBottom) {
        parts.push('<line x1="' + padL + '" y1="' + hy.toFixed(1) + '" x2="' + (padL + plotW) + '" y2="' + hy.toFixed(1) + '" stroke="#9aa6b2" stroke-width="1" stroke-dasharray="3 3"></line>');
      }
    }
    const old = el.querySelector("svg");
    if (old) old.remove();
    el.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 ' + width + " " + height + '" width="' + width + '" height="' + height + '">' + parts.join("") + "</svg>");
    if (nowX == null) {
      now.hidden = true;
    } else {
      now.hidden = false;
      now.style.left = nowX.toFixed(1) + "px";
    }
    let shown = visible[hover] ? hover : -1;
    if (shown < 0 && cursor) {
      visible.forEach((bar, i) => {
        if (bar && dates[start + i] === cursor) shown = i;
      });
    }
    if (shown < 0) {
      for (let i = visible.length - 1; i >= 0; i--) {
        if (visible[i]) { shown = i; break; }
      }
    }
    const bar = shown >= 0 ? visible[shown] : null;
    if (!bar) {
      read.textContent = "";
      return;
    }
    const date = dates[start + shown];
    const played = cursor && date <= cursor;
    const tone = !played ? "mu" : (bar.c >= bar.o ? "up" : "dn");
    read.innerHTML = '<span class="' + tone + '">' + simDay(date) + "</span>  O " + bar.o.toFixed(2) + "  H " + bar.h.toFixed(2) + "  L " + bar.l.toFixed(2) + "  C " + bar.c.toFixed(2) + "  Vol " + simVol(bar.v);
  }
  function bootSim() {
    const node = document.getElementById("sim-data");
    if (!node) return;
    if (document.querySelector(".sim-idle")) {
      setInterval(() => {
        fetch("/api/simulation").then((res) => res.json()).then((next) => {
          if (next.status === "downloading" || next.status === "active" || next.status === "halted") location.reload();
        }).catch(() => {});
      }, 5000);
      return;
    }
    const data = JSON.parse(node.textContent);
    paintSimText(data);
    const dates = data.dates || [];
    let pinned = true;
    let origin = simWindow(dates.length, data.cursor ? dates.indexOf(data.cursor) : -1);
    let space = false;
    let drag = null;
    function paint() {
      const cursor = data.cursor || "";
      const at = cursor ? dates.indexOf(cursor) : -1;
      if (pinned) origin = simWindow(dates.length, at);
      document.querySelectorAll(".sim-plot").forEach((el) => {
        const symbol = el.getAttribute("data-symbol");
        paintSimPlot(el, dates, (data.series || {})[symbol] || [], cursor, origin);
      });
    }
    function endDrag() {
      drag = null;
      document.querySelectorAll(".sim-plot").forEach((el) => el.classList.remove("dragging"));
    }
    paint();
    document.querySelectorAll(".sim-plot").forEach((el) => {
      el.addEventListener("mousemove", (event) => {
        if (space) return;
        const rect = el.getBoundingClientRect();
        const slotW = Math.max((rect.width - 66) / 60, 1);
        const index = Math.floor((event.clientX - rect.left - 8) / slotW);
        el.dataset.hover = String(index);
        el.dataset.hy = String(event.clientY - rect.top);
        paint();
      });
      el.addEventListener("mouseleave", () => {
        delete el.dataset.hover;
        delete el.dataset.hy;
        paint();
      });
    });
    window.addEventListener("resize", paint);
    document.addEventListener("keydown", (event) => {
      if (event.code !== "Space") return;
      const tag = (event.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea") return;
      event.preventDefault();
      if (event.repeat) return;
      space = true;
      document.querySelectorAll(".sim-plot").forEach((el) => el.classList.add("space"));
    });
    document.addEventListener("keyup", (event) => {
      if (event.code !== "Space") return;
      space = false;
      document.querySelectorAll(".sim-plot").forEach((el) => el.classList.remove("space"));
    });
    window.addEventListener("blur", () => {
      space = false;
      endDrag();
      document.querySelectorAll(".sim-plot").forEach((el) => el.classList.remove("space"));
    });
    document.addEventListener("pointerdown", (event) => {
      if (!space || !event.target.closest(".sim-plot")) return;
      event.preventDefault();
      drag = { x: event.clientX, start: origin };
      document.querySelectorAll(".sim-plot").forEach((el) => el.classList.add("dragging"));
    });
    document.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const plot = document.querySelector(".sim-plot");
      const width = plot ? plot.clientWidth : 1;
      const slot = Math.max((width - 64) / 60, 1);
      const shift = Math.round((drag.x - event.clientX) / slot);
      const max = Math.max(0, dates.length - 60);
      origin = Math.min(max, Math.max(0, drag.start + shift));
      pinned = false;
      paint();
    });
    document.addEventListener("pointerup", endDrag);
    document.addEventListener("pointercancel", endDrag);
    setInterval(() => {
      fetch("/api/simulation").then((res) => res.json()).then((next) => {
        if (next.run_id !== data.run_id || next.bars !== data.bars || next.status !== data.status) {
          location.reload();
          return;
        }
        if (next.cursor !== data.cursor) {
          data.cursor = next.cursor;
          data.day = next.day;
          data.left = next.left;
          data.total = next.total;
          data.cursor_label = next.cursor_label;
          pinned = true;
          paint();
          paintSimText(next);
        }
      }).catch(() => {});
    }, 5000);
  }
  const skillTry = document.querySelector(".skill-try");
  if (skillTry) {
    const kind = document.getElementById("skill-play-kind");
    const urlBox = document.getElementById("skill-play-url");
    const body = document.getElementById("skill-play-body");
    const send = document.getElementById("skill-play-send");
    const out = document.getElementById("skill-play-out");
    skillTry.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-skill-tab]");
      if (!tab) return;
      const name = tab.getAttribute("data-skill-tab");
      skillTry.querySelectorAll("[data-skill-tab]").forEach((node) => {
        node.classList.toggle("on", node === tab);
      });
      skillTry.querySelectorAll("[data-skill-panel]").forEach((panel) => {
        panel.hidden = panel.getAttribute("data-skill-panel") !== name;
      });
    });
    let samples = {};
    const samplesNode = document.getElementById("skill-play-samples");
    try { samples = samplesNode ? JSON.parse(samplesNode.textContent || "{}") : {}; }
    catch (err) { samples = {}; }
    function paintPlayUrl() {
      const spec = kind ? samples[kind.value] : null;
      if (!spec || !urlBox) return;
      let path = spec.path;
      if (path.indexOf("{task_id}") >= 0) {
        const taskId = body ? body.value.trim() : "";
        path = path.replace("{task_id}", taskId ? encodeURIComponent(taskId) : "{task_id}");
      }
      urlBox.value = "http://127.0.0.1:8788" + path;
    }
    if (kind && body) {
      kind.addEventListener("change", () => {
        const spec = samples[kind.value];
        if (spec) body.value = spec.body;
        paintPlayUrl();
      });
      body.addEventListener("input", paintPlayUrl);
      paintPlayUrl();
    }
    function escapeSkill(text) {
      return text.replace(/[&<>]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[ch]));
    }
    function paintJson(node, value) {
      const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      try { JSON.parse(text); }
      catch (err) {
        node.textContent = text;
        return;
      }
      const token = /"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\b(?:true|false|null)\b|[{}\[\],:]/g;
      let html = "";
      let pos = 0;
      let match;
      while ((match = token.exec(text))) {
        html += escapeSkill(text.slice(pos, match.index));
        const piece = match[0];
        pos = match.index + piece.length;
        let kind = "jn";
        if (piece.charAt(0) === '"') kind = text.slice(pos).trim().charAt(0) === ":" ? "jk" : "js";
        else if ("{}[],:".indexOf(piece) >= 0) kind = "jp";
        else if (piece === "true" || piece === "false" || piece === "null") kind = "jb";
        html += '<span class="' + kind + '">' + escapeSkill(piece) + "</span>";
      }
      node.innerHTML = html + escapeSkill(text.slice(pos));
    }
    async function skillFetch(url, init) {
      const res = await fetch(url, init);
      const text = await res.text();
      try { return JSON.parse(text); }
      catch (err) { return { ok: false, status: "failed", error: text || "The console did not answer." }; }
    }
    if (send && kind && body && out) {
      send.addEventListener("click", async () => {
        const spec = samples[kind.value];
        const keyNode = document.querySelector("[data-secret]");
        const key = keyNode ? (keyNode.getAttribute("data-secret") || "") : "";
        if (!spec) return;
        if (!key) {
          out.textContent = "No key yet. Generate one on Settings.";
          return;
        }
        const headers = { Authorization: "Bearer " + key };
        let url = spec.path;
        const init = { method: spec.method, headers: headers };
        if (spec.method === "POST") {
          headers["Content-Type"] = "application/json";
          init.body = body.value;
        } else {
          url = "/api/v1/tasks/" + encodeURIComponent(body.value.trim());
        }
        send.disabled = true;
        send.classList.add("busy");
        try {
          let payload = await skillFetch(url, init);
          paintJson(out, payload);
          const taskId = payload && payload.task_id;
          if (spec.method === "POST" && taskId && payload.status === "running") {
            for (let step = 0; step < 60; step += 1) {
              await new Promise((resolve) => setTimeout(resolve, 1500));
              payload = await skillFetch("/api/v1/tasks/" + encodeURIComponent(taskId), {
                method: "GET",
                headers: { Authorization: "Bearer " + key }
              });
              paintJson(out, payload);
              if (!payload || payload.status !== "running") break;
            }
          }
        } catch (err) {
          out.textContent = "The console did not answer.";
        }
        send.disabled = false;
        send.classList.remove("busy");
      });
    }
  }
  function paintBookHeat() {
    const dataNode = document.getElementById("book-heat-data");
    const svg = document.querySelector("svg.book-heat");
    if (!dataNode || !svg) return;
    let groups = [];
    try { groups = JSON.parse(dataNode.textContent || "[]"); }
    catch (err) { return; }
    if (!groups.length) return;
    const ns = "http://www.w3.org/2000/svg";
    const font = "ui-monospace, monospace";
    function worst(row, side) {
      if (!row.length || side <= 0) return Infinity;
      const total = row.reduce((sum, value) => sum + value, 0);
      if (total <= 0) return Infinity;
      const high = Math.max.apply(null, row);
      const low = Math.min.apply(null, row);
      const side2 = side * side;
      return Math.max((side2 * high) / (total * total), (total * total) / (side2 * low));
    }
    function squarify(items, x, y, w, h) {
      if (w <= 0 || h <= 0 || !items.length) return [];
      const ranked = items.slice().sort((a, b) => b.area - a.area);
      const total = ranked.reduce((sum, item) => sum + item.area, 0) || 1;
      const areas = ranked.map((item) => (item.area / total) * (w * h));
      const out = [];
      let left = x, top = y, width = w, height = h, start = 0;
      while (start < areas.length) {
        const row = [];
        let index = start;
        while (index < areas.length) {
          const candidate = row.concat([areas[index]]);
          const side = Math.min(width, height);
          const current = row.length ? worst(row, side) : Infinity;
          if (!row.length || worst(candidate, side) <= current) {
            row.push(areas[index]);
            index += 1;
          } else break;
        }
        const rowSum = row.reduce((sum, value) => sum + value, 0) || 1;
        if (width >= height) {
          const column = height ? rowSum / height : 0;
          let cursor = top;
          for (let step = 0; step < row.length; step += 1) {
            const tileH = (row[step] / rowSum) * height;
            out.push({ x: left, y: cursor, w: column, h: tileH, ref: ranked[start + step] });
            cursor += tileH;
          }
          left += column;
          width -= column;
        } else {
          const rowH = width ? rowSum / width : 0;
          let cursor = left;
          for (let step = 0; step < row.length; step += 1) {
            const tileW = (row[step] / rowSum) * width;
            out.push({ x: cursor, y: top, w: tileW, h: rowH, ref: ranked[start + step] });
            cursor += tileW;
          }
          top += rowH;
          height -= rowH;
        }
        start += row.length;
      }
      return out;
    }
    function fit(text, width, size) {
      const room = Math.max(1, Math.floor(width / (size * 0.62)));
      if (text.length <= room) return text;
      return text.slice(0, Math.max(1, room - 1)) + "…";
    }
    function tip(tile) {
      const lines = [tile.name];
      if (tile.mark) lines.push("Mark " + tile.mark);
      if (tile.pct == null) lines.push("P&L —");
      else lines.push("P&L " + (tile.pct > 0 ? "+" : "") + tile.pct.toFixed(1) + "%");
      return lines.join(String.fromCharCode(10));
    }
    function el(name, attrs) {
      const node = document.createElementNS(ns, name);
      Object.keys(attrs).forEach((key) => node.setAttribute(key, attrs[key]));
      return node;
    }
    function text(x, y, value, fill, size, anchor) {
      const node = el("text", {
        x: x.toFixed(1), y: y.toFixed(1), fill: fill, "font-size": String(size), "font-family": font
      });
      if (anchor) node.setAttribute("text-anchor", anchor);
      node.textContent = value;
      return node;
    }
    let lastW = 0, lastH = 0;
    function draw() {
      const w = Math.floor(svg.clientWidth);
      const h = Math.floor(svg.clientHeight);
      if (w < 40 || h < 40 || (w === lastW && h === lastH)) return;
      lastW = w;
      lastH = h;
      svg.setAttribute("viewBox", "0 0 " + w + " " + h);
      svg.setAttribute("preserveAspectRatio", "xMinYMin meet");
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      squarify(groups, 0, 0, w, h).forEach((block) => {
        const group = block.ref;
        svg.appendChild(el("rect", {
          x: block.x.toFixed(1), y: block.y.toFixed(1),
          width: block.w.toFixed(1), height: block.h.toFixed(1),
          fill: "#10141a", stroke: "#3a4350"
        }));
        const header = block.h > 70 && block.w > 90 ? 22 : 0;
        if (header) {
          const sign = group.pct > 0 ? "+" : "";
          svg.appendChild(text(block.x + 8, block.y + 15, group.label, "#e7ecf3", 11));
          svg.appendChild(text(block.x + block.w - 8, block.y + 15, sign + group.pct.toFixed(1) + "%", group.ink, 11, "end"));
        }
        const innerW = block.w;
        const innerH = block.h - header;
        if (innerW <= 6 || innerH <= 6) return;
        squarify(group.tiles, block.x, block.y + header, innerW, innerH).forEach((cell) => {
          const tile = cell.ref;
          const gapX = cell.x + 2, gapY = cell.y + 2;
          const gapW = Math.max(0, cell.w - 4), gapH = Math.max(0, cell.h - 4);
          const rect = el("rect", {
            x: gapX.toFixed(1), y: gapY.toFixed(1),
            width: gapW.toFixed(1), height: gapH.toFixed(1), fill: tile.fill
          });
          const title = document.createElementNS(ns, "title");
          title.textContent = tip(tile);
          rect.appendChild(title);
          svg.appendChild(rect);
          if (gapW <= 32 || gapH <= 22) return;
          const size = Math.min(13, Math.max(9, Math.floor(gapW / 7)));
          svg.appendChild(text(gapX + 6, gapY + size + 4, fit(tile.name, gapW - 8, size), tile.ink, size));
          if (gapW <= 60 || gapH <= 44) return;
          let cursor = gapY + size + 4 + size + 2;
          if (tile.mark) {
            svg.appendChild(text(gapX + 6, cursor, tile.mark, tile.ink, size));
            cursor += size + 2;
          }
          if (tile.pct != null) {
            const sign = tile.pct > 0 ? "+" : "";
            svg.appendChild(text(gapX + 6, cursor, "Δ " + sign + tile.pct.toFixed(1) + "%", tile.ink, size));
          }
        });
      });
    }
    draw();
    requestAnimationFrame(draw);
    if (window.ResizeObserver) new ResizeObserver(draw).observe(svg);
  }
  paintBookHeat();
  bootSim();
  pollTasks();
  setInterval(pollTasks, 1000);
  pollCycle();
  setInterval(pollCycle, 1000);
  showFlashes();
})();
</script>
"""
