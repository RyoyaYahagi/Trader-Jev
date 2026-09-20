# Coding Agent Guide

## Read order

Before implementing any issue, read:

1. README.md
2. docs/PRODUCT_SPEC.md
3. docs/ARCHITECTURE.md
4. docs/TRADING_ASSUMPTIONS.md
5. target GitHub Issue

Do not implement an issue in isolation from these documents.

## Architecture invariants

Never:

- call Broker directly from Jev / ML / DecisionModel
- hide market-specific SDK types inside core domain objects
- use future information in Replay
- silently fall back from prohibited LONG to SHORT or vice versa
- place Live orders on model/API/data errors
- enable Live by default
- hard-code experimental thresholds that are intentionally unresolved in TRADING_ASSUMPTIONS.md

## Coding standards

Default stack:

- Python 3.12
- uv
- src layout
- Pydantic
- Polars
- DuckDB / Parquet
- pytest
- Ruff
- pyright
- FastAPI where an API is needed

Maintain:

- unit tests
- integration tests for adapters
- deterministic replay tests
- typed public interfaces
- structured logging
- explicit config
- no secrets in source/logs

## Decision process

If an issue leaves a local implementation detail unspecified, choose the simplest implementation consistent with the docs.

If a choice would materially change architecture, trading assumptions, data semantics, or Live safety:

1. do not silently decide it
2. document the proposed change
3. add/update an ADR or issue note
4. keep the default behavior conservative

## Definition of done

An implementation is not complete only because it runs.

It must also:

- satisfy issue acceptance criteria
- have tests for failure modes
- preserve replay determinism where applicable
- emit enough metadata for later audit
- avoid future leakage
- keep Live disabled unless explicitly armed
