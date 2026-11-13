# Architecture documentation verification

This protocol verifies the architecture documentation. The atlas describes all eight completed phases and their component connections. No model, network service, new Python dependency, or database is
needed to view it.

## 1. Open locally

Open `docs/architecture.html` directly in a browser. Alternatively, from the
repository root:

```bash
python3 -m http.server 8765 --bind 127.0.0.1
```

Visit **http://127.0.0.1:8765/docs/architecture.html**. Expected: the architecture
atlas, eight phase buttons, component cards, a details panel, and zoom controls.
Stop the server with Ctrl+C when finished.

## 2. Check interactions

| Action | Expected result |
| --- | --- |
| Toggle Dark mode / Light mode | Canvas, components, panels, labels, and controls change theme; an explicit choice survives reload when storage is available. |
| Click + / − or scroll over the map | Zoom percentage and card scale change around the viewport/pointer. |
| Drag the canvas | The map pans; buttons and the details panel remain fixed. |
| Press F with map focus / click Fit | The visible selection or diagram fits the viewport. |
| Search `database.py`, then select the result | That file is focused; inputs, outputs, interfaces, and connections appear. |
| Click an incoming/outgoing connection | The connected component is revealed and inspected. |
| Click a phase button | That subsystem is highlighted and fitted. |
| Inspect the toolbar, cards and inventory | No implementation-status selector, planned legend or repeated implementation badges remain. |
| Enable Supporting files | The map shows all 80 components, including tests, package markers, setup, and documentation. |
| Choose Remember a fact / Recall context / The A/B experiment | The relevant connection path is highlighted and its explanation changes. |
| Click Export SVG | A standalone `dynagraph-architecture.svg` is downloaded. Open it as a vector image. |
| Click Reset | Restore the whole-system core map and clear filters/selection. |
| Use a narrow/mobile viewport | Controls wrap; the inspector moves below the map without horizontal page overflow. |
| Navigate cards with keyboard and press Enter | Component details open without requiring a pointer. |

The initial diagram includes the 32 core components. Supporting files add 48
nodes, for **80 components and 161 connections** in the complete atlas. Observation/support relationships are dotted; other connections are solid.
Components have wider horizontal and vertical gutters. Every connector receives
its own perimeter attachments. The router reserves tracks before drawing them:
no two lines share a collinear section, and all paths avoid card interiors.
Perpendicular crossings remain possible; a background halo makes them distinct
from shared junctions. Normal strokes are 2.55 units (previously 1.7) and
highlighted strokes are 3.75 units (previously 2.5), both exactly 50% thicker.

Labels on selected connections appear when zoom is at least 50% and there is
space outside component cards. Every connection is described in the inspector;
hovering a line also reveals its label.

## 3. Automated connector geometry checks

Viewing the atlas requires only a browser. Optional development checks use Node.js
with built-in modules and require no npm packages. From the repository root:

```bash
node docs/testing/architecture-routing.mjs
```

Expected: all seven geometry checks pass with no failures. They verify unique
ports on the actual rectangle perimeter, orthogonal nonzero segments, avoidance
of every card interior, no shared collinear tracks, separated parallel lines,
detours around an obstacle, and deterministic multiple/reverse connections.

The tests include all 161 connectors, even those hidden by filters. Zoom, theme,
phase, and workflow changes keep the same routes. This avoids new line collisions
when supporting files are revealed. Ordinary perpendicular crossings are allowed;
these are different from two lines occupying the same section.

## 4. Check offline behavior and scope

With the standalone HTML file open, disconnect WAN access and reload. Expected:
the same layout, controls, search, and SVG export without external requests.
The page uses inline CSS, JavaScript, SVG, and component data; no CDN or font
download is required. The browser console should show no JavaScript errors.

Review these architectural distinctions:

- All Python coordination and SQLite reside on Node 1; Node 2 serves Ollama only.
- All eight phases are implemented. Both the original 80-turn and integrated
  960-turn studies are complete. Their supporting cards open the reports.
  Synthetic tests and their figures remain separate from measured results.
- Standalone extraction does not write; the controller now persists accepted facts
  atomically with usage, pruning, and the turn checkpoint.
- Phase 4 governance is implemented; raw GraphStore CRUD bypasses its policy.
- Fact-level SUPERSEDES events are distinct from semantic entity edges.
- Phase 5 recall is active-only and budgeted; byte bounds are not measured tokens.
- Performance targets and experiment hypotheses are not presented as results.
- Public documentation contains no real deployment IPs, hostnames, usernames,
  absolute checkout paths, or references to private implementation notes.

## 5. Repository checks

```bash
git diff --check
git status --short
git diff --stat
git check-ignore .env docs/local/
```

Expected: no whitespace errors. Review changes against the active phase; atlas
updates can accompany runtime implementation. `.env` and `docs/local/` remain
ignored. Viewing the atlas does not change runtime settings or database files.

Browser verification during development covers offline load, node inventory,
search/reveal, pointer selection, zoom, pan, phase/workflow filters,
theme persistence and storage-unavailable fallback, themed SVG serialization,
mobile layout, connector geometry, and console/network errors. Documentation-only
edits do not require backend tests; runtime changes follow their phase protocol.

## 6. Commit message

When atlas changes accompany other documentation work, use that step's message.
The final readability refactor uses the message in the
[Step 6 protocol](refactor_step6.md).
