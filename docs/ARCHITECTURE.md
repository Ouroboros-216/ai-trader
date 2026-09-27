# Interfaces, recovery and verification

## Ownership and boundaries

This directory is independent of the existing `xau` package. Python 3.11+ standard library only; Gemini and OpenAI are implemented providers. MT5 is the only component with broker execution authority. Each account is bound by exact login, server, magic and selected demo/real mode. Contest accounts and Strategy Tester initialization are refused by this EA. Existing profiles default to demo until explicitly changed in setup.

The Python service calls a small `AIProvider.call(kind, payload)` interface through `build_provider`. Gemini and OpenAI implement the same strategy/decision/chat JSON contract without changing the EA or bridge. Other `provider.kind` values are rejected until their adapter, credential handling and tests are installed. API usage plus saved decisions provide an audit trail; they do not update the model weights or train a new trading model.

Multi-account operation uses one Python process and bot with an independent EA instance, account/server identity, Common Files subdirectory, service database, strategy version and risk limits per MT5 account. Each command is routed to one named account. A batch strategy proposal shares the trading rules but translates symbols and preserves each account's risk settings; it pauses all accounts after confirmation. Each account must then be resumed separately. Commission inputs are per account and may differ by symbol; a single value is only a convenience when a broker charges the same round-trip amount across symbols.

Both programs run as the same Windows user. The bridge is a trusted local-files boundary, **not** an authenticated remote API. Do not share write access with unrelated users/processes. The EA exclusively locks an account/magic file across local terminals; Python takes an OS byte-range lock on each bridge. This does not coordinate two Windows users or two VPS machines. Cross-machine control remains future work.

The setup window writes each account's binding and a Common Files account index when settings are saved. The EA may use its default login `0` and empty server to resolve its own account from that index, but still requires the exact logged-in account, server, mode and matching magic. A bridge directory holds only one account binding. The setup mode selector sets the account's live-entry permission; the existing policy and resume confirmation still apply.

## Wire interfaces

All files use UTF-8. Producer writes temporary files and atomically renames single-record snapshots. CSV wire fields cannot contain comma, quote or newline; symbols are restricted tokens. Journal files are append-only, flushed by the producer. Python ignores an incomplete trailing line and persists byte offsets only after ingestion; truncated/missing consumed journals are errors. JSON results are deduplicated and applied to SQLite in one transaction.

| File | Producer | Contract |
| --- | --- | --- |
| `snapshot.json` | EA, every 5 seconds | MarketSnapshot schema 1: UTC observation time; identity; demo flag and account mode; equity/balance; risk state; all positions with immutable broker position identifier and owned flag; symbol quotes/specs; last 100 completed bars of M5/M15/H1/H4 |
| `catalog.json` | EA, at start and periodically | Account-bound list of broker symbols. Python matches strategy names across the catalog and asks on ambiguous suffixes; confirmed policy symbols become active chart data. |
| `policy.csv` | Python | `1,account,server,magic,version,expires,enabled,direction,risk_pct,total_pct,daily_pct,dd_pct,symbols_pipe_separated,reset_nonce,resume_nonce,sizing_mode,sizing_value` |
| `command.csv` | Python | `1,id,account,server,magic,policy_version,expires,action,symbol,position_identifier,sl,tp` |
| `results.jsonl` | EA | ExecutionResult: `id,status,retcode,detail,time,account,server,latency_ms`; status DONE/PARTIAL/REJECTED/UNCERTAIN. Older rows without latency remain readable. |
| `deals.jsonl` | EA | Broker deal/position IDs, entry classification, volume, realized profit, commission, swap and fee; includes manual exits of system-owned positions found through history |
| `ui.jsonl` | EA | Unique event ID, identity, UTC time, action and exact displayed confirmation ID; chart chat carries bounded text and magic |
| `status.json`, `panel.txt`, `pending.csv` | Python | Human-readable status and current proposal ID; never broker commands |
| `risk.csv`, `position-*.risk`, `used.txt` | EA | Persistent equity baselines/latches, initial reserved stop risk and pre-execution command ledger |

`StrategyPolicy` is immutable in Python and stored by confirmed version. Every strategy card includes entry, invalidation, management and terminology definitions. The AI can propose policies, but only an explicit authenticated confirmation promotes one. Changing a policy pauses entries. A second confirmation enables trading. A symbol with owned open exposure cannot be removed from policy. The panel and Telegram share one proposal store; superseded, expired or used IDs cannot apply.

`自動模式` drafts a strategy from symbols with at least 20 completed M15 and H1 bars, with bounded available H4 history. Drafting does not require a fresh quote or known commission. It does not activate entries by itself; resume and order submission retain their stricter checks. The model can define several named methods with conditions; later reviews choose only within the confirmed card. User risk percentages are changed only through a confirmed policy proposal; a risk-only message edits the relevant field without an AI call when a policy exists. This is recurring inference, not model training or permission for automatic strategy rewrites.

`DecisionProposal` allows WAIT/HOLD/BUY/SELL/CLOSE/TIGHTEN/REVERSE. BUY/SELL need valid absolute SL/TP, reason, invalidation and management plan. Position management targets broker `POSITION_IDENTIFIER`, not a potentially changed ticket. The EA resolves it to the latest ticket and independently verifies magic/symbol ownership.

