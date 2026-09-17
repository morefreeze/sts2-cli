# Vendored from Combat Solver

Source: https://github.com/Torch1230/CombatSolver (MIT license)
Vendored: 2026-09-17, from `main` branch, commit at clone time.
Scope: Engine/, Search/, Prediction/, Strategy/, and Runtime/CombatRootSnapshot.cs only.
NOT vendored: Runtime/ (remainder), UI/, Api/, Diagnostics/, Replay/, Testing/ —
these are the live-mod / RitsuLib / overlay glue this headless integration
does not need. See docs/superpowers/specs/2026-09-17-combatsolver-port-design.md.

Do not hand-edit ported files' algorithm logic. If upstream fixes a bug we
need, re-vendor the affected file(s) from a fresh clone instead of patching
by hand, so we don't silently diverge from upstream.
