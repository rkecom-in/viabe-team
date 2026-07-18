# Tool Catalog (`agent_framework.tool_catalog`)

> GENERATED from `orchestrator.agent_framework.tool_catalog` by `render_catalog_markdown()`.
> Do NOT hand-edit — edit the catalog annotations and regenerate. ARCHITECTURE §1.3.

**82 tool surfaces** across the roster — 3 gated (GateFacade doors). advisory: 31, decision: 4, eval: 2, gated_effect: 2, integration: 10, read: 30, spawn: 3

| Tool | Surface | Kind | Capability | Gated | PII-safe | Tenant | Holders | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `accounting_categorize_books` | `agent/accounting_lane.py` | advisory | — | no | yes | resolved | manager_advisory, accounting_lane | prepares categorization; no write |
| `accounting_escalate_to_fazal` | `agent/accounting_lane.py` | advisory | — | no | yes | n/a | accounting_lane | ops escalation to Fazal (excluded from ADVISORY_TOOLS as redundant); no effect |
| `accounting_organize_invoices_expenses` | `agent/accounting_lane.py` | advisory | — | no | yes | resolved | manager_advisory, accounting_lane | prepares an organization; no write |
| `accounting_prepare_tax_summary` | `agent/accounting_lane.py` | advisory | — | no | yes | resolved | manager_advisory, accounting_lane | prepares a tax summary; no write |
| `accounting_reconcile_transactions` | `agent/accounting_lane.py` | advisory | — | no | yes | resolved | manager_advisory, accounting_lane | prepares a reconciliation; no write |
| `finance_escalate_to_fazal` | `agent/finance_lane.py` | advisory | — | no | yes | n/a | finance_lane | ops escalation to Fazal (excluded from ADVISORY_TOOLS as redundant); no effect |
| `finance_pushback` | `agent/finance_lane.py` | advisory | — | no | yes | n/a | finance_lane | specialist->manager pushback protocol; no effect (excluded from ADVISORY_TOOLS) |
| `propose_payment_reminder` | `agent/finance_lane.py` | advisory | — | no | yes | resolved | manager_advisory, finance_lane | drafts a reminder PROPOSAL; no send/persist |
| `integration_escalate_to_fazal` | `agent/integration_agent.py` | advisory | — | no | yes | n/a | integration_specialist | ops escalation to Fazal (integration surface); no external effect |
| `draft_campaign_plan` | `agent/marketing_lane.py` | advisory | — | no | yes | resolved | manager_advisory, marketing_lane | drafts a campaign/offer intent; no send |
| `draft_content` | `agent/marketing_lane.py` | advisory | — | no | yes | resolved | manager_advisory, marketing_lane | drafts content copy; no send |
| `marketing_escalate_to_fazal` | `agent/marketing_lane.py` | advisory | — | no | yes | n/a | marketing_lane | ops escalation to Fazal (excluded from ADVISORY_TOOLS as redundant); no effect |
| `apply_correction` | `agent/onboarding_conductor.py` | advisory | — | no | yes | resolved | onboarding_specialist | onboarding-profile WRITE (correction) — non-gated |
| `conductor_escalate_to_fazal` | `agent/onboarding_conductor.py` | advisory | — | no | yes | n/a | onboarding_specialist | ops escalation to Fazal (onboarding surface); no external effect |
| `extract_owner_answer` | `agent/onboarding_conductor.py` | advisory | — | no | yes | n/a | onboarding_specialist | parses a structured answer from the owner's message; no DB |
| `propose_business_policy` | `agent/onboarding_conductor.py` | advisory | — | no | yes | resolved | onboarding_specialist | drafts a business-policy PROPOSAL (owner confirms the bound); no effect |
| `record_answer` | `agent/onboarding_conductor.py` | advisory | — | no | yes | resolved | onboarding_specialist | onboarding-profile WRITE (owner's own data) — non-gated, no customer send |
| `record_skip` | `agent/onboarding_conductor.py` | advisory | — | no | yes | resolved | onboarding_specialist | onboarding-profile WRITE (skip marker) — non-gated |
| `escalate_to_fazal` | `agent/orchestrator_agent.py` | advisory | — | no | yes | n/a | manager_core | ops escalation to Fazal; no external effect |
| `export_customer_list` | `agent/orchestrator_agent.py` | advisory | — | no | yes | resolved | manager_core | VT-676 F3: delivers the owner's OWN customer list as a WhatsApp CSV to the VERIFIED owner (send_customer_list_to_owner: server-derived recipient, private bucket, 300s URL, audit) — OWNER-comms delivery, not a customer send; Manager-only holder (§1.2) |
| `record_business_objective` | `agent/orchestrator_agent.py` | advisory | — | no | yes | resolved | manager_core | Manager-scoped context WRITE (business_objective, VT-466) — non-gated |
| `set_language_preference` | `agent/orchestrator_agent.py` | advisory | — | no | yes | resolved | manager_core | VT-677: the owner's EXPLICIT language choice (preferred_language write, D3 verbal override) — non-gated owner-own-preference write; never affects live-turn mirroring (D2) |
| `write_l0_fragment` | `agent/orchestrator_agent.py` | advisory | — | no | yes | resolved | manager_core | Manager-scoped context WRITE (L0 memory) — non-gated, no external effect (VT-268 benign) |
| `push_back_to_manager` | `agent/sales_lane.py` | advisory | — | no | yes | n/a | sales_lane | specialist->manager pushback protocol; no effect (excluded from ADVISORY_TOOLS) |
| `recommend_sales_play` | `agent/sales_lane.py` | advisory | — | no | yes | resolved | manager_advisory, sales_lane | drafts a sales-play recommendation (intent only; no send) |
| `sales_lane_escalate_to_fazal` | `agent/sales_lane.py` | advisory | — | no | yes | n/a | sales_lane | ops escalation to Fazal (excluded from ADVISORY_TOOLS as redundant); no effect |
| `advise_integration_setup` | `agent/tech_lane.py` | advisory | — | no | yes | n/a | manager_advisory, tech_lane | read-only registry advice (owner-visible connector catalogue only) |
| `propose_config_change` | `agent/tech_lane.py` | advisory | propose_config_change | no | yes | resolved | manager_advisory, tech_lane | drafts a config intent; no write |
| `tech_escalate_to_fazal` | `agent/tech_lane.py` | advisory | — | no | yes | n/a | tech_lane | ops escalation to Fazal (excluded from ADVISORY_TOOLS as redundant); no effect |
| `schedule_followup` | `agent/tools/schedule_followup.py` | advisory | — | no | yes | resolved | — | schedules an internal followup (non-gated write; no customer send) |
| `escalate` | `agent_framework/tools_common.py` | advisory | — | no | yes | n/a | — | VT-672: the ONE common escalate — a specialist hands a decision back to the Manager (§1.2 owner-comms stays Manager-only); no external effect, no DB write. The Manager's own escalate_to_fazal terminal signal is separate and untouched. |
| `check_ad_spend_intent` | `agent/marketing_lane.py` | decision | — | no | yes | resolved | manager_advisory, marketing_lane | rail-facing probe: reports the SPEND business-impact gate; spends nothing (non-gated) |
| `check_send_intent` | `agent/marketing_lane.py` | decision | — | no | yes | resolved | manager_advisory, marketing_lane | rail-facing probe: reports the CUSTOMER_SEND policy bound; sends nothing (non-gated) |
| `check_config_change_intent` | `agent/tech_lane.py` | decision | — | no | yes | resolved | manager_advisory, tech_lane | rail-facing probe: reports the CONFIG business-impact gate; writes nothing (non-gated) |
| `gate_business_action` | `agent_framework/gate_facade.py` | decision | request_business_action | yes | yes | resolved | — | decision-ONLY door: returns the gate decision (issues no effect) |
| `classify_owner_message` | `agent/tools/classify_owner_message.py` | eval | — | no | yes | n/a | — | LLM classification of the owner's message; no DB, no effect |
| `self_evaluate` | `agent/tools/self_evaluate.py` | eval | — | no | yes | n/a | — | LLM self-evaluation of a proposal (VT-36); returns a verdict, no effect |
| `perform_business_action` | `agent_framework/gate_facade.py` | gated_effect | request_business_action | yes | yes | resolved | — | whole-round-trip business-action door (classify + issue-inside-choke, ARCHITECTURE §2) |
| `request_customer_send` | `agent_framework/gate_facade.py` | gated_effect | request_customer_send | yes | yes | resolved | — | the SOLE customer-send door; routes to customer_send.agent_send_draft (Gate 0..5) |
| `check_oauth_status` | `agent/integration_agent.py` | integration | read_integration_state | no | yes | resolved | integration_specialist |  |
| `commit_ingestion` | `agent/integration_agent.py` | integration | propose_config_change | no | yes | resolved | integration_specialist | VT-268: PROPOSAL only — the ingest WRITE is the module-external deterministic executor |
| `confirm_mapping` | `agent/integration_agent.py` | integration | propose_config_change | no | yes | resolved | integration_specialist |  |
| `list_supported_connectors` | `agent/integration_agent.py` | integration | read_integration_state | no | yes | n/a | integration_specialist | static supported-connector registry read; no tenant DB |
| `propose_mapping` | `agent/integration_agent.py` | integration | propose_config_change | no | yes | resolved | integration_specialist |  |
| `pull_sample` | `agent/integration_agent.py` | integration | read_integration_state | no | yes | resolved | integration_specialist | VT-268: counts-only sample summary (no raw customer rows returned) |
| `read_integration_state` | `agent/integration_agent.py` | integration | read_integration_state | no | yes | resolved | integration_specialist |  |
| `schedule_recurring_pull` | `agent/integration_agent.py` | integration | propose_config_change | no | yes | resolved | integration_specialist | cadence CONFIG staging (VT-210 accepted precedent); non-gated |
| `start_oauth` | `agent/integration_agent.py` | integration | propose_config_change | no | yes | resolved | integration_specialist | OAuth link-out (a proposal/hand-off, not a config write) |
| `verify_connector` | `agent/integration_agent.py` | integration | read_integration_state | no | yes | resolved | integration_specialist |  |
| `analyze_marketing_roi` | `agent/cost_opt_lane.py` | read | — | no | yes | resolved | manager_advisory, cost_opt_lane | read-only aggregate |
| `analyze_tenant_spend` | `agent/cost_opt_lane.py` | read | — | no | yes | resolved | manager_advisory, cost_opt_lane | read-only aggregate |
| `analyze_unit_economics` | `agent/cost_opt_lane.py` | read | — | no | yes | resolved | manager_advisory, cost_opt_lane | read-only aggregate |
| `identify_spend_anomaly` | `agent/cost_opt_lane.py` | read | — | no | yes | resolved | manager_advisory, cost_opt_lane | read-only aggregate |
| `read_cost_context` | `agent/cost_opt_lane.py` | read | — | no | yes | resolved | manager_advisory, cost_opt_lane | read-only (business_context slice) |
| `analyze_cash_flow` | `agent/finance_lane.py` | read | — | no | yes | resolved | manager_advisory, finance_lane | read-only aggregate |
| `analyze_receivables` | `agent/finance_lane.py` | read | — | no | yes | resolved | manager_advisory, finance_lane | read-only aggregate |
| `pricing_margin_input` | `agent/finance_lane.py` | read | — | no | yes | resolved | manager_advisory, finance_lane | read-only aggregate |
| `list_recent_campaigns` | `agent/marketing_lane.py` | read | — | no | yes | resolved | manager_advisory, marketing_lane | read-only rollup (counts only, CL-390) |
| `activation_check` | `agent/onboarding_conductor.py` | read | — | no | yes | resolved | onboarding_specialist |  |
| `next_required_question` | `agent/onboarding_conductor.py` | read | — | no | yes | resolved | onboarding_specialist |  |
| `profile_completion_check` | `agent/onboarding_conductor.py` | read | — | no | yes | resolved | onboarding_specialist |  |
| `read_onboarding_state` | `agent/onboarding_conductor.py` | read | — | no | yes | resolved | onboarding_specialist |  |
| `query_l0` | `agent/orchestrator_agent.py` | read | — | no | yes | resolved | manager_core | Manager L0-memory read |
| `search_conversation_history` | `agent/orchestrator_agent.py` | read | — | no | yes | resolved | manager_core | owner<->assistant conversation-log retrieval (owner-authored text; not customer rows) |
| `identify_repeat_upsell_opportunity` | `agent/sales_lane.py` | read | — | no | yes | n/a | manager_advisory, sales_lane | pure reasoning-grounding read; no DB, no effect |
| `read_integration_health` | `agent/tech_lane.py` | read | — | no | yes | resolved | manager_advisory, tech_lane | read-only (tenant_connector_status) |
| `read_listing_health` | `agent/tech_lane.py` | read | — | no | yes | resolved | manager_advisory, tech_lane | read-only (platform_listings) |
| `read_tech_context` | `agent/tech_lane.py` | read | — | no | yes | resolved | manager_advisory, tech_lane | read-only (business_context slice) |
| `get_attribution_data` | `agent/tools/get_attribution_data.py` | read | — | no | yes | resolved | — | attribution rollup (counts/aggregates) |
| `get_business_profile` | `agent/tools/get_business_profile.py` | read | read_business_context | no | yes | resolved | — | owner's own business profile |
| `query_customer_ledger` | `agent/tools/query_customer_ledger.py` | read | read_customer_ledger | no | yes | resolved | — | returns customer_id + amounts/dates/notes — no name/phone column (CL-390) |
| `get_attribution_data` | `agent_framework/tools_common.py` | read | — | no | yes | resolved | manager_common_read | VT-675 promoted (resolve-first wrapper): attribution rollup (counts/aggregates) |
| `get_recent_campaigns` | `agent_framework/tools_common.py` | read | — | no | yes | resolved | manager_common_read | VT-675 promoted (resolve-first wrapper): recent-campaign rollup (counts/statuses only, CL-390) |
| `query_customer_ledger` | `agent_framework/tools_common.py` | read | — | no | yes | resolved | manager_common_read | VT-675 promoted (resolve-first wrapper): operator-role ledger read (phone-token input, customer_id UUIDs + amounts out — never name/phone/email; scope unchanged, CL-82/CL-390) |
| `read_active_plan` | `agent_framework/tools_common.py` | read | — | no | yes | resolved | manager_common_read | VT-673: first-class plan/roadmap read (delegates to business_plan store/seams; owner's own plan data, no customer PII) |
| `read_agent_memory` | `agent_framework/tools_common.py` | read | — | no | yes | n/a | manager_common_read | VT-674: on-demand L3-prior read (delegates to knowledge.l3_query.lookup_pattern; 180d quarantine + k>=10 anonymized aggregates structural — cross-tenant global table, resolved tenant used ONLY for the quarantine check) |
| `read_business_context` | `agent_framework/tools_common.py` | read | read_business_context | no | yes | resolved | manager_common_read |  |
| `read_customer_ledger_summary` | `agent_framework/tools_common.py` | read | read_customer_ledger | no | yes | resolved | manager_common_read |  |
| `read_integration_state` | `agent_framework/tools_common.py` | read | read_integration_state | no | yes | resolved | manager_common_read |  |
| `spawn_integration` | `agent/roster.py` | spawn | — | no | yes | n/a | — | handoff to the Integration specialist sub-graph; returns a Command |
| `spawn_onboarding_conductor` | `agent/roster.py` | spawn | — | no | yes | n/a | — | handoff to the Onboarding-Conductor specialist sub-graph; returns a Command |
| `spawn_sales_recovery` | `agent/roster.py` | spawn | — | no | yes | n/a | — | handoff to the Sales-Recovery specialist sub-graph; returns a Command |

## Open capability gaps (sufficiency frontier)

None open — every tracked capability gap has been built/promoted.
