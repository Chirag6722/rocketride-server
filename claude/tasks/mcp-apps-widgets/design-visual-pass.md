# Widget Visual Pass — Design Brief (2026-08-06)

Approved in-session by Dylan (brainstormed, then "implement it"). Scope: the two
surviving MCP Apps widgets (dropper, pipelines-table). No structural/behavioral
changes — CSS plus small markup additions only.

## Decisions

1. **Direction: host-adaptive + brand accents.** Neutrals keep following the
   host theme (`color-scheme: light dark`, `currentColor`/`color-mix`); the
   RocketRide identity appears only in accents and the logo mark.
2. **Scope: both widgets, shared mini design-system** under
   `apps/mcp-widgets/src/shared/` (`theme.css` + `brand.ts`), imported by each
   widget and inlined by the singlefile build.
3. **Accents (official palette from github.com/rocketride-org/branding —
   NOT dropper-ui's stale orange/navy):**
   - Working accent: Horizon Blue `#41b6e6` — tabs, buttons, focus, hover.
   - Flame `#F93822` (the logo's spark) reserved for "energy" moments:
     upload-progress fill, dropzone drag-over, error accents.
   - Abyss Blue `#1e1a34` available as a dark-surface tint.
4. **Branding: small header mark.** The 1.5 KB icon SVG inlined at ~18px in a
   slim header row (mark + widget title). The swoosh path is recolored to
   `currentColor` so it adapts to light/dark hosts; the flame path stays
   `#F93822`.
5. **Typography: system stack** (no font files exist in the branding repo;
   CSP forbids external fonts anyway).

## Per-widget

- **Dropper**: header "RocketRide Dropper"; dashed dropzone → Horizon Blue on
  hover, flame on drag-over; flame-gradient progress fill; Horizon Blue active
  tab pill + tinted badges; result cards with muted mono captions and inset
  code surfaces; failed-object cards get a flame left bar.
- **Pipelines table**: header "RocketRide Pipelines"; muted uppercase column
  labels, hairline dividers, row hover tint, mono tokens; Terminate = quiet
  danger button, Refresh = ghost accent button; branded empty state.
