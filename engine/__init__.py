"""engine — the Phase 3 module pipeline (owner production path, 2026-09-11).

Data -> Features -> Regime -> Opportunity -> Scoring -> Risk -> Execution -> Analytics

Each module is pure computation: no network, no state mutation, no side
effects. tick.py remains the orchestrator (fetch/sync/execution); everything
decidable from candles and numbers lives here. Parity-proven against the
monolith on the NEAR 30-day regression set before shipping.
"""