Before dispatching each BUY/SELL command, including a reversal follow-up, Python makes a second provider call with the confirmed policy, candidate decision and latest snapshot. The model can only return a boolean approval and reason; malformed, denied or failed reviews reject the command. Shared API cooldown can defer the queued command only until its original expiry. After approval, Python reloads the snapshot, rejects a newly completed strategy-timeframe bar or expired authority, and validates the original decision again. The EA then performs its independent broker-side gates. Each candidate entry can therefore use an additional billable API call; the review is an LLM judgment, not a formal proof that discretionary entry rules hold.

## Risk and execution

Initial defaults: risk per entry 0.5% equity, total initial stop-risk 1.5%, daily equity loss 2%, peak equity drawdown 5%. The user can confirm percentages in the 0.01%–100% range, with total initial stop-risk at least as large as per-entry risk when percentage sizing is used. Single-entry sizing can instead use a maximum estimated stop loss in account currency or exact fixed lots; both retain the total-risk, daily-loss and drawdown thresholds. The last effective thresholds persist across restarts. Unknown/corrupt persisted state prevents new risk and attempts protective closure of owned positions. UTC daily latch resets at the next UTC date; total latch requires an explicit confirmed reset while flat. Deposits/withdrawals are not adjusted out of equity calculations: use a dedicated account with stable capital during validation.

The EA uses `OrderCalcProfit` in account currency, bid/ask, slippage allowance and configured round-trip commission per lot to estimate stop loss. For percentage or cash sizing it floors to the broker volume step and never raises to minimum lots. Fixed-lot sizing requires an exact broker-valid volume and rejects if its estimated loss exceeds the remaining total-risk budget. All modes account for existing risk and check free margin with `OrderCalcMargin` before sending. One position/order per symbol blocks additional entries. Existing unrelated risk may block new trading but is never deliberately closed by the agent. Native broker execution is still subject to gaps, fills and slippage; stop-risk budgets are engineering estimates, not loss guarantees.

Each account's snapshot carries live spread in points, spread limit, slippage budget and round-trip commission. The service also summarizes the last 40 execution results by symbol, including rejection counts and median/p90 synchronous broker-call durations. Those observations enter only that account's decision prompt and audit record. They are not trained weights or a guarantee of future fill speed. Unknown commission or excessive spread blocks new entries at the EA, regardless of AI output; no costs, lot sizes or trades are copied between accounts.

Initial risk is reserved conservatively for the full requested amount even after a partial fill; it remains charged until the position is flat. Tightening stops does not free that initial allocation. Missing initial-risk files block subsequent entries. An ambiguous entry result pauses new entries and requires account inspection; commands are never automatically resubmitted.

EA journals a command ID **before** a broker call. Python marks a command sent **before** publishing it. Either side can therefore lose an intended operation on a crash, but neither retries an uncertain operation blindly. A timed-out sent command becomes UNCERTAIN and pauses new entries. Broker results and position/deal snapshots permit investigation. This is at-most-once attempt behavior, not a claim of exactly-once broker execution.

REVERSE is two distinct commands: first CLOSE; only a DONE result, a newer snapshot confirming no position on the symbol, an unexpired original decision, unchanged policy, allowed direction and valid new price geometry permit the new entry. PARTIAL/UNCERTAIN/REJECTED never trigger a reversal follow-up. The EA rechecks current sizing/margin/risk before the new order.

On API failure or service loss, the EA's broker stops and tick/timer protection continue. It cannot close while disconnected or without trading permission, and retries protection on later events. Pausing new entries keeps AI CLOSE/TIGHTEN analysis active for existing owned positions; stopping Python leaves only local EA/broker protection.

## AI and Telegram

Gemini calls use HTTPS, a fixed official endpoint, `x-goog-api-key`, JSON response mode and strict local parsing. HTTP redirects are refused. Requests are reserved in SQLite before networking; timeouts count toward local daily quota. Provider 429 responses create a persisted 15-minute backoff. Model ID, limits and timeout are configured; no provider fallback or billing upgrade occurs. API cost remains null when unavailable; token counts and latency come from response metadata and local timing.

Inputs include strategy text, market/account state and bounded recent decisions/executions/conversation. Full previous market snapshots are not recursively included in memory. News, uploads, images and indicator plugins are not implemented. External data and model response text cannot invoke operating-system commands or modify policy directly. The local execution gates enforce ownership, action allowlist, direction and risk even if the LLM fails to follow its prompt.

Telegram accepts an explicitly paired private user/chat pair. Messages older than two minutes are ignored. Confirmation callbacks are bound to proposal IDs with a ten-minute lifetime and the current base policy version. Update offset is committed before executing user commands, preferring a lost command to duplicated operations on crash. Bot credentials remain in environment variables; no raw network exception strings are written to service logs.

## Research records and limitations

SQLite records inputs, validated decisions, provider responses, executions, deals, equity samples, proposals, confirmations and conversations. `report` aggregates partial exit deals by position identifier and deducts reported commission/swap/fees. It computes sampled equity drawdown, not the precise tick-level maximum. Elapsed calendar days do not establish service uptime. The report's 28-day/100-closed-position indicator is a minimum observation check, never live approval.

`replay` revalidates recorded decisions against saved input snapshots without network calls. It does not simulate prices, fills, margin or slippage and must not be described as a historical return backtest. Broker-side execution and the visual panel need a dedicated configured terminal for runtime acceptance. Offline tests do not validate the broker's filling rules or actual API model behavior.

Do not delete or reset journals/database to hide failures or unlock risk. Back up both the SQLite database and the matching bridge state together while stopped. Preserve account identity, strategy version and execution ledger when restoring. A clean new independent run should use a new bridge, database and magic on a flat dedicated account.
