# A-Share Mainline Research and Decision Support

[![CI](https://github.com/Taosheng777/a-share-mainline-os/actions/workflows/ci.yml/badge.svg)](https://github.com/Taosheng777/a-share-mainline-os/actions/workflows/ci.yml)
[![Live smoke](https://github.com/Taosheng777/a-share-mainline-os/actions/workflows/live-smoke.yml/badge.svg)](https://github.com/Taosheng777/a-share-mainline-os/actions/workflows/live-smoke.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[中文](README.md)

`a-share-mainline-os` is a Claude Code and Codex Skill suite for researching China A-share market themes. It organizes daily review, vehicle screening, and position analysis around a theme lifecycle while separating user-owned rules, AI recommendations, and user execution.

> For research and decision support only. The provider is not a licensed securities investment advisory institution. Neither this project nor its output constitutes investment advice, and it does not place orders. Users assume all risk. Data sources and as-of dates are those reported by each runtime output.

## Skills

| Skill | Purpose | Boundary |
|---|---|---|
| `stock-daily` | Review the latest trading day, market regime, themes, and position rules | A mechanical candidate pool is never presented as a formal nomination |
| `stock-screener` | Find and rank ETFs and leading stocks for an established theme | Screening does not decide account fit or trades |
| `stock-buddy` | Challenge, monitor, and review themes or positions; run five-dimensional expert mode only when explicitly enabled | No order placement; no fabricated target range or personalized sizing when evidence is insufficient |

## What is different

- A complete theme lifecycle with pre-registered failure conditions and an independent decline-review gate.
- **Two-key exit conditions and a `warning` stage.** A liquidate-level failure condition must fire on both a *fund key* and a *price key*. A fund key alone only reaches `warning` — freeze additions, propose a raised stop for the user to confirm — and never triggers the liquidate rule. Pure fund-sign conditions have a high false-positive base rate, so they should not pull an irreversible trigger on their own.
- **Deterministic, replayable classification.** The mechanical decision runs through a truth table in `mainline_validation.py`; a missing key returns `unverified` instead of silently reading as "not triggered". A structured case set ships with the repo, and changing the semantics requires updating the cases in the same change.
- **The system looks back after it decides.** Decline-review criteria are pre-registered at theme creation, every decline decision enters a T+N ledger classified by those pre-registered criteria, and a revival sentinel flags a possible false kill when an archived theme's fund flow reverses. The sentinel is a prompt to re-evaluate, **not a buy signal**, and a "false kill" label never rolls back discipline actions already executed.
- **Mandatory false-positive backtest** before a failure condition is accepted: replay the proposed liquidate-level combination over the last 60 trading days using the local board history cache; more than one hit means rewrite the condition.
- A three-layer contract: user-owned discipline facts, explicit AI recommendations, and user-confirmed execution.
- Anti-anchoring handoff between a mechanical candidate pool and independent formal nominations.
- Explicit multi-source degradation and dual-source market-breadth reconciliation.
- Zero catch-up debt after interruptions: resume from the latest valid trading day without inventing missing daily logs.
- Every material market number carries a source and as-of date; missing evidence remains missing.

## Quick start

```bash
git clone https://github.com/Taosheng777/a-share-mainline-os.git
cd a-share-mainline-os
```

Codex:

```bash
codex plugin marketplace add Taosheng777/a-share-mainline-os
codex plugin add a-share-mainline-os@a-share-mainline-os
```

Claude Code:

```text
/plugin marketplace add Taosheng777/a-share-mainline-os
/plugin install a-share-mainline-os@a-share-mainline-os
```

The tested local installers are also available:

```bash
bash adapters/codex/install.sh   # or: bash adapters/claude/install.sh
```

Preview and perform an explicit upgrade with:

```bash
bash adapters/codex/install.sh --upgrade --dry-run
bash adapters/codex/install.sh --upgrade
```

The upgrade replaces only the three Skills and creates one batch backup. It never reads or modifies the user config or vault. See [Upgrade and rollback](docs/升级与回滚.md), then run `python3 scripts/doctor.py --platform codex` for a read-only readiness check.

Create an empty vault and configuration:

```bash
cp -R vault-template "$HOME/a-share-mainline-vault"
mkdir -p "$HOME/.config/a-share-mainline"
cp config/config.example.json "$HOME/.config/a-share-mainline/config.json"
```

Replace `__VAULT_ROOT__` with the absolute path above, then fill in the three user-owned fields in `01-纪律卡.md`. Empty thresholds fail closed and are never inferred from old notes or model memory.

Install the external free data Skill:

```bash
python3 adapters/shared/install_a_stock_data.py \
  --target "$HOME/.codex/skills/a-stock-data"
```

Claude Code users should target `$HOME/.claude/skills/a-stock-data`. The installer pins and verifies upstream `v3.6.1`, its commit, `SKILL.md` digest, and Apache-2.0 license. This repository does not vendor or relicense upstream source.

Start a new task with prompts such as:

- `Review the latest A-share trading day.`
- `Screen ETFs and leading stocks for my established theme.`
- `Use stock-buddy to review this theme.`

To reproduce the clean-environment contract locally:

```bash
python3 scripts/cold_start.py
```

Both platform distributions are deterministically exported from the public canonical source in `src/skills/`. A public clone can verify both generated trees with `python3 scripts/export_from_source.py --check`; no maintainer-private directory is required.

## Data and licensing boundary

`a-stock-data` is the default external free data layer. `hithink-finance`, iWenCai, local iFinD evidence, and news or report search tools are optional user-installed adapters. Their absence must be reported explicitly and must never trigger a fallback to an author's private directory or invented data.

`stock-daily/scripts/board_fund_flow_cache.py` is **first-party code in this repository**, not part of upstream `a-stock-data`. It stores per-day board net flows, the four order-size tiers, and board closing levels in a local SQLite database (location set by `A_STOCK_DATA_HOME`), which is what makes MA20 price keys, multi-day conditions, false-positive backtests, and T+N ledger backfill possible. **The cache accumulates day by day, so a fresh install has almost no history**; when coverage is insufficient, every dependent check is written as "not verified" rather than defaulting to "not triggered", and the rest of the review still completes.

Original code and documentation in this repository are licensed under the [MIT License](LICENSE). Data interoperability uses Simon Lin's external [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data), licensed under Apache-2.0. See [NOTICE](NOTICE) and the [dependency license audit](docs/依赖许可证清单.md).

No market data, filings, research reports, news content, credentials, API keys, brokerage screenshots, or private account data are distributed. Public web endpoints are not a grant of official API or redistribution rights; interfaces may change, be rate-limited, or fail. Users are responsible for provider terms, permissions, and request frequency.

Run `python3 scripts/run_ci.py` to reproduce the offline release gate. The scheduled `Live smoke` only checks the pinned `a-stock-data` upstream revision and a public Tencent quote path. A failure preserves logs and opens an issue, but does not block offline CI or a release.

Contributions must follow [CONTRIBUTING.md](CONTRIBUTING.md). Report credential, private-data, path-traversal, or command-injection issues privately as described in [SECURITY.md](SECURITY.md).
