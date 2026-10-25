# Architecture documentation verification

This is a documentation update, not an implementation of a later runtime phase.
The atlas describes all eight phases and distinguishes current code from planned
components. No model, network service, new Python dependency, or database is
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
| Click + / − or scroll over the map | Zoom percentage and card scale change around the viewport/pointer. |
| Drag the canvas | The map pans; buttons and the details panel remain fixed. |
| Press F with map focus / click Fit | The visible selection or diagram fits the viewport. |
| Search `database.py`, then select the result | That file is focused; inputs, outputs, interfaces, and connections appear. |
| Click an incoming/outgoing connection | The connected component is revealed and inspected. |
| Click a phase button | That subsystem is highlighted and fitted. |
| Choose Implemented only | No planned component is displayed. Local current inference/storage resources remain visible. |
| Enable Supporting files | The map shows all 55 components, including tests, package markers, setup, and documentation. |
| Choose Remember a fact / Recall context / The A/B experiment | The relevant connection path is highlighted and its explanation changes. |
| Click Export SVG | A standalone `dynagraph-architecture.svg` is downloaded. Open it as a vector image. |
| Click Reset | Restore the whole-system core map and clear filters/selection. |
| Use a narrow/mobile viewport | Controls wrap; the inspector moves below the map without horizontal page overflow. |
| Navigate cards with keyboard and press Enter | Component details open without requiring a pointer. |

The initial diagram includes the 29 core components. Supporting files add 26
nodes, for **55 components and 87 connections** in the complete atlas. Lines for
planned integration are dashed; observation/support relationships are dotted.
Labels on selected connections appear when zoom is at least 50% and there is
space outside component cards. Every connection is described in the inspector;
hovering a line also reveals its label.

## 3. Check offline behavior and scope

With the standalone HTML file open, disconnect WAN access and reload. Expected:
the same layout, controls, search, and SVG export without external requests.
The page uses inline CSS, JavaScript, SVG, and component data; no CDN or font
download is required. The browser console should show no JavaScript errors.

Review these architectural distinctions:

- All Python coordination and SQLite reside on Node 1; Node 2 serves Ollama only.
- Phases 1–3 are implemented; phases 4–8 and their connecting integration paths
  remain planned.
- Extraction returns validated facts without automatically writing them.
- Stored status/pin fields are not implemented decay/supersession algorithms.
- Performance targets and experiment hypotheses are not presented as results.
- Public documentation contains no real deployment IPs, hostnames, usernames,
  absolute checkout paths, or references to private implementation notes.

## 4. Repository checks

```bash
git diff --check
git status --short
git diff --stat
git check-ignore .env docs/local/
```

Expected: no whitespace errors; this step changes only `README.md` and the three
architecture documentation files. `.env` and `docs/local/` remain ignored. No
runtime source, model settings, requirements, or database files are changed.

Browser verification during development covers offline load, node inventory,
search/reveal, pointer selection, zoom, pan, phase/status/workflow filters,
SVG serialization, mobile layout, and console/network errors. Backend tests need
not be rerun for this documentation-only change; their protocols are unchanged.

## 5. Commit message

```text
docs: add interactive full-project architecture atlas

Map all eight phases with component responsibilities and integration paths.
Add offline pan/zoom, search, phase focus, details, and SVG export.
Distinguish implemented code from planned modules and document the workflow.
```
