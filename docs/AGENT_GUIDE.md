# Coding Agent Guide

## Read order

Before implementing any issue, read:

1. README.md
2. docs/PRODUCT_SPEC.md
3. docs/ARCHITECTURE.md
4. docs/TRADING_ASSUMPTIONS.md
5. docs/TEST_GATES.md
6. target GitHub Issue

Do not implement an issue in isolation.

## Current scope is Paper-only; future Live is planned

The project is designed to reach Live trading in a later milestone. This rule applies only to the **current Paper milestone** and is mandatory until the Future Live milestone is explicitly started.

Do NOT:

- implement or call kabuステーションAPI
- implement or call moomoo API
- add KabuStationBroker / MoomooBroker
- authenticate to a brokerage account
- read real brokerage positions/orders/balances
- submit real orders
- implement Shadow broker connectivity
- add live-order arming logic as executable functionality

You should preserve generic interfaces that make future Live adapters possible, because Shadow/Live is part of the long-term roadmap. However, only PaperBroker/FakeBroker are implemented in the current milestone.

If an issue or old comment conflicts with this rule, this document and the latest issue text take precedence.

## Architecture invariants

Never:

- call PaperBroker directly from Jev / ML / DecisionModel
- use future information in Replay
- hide external SDK types inside core domain objects
- silently reverse prohibited actions
- bypass RiskEngine
- hard-code unresolved experimental thresholds

## Coding standards

- Python 3.12
- uv
- src layout
- Pydantic
- Polars
- DuckDB / Parquet
- pytest
- Ruff
- pyright
- FastAPI where needed

Maintain:

- unit tests
- integration tests
- deterministic replay tests
- typed interfaces
- structured logging
- explicit config
- no secrets

## Mandatory Test Gates

docs/TEST_GATES.md defines 5 mandatory gates.

Rules:

1. Each issue still needs its own unit/integration tests.
2. A Test Gate is broader than an issue's tests.
3. Do not proceed past a gate boundary while the gate is failing.
4. Record the command/config/dataset/commit used for gate verification.
5. A profitable strategy result does not override a failed data/replay/execution gate.

## Decision process

If a local implementation detail is unspecified, choose the simplest design consistent with docs.

If a choice changes architecture, data semantics, trading assumptions, or future live safety:

1. do not silently decide
2. document proposal
3. update ADR/issue
4. preserve Paper-only behavior for the current milestone while keeping future Live extensibility

## Definition of done

Implementation must:

- satisfy issue acceptance criteria
- pass issue-level tests
- pass the relevant Test Gate when reaching a gate boundary
- preserve replay determinism
- emit audit metadata
- avoid future leakage
- never require a brokerage API in current milestone
