# ADR 0005 — The dashboard package over neutral chart modules

Status: accepted (4.2.0). Applies to `src/nextdns_mcp/tools/dashboard/` (the package)
and `src/nextdns_mcp/charts.py` and `src/nextdns_mcp/chart_ui.py` (the shared modules
neither analytics surface owns).

## Context

The interactive dashboard grew in one file. By the time of the split,
`src/nextdns_mcp/tools/dashboard.py` had passed 1,500 lines holding four different
concerns: transport calls, the bounded fan-out, KPI math and row shapers; the Prefab
cards, charts and page views; the summary text a model reads back beside each card; and
the per-section dispatch with its published wrapper.

Its time-series primitives lived in the plotting sibling, `tools/plots.py`, imported
across the module boundary by `_`-prefixed name. That made a rename inside the plotting
tool a silent break in the dashboard, and it left a thousand-line module reaching into a
peer for private helpers — the defect this split was filed for, recorded in the boundary
test's own docstring (`tests/unit/test_dashboard_boundaries.py:1-10`, and the history of
the primitives in `src/nextdns_mcp/charts.py:1-14`).

A move alone would not hold. The second problem was coupling, not size:
`spec_tools` — the door every generated tool stands behind — needs the series budgets
to price a request, and the primitives lived in a module whose dependency chain pulled
the Prefab UI component tree into every process that renders nothing (issue #550). And
the first attempt at sharing left the coupling wearing a public name: the primitives
re-aliased at the bottom of `tools/plots.py` under a fresh name, imported by the alias.

## Decision

**One published tool, one package, one concern per module; the primitives move out of
both consumers into two neutral modules; and the boundary is enforced by a test that
reads the source, because a split nothing defends collapses back the first time someone
needs a card in a hurry.**

- **The package** is `src/nextdns_mcp/tools/dashboard/` with four modules, one concern
  each: `data` (transport calls, the bounded fan-out, KPI/rate math, row shapers),
  `components` (the Prefab cards, charts, tables and page views), `formatters` (the
  summary text beside each rendered card), and `tool` (one implementation per section,
  the `section` dispatch, and the single published wrapper). Names beginning with `_` are
  package-internal; the surface is imported from the package
  (`src/nextdns_mcp/tools/dashboard/__init__.py:1-23`).
- **The shared primitives** live outside both analytics surfaces: `src/nextdns_mcp/charts.py`
  is pure — range resolution, payload shape, rows, series budgets — and imports nothing
  from the Prefab UI component library; `src/nextdns_mcp/chart_ui.py` holds the Prefab
  chart builders. Neither the dashboard package nor `tools/plots.py` owns either module,
  and both cross into them under public names only
  (`src/nextdns_mcp/charts.py:1-14`, `src/nextdns_mcp/chart_ui.py:1-14`,
  `src/nextdns_mcp/tools/dashboard/__init__.py:12-14`).
- **The boundary test** (`tests/unit/test_dashboard_boundaries.py`) pins the layout the
  split chose: the god module stays a package; no dashboard module imports a sibling tool
  module; nothing in the package imports a `_`-prefixed name from the shared modules; the
  shared primitives are defined in their own modules — a top-level re-alias under
  `tools/` is the coupling again and fails; `charts.py` stays free of `prefab_ui`; only
  `chart_ui.py` constructs an `AreaChart` (issue #439); and the package exports
  `overviewDashboard` as its public surface.

## Consequences

**Gained**

- File-level ownership matches the concern: a change to one section touches the module
  that owns that concern, not a 1,500-line grab-bag.
- `spec_tools` and every generated tool behind it can price a series request without
  importing the component tree.
- The primitives have one home and public names, so a rename is a deliberate edit in the
  owning module rather than a silent break in a consumer.
- The boundary survives the next "quick card": the layout, the import rules, and the
  single-`AreaChart`-factory rule are all asserted in CI.

**Accepted costs**

- One section spans several modules — fetch, shape, render, summarize — so most section
  work touches two or three files in the package. The modules exist because those four
  jobs change for different reasons, not to make a section cheap to edit.
- The dashboard package may not construct a Prefab chart itself: it renders through
  `build_timeseries_area_chart` in `chart_ui`, and re-inlining the component is what the
  test exists to catch (issue #439).
- The boundary test reads source text rather than exercising behaviour, so moving a
  module or widening an import is a deliberate edit to the test, not a silent one.
- Neither consumer owns the shared modules, so a change to the primitives has to be made
  with both analytics surfaces — and `spec_tools`'s series pricing — in mind.

## History

- `a09d8fa` (#477, 4.2.0): `tools/dashboard.py` (~1,504 lines) is split into
  the package over a new neutral `charts.py`; the boundary test arrives with it, and the
  suites are repointed at their new owners. The branch carried the main-line fixes that
  landed while it was open into the modules that now own them, per the merge record.
- `d503847` (#579): the Prefab chart builders split out of `charts.py` into
  `chart_ui.py` (issue #550), keeping the pure half free of the component library.
- `tests/unit/test_dashboard_boundaries.py` records the coupling this replaced —
  private-name imports across the plotting boundary — and is the line the package may
  not cross back over.
- Issue #780: the section vocabulary (`DashboardSection`, `DASHBOARD_SECTIONS`) moved
  out of `tool.py` into a fifth module, `constants.py`, and the package `__init__`
  resolves `overviewDashboard` lazily, so metadata consumers (the E2E sweep, the
  schema/catalog readers) load no Prefab UI component tree. `server.py` likewise
  imports the composite tool functions at the registration site, publishing their
  names as static metadata from `catalog.py`.
