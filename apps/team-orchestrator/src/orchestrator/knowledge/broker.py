"""Storage-agnostic retrieval broker for Phase-1 Viabe knowledge sources."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from orchestrator.knowledge.contracts import (
    GLOBAL_LAYERS,
    TENANT_SCOPED_LAYERS,
    EvidenceAuthority,
    EvidenceItem,
    KnowledgeBundle,
    KnowledgeConflict,
    KnowledgeLayer,
    KnowledgeQuery,
    KnowledgeScope,
    MemoryKind,
    RetrievalTrace,
)

_CHARS_PER_TOKEN = 4
_ITEM_OVERHEAD_TOKENS = 8
_MIN_TRUNCATED_CONTENT_TOKENS = 24

_LAYER_ORDER = {layer: index for index, layer in enumerate(KnowledgeLayer)}
_AUTHORITY_ORDER = {
    EvidenceAuthority.OWNER: 0,
    EvidenceAuthority.VERIFIED_SYSTEM: 1,
    EvidenceAuthority.VTR: 2,
    EvidenceAuthority.VERIFIED_OUTCOME: 3,
    EvidenceAuthority.SEED: 4,
    EvidenceAuthority.AGENT_INFERENCE: 5,
}


class KnowledgeIsolationError(RuntimeError):
    """An adapter returned evidence outside the trusted retrieval scope."""


class KnowledgeContractError(RuntimeError):
    """An adapter violated its declared layer contract."""


class RetrievalAdapter(Protocol):
    layer: KnowledgeLayer

    def retrieve(
        self,
        scope: KnowledgeScope,
        query: KnowledgeQuery,
        *,
        limit: int,
    ) -> Sequence[EvidenceItem]: ...


class KnowledgeBroker:
    """Collect, validate, rank, and budget evidence from independent adapters.

    Ordinary adapter outages are fail-soft and recorded by exception class only.
    Contract or tenant-isolation violations are fail-closed because returning a
    partial bundle after either condition would conceal a security defect.
    """

    def __init__(self, adapters: Iterable[RetrievalAdapter]) -> None:
        by_layer: dict[KnowledgeLayer, RetrievalAdapter] = {}
        for adapter in adapters:
            if adapter.layer in by_layer:
                raise ValueError(f"duplicate adapter for layer {adapter.layer.value}")
            by_layer[adapter.layer] = adapter
        self._adapters = by_layer

    def retrieve(
        self,
        scope: KnowledgeScope,
        query: KnowledgeQuery,
        *,
        now: datetime | None = None,
    ) -> KnowledgeBundle:
        started = time.perf_counter()
        current_time = now or datetime.now(UTC)
        raw_items: list[EvidenceItem] = []
        adapter_errors: dict[str, str] = {}
        layer_hits: dict[str, int] = {}

        selected_layers = tuple(sorted(query.layers, key=_LAYER_ORDER.__getitem__))
        for layer in selected_layers:
            adapter = self._adapters.get(layer)
            if adapter is None:
                layer_hits[layer.value] = 0
                continue
            try:
                results = list(
                    adapter.retrieve(scope, query, limit=query.top_k_per_layer)
                )[: query.top_k_per_layer]
                for item in results:
                    if not isinstance(item, EvidenceItem):
                        raise KnowledgeContractError(
                            f"adapter {layer.value} returned a non-EvidenceItem result"
                        )
                    self._validate_scope(scope, layer, item)
            except (KnowledgeIsolationError, KnowledgeContractError):
                raise
            except Exception as exc:  # noqa: BLE001 - ordinary source outage is fail-soft
                adapter_errors[layer.value] = type(exc).__name__
                layer_hits[layer.value] = 0
                continue
            layer_hits[layer.value] = len(results)
            raw_items.extend(results)

        self._validate_unique_evidence_ids(raw_items)
        eligible, filtered_ids = self._filter_eligible(raw_items, current_time)
        deduplicated, duplicate_ids = self._deduplicate(eligible)
        ranked = sorted(deduplicated, key=self._rank_key)
        selected, budget_omissions, token_count = self._apply_budget(
            ranked, query.token_budget
        )
        conflicts = self._detect_conflicts(selected)
        categories = self._categorize(selected)

        elapsed_ms = (time.perf_counter() - started) * 1_000
        omitted = tuple(dict.fromkeys((*filtered_ids, *duplicate_ids, *budget_omissions)))
        trace = RetrievalTrace(
            layers_queried=selected_layers,
            layer_hits=layer_hits,
            adapter_errors=adapter_errors,
            omitted_evidence_ids=omitted,
            elapsed_ms=elapsed_ms,
        )
        return KnowledgeBundle(
            query=query,
            facts=tuple(categories["facts"]),
            relationships=tuple(categories["relationships"]),
            episodes=tuple(categories["episodes"]),
            priors=tuple(categories["priors"]),
            policies_and_lessons=tuple(categories["policies_and_lessons"]),
            conflicts=conflicts,
            evidence_manifest=tuple(item.evidence_id for item in selected),
            token_count=token_count,
            trace=trace,
        )

    @staticmethod
    def _validate_scope(
        scope: KnowledgeScope,
        adapter_layer: KnowledgeLayer,
        item: EvidenceItem,
    ) -> None:
        if item.layer != adapter_layer:
            raise KnowledgeContractError(
                f"adapter {adapter_layer.value} returned {item.layer.value} evidence"
            )
        if adapter_layer in TENANT_SCOPED_LAYERS and item.tenant_id != scope.tenant_id:
            raise KnowledgeIsolationError(
                f"tenant-scoped {adapter_layer.value} evidence did not match runtime scope"
            )
        if adapter_layer in GLOBAL_LAYERS and item.tenant_id is not None:
            raise KnowledgeIsolationError(
                f"global {adapter_layer.value} evidence unexpectedly carried tenant identity"
            )

    @staticmethod
    def _validate_unique_evidence_ids(items: Sequence[EvidenceItem]) -> None:
        seen: set[str] = set()
        for item in items:
            if item.evidence_id in seen:
                raise KnowledgeContractError(
                    f"duplicate evidence_id returned: {item.evidence_id}"
                )
            seen.add(item.evidence_id)

    @staticmethod
    def _filter_eligible(
        items: Sequence[EvidenceItem], now: datetime
    ) -> tuple[list[EvidenceItem], list[str]]:
        kept: list[EvidenceItem] = []
        omitted: list[str] = []
        for item in items:
            valid = (
                item.retrieval_eligible
                and item.superseded_by is None
                and (item.valid_from is None or item.valid_from <= now)
                and (item.valid_to is None or item.valid_to > now)
            )
            if valid:
                kept.append(item)
            else:
                omitted.append(item.evidence_id)
        return kept, omitted

    @classmethod
    def _deduplicate(
        cls, items: Sequence[EvidenceItem]
    ) -> tuple[list[EvidenceItem], list[str]]:
        winners: dict[tuple[KnowledgeLayer, str], EvidenceItem] = {}
        omitted: list[str] = []
        for item in items:
            key = (item.layer, item.source_id)
            existing = winners.get(key)
            if existing is None:
                winners[key] = item
                continue
            if cls._rank_key(item) < cls._rank_key(existing):
                omitted.append(existing.evidence_id)
                winners[key] = item
            else:
                omitted.append(item.evidence_id)
        return list(winners.values()), omitted

    @staticmethod
    def _rank_key(item: EvidenceItem) -> tuple[int, float, float, str]:
        timestamp = item.occurred_at or item.valid_from
        epoch = timestamp.timestamp() if timestamp else 0.0
        return (
            _AUTHORITY_ORDER[item.authority],
            -(item.score or 0.0),
            -epoch,
            item.evidence_id,
        )

    @classmethod
    def _apply_budget(
        cls, items: Sequence[EvidenceItem], token_budget: int
    ) -> tuple[list[EvidenceItem], list[str], int]:
        selected: list[EvidenceItem] = []
        omitted: list[str] = []
        used = 0
        for item in items:
            item_tokens = cls._estimate_tokens(item.content)
            if used + item_tokens <= token_budget:
                selected.append(item)
                used += item_tokens
                continue

            remaining = token_budget - used
            if remaining >= _MIN_TRUNCATED_CONTENT_TOKENS:
                selected.append(cls._truncate_item(item, remaining))
                used = token_budget
            else:
                omitted.append(item.evidence_id)
            if used >= token_budget:
                omitted.extend(i.evidence_id for i in items[len(selected) :])
                break
        return selected, omitted, used

    @staticmethod
    def _estimate_tokens(content: str) -> int:
        return max(1, math.ceil(len(content) / _CHARS_PER_TOKEN)) + _ITEM_OVERHEAD_TOKENS

    @staticmethod
    def _truncate_item(item: EvidenceItem, token_budget: int) -> EvidenceItem:
        content_tokens = max(1, token_budget - _ITEM_OVERHEAD_TOKENS)
        max_chars = content_tokens * _CHARS_PER_TOKEN
        suffix = "\n[truncated]"
        content = item.content[: max(1, max_chars - len(suffix))] + suffix
        metadata = {**item.metadata, "content_truncated": True}
        return item.model_copy(update={"content": content, "metadata": metadata})

    @staticmethod
    def _detect_conflicts(
        items: Sequence[EvidenceItem],
    ) -> tuple[KnowledgeConflict, ...]:
        claims: dict[str, list[EvidenceItem]] = defaultdict(list)
        for item in items:
            if item.claim_key is not None:
                claims[item.claim_key].append(item)

        conflicts: list[KnowledgeConflict] = []
        for claim_key, claim_items in sorted(claims.items()):
            values = tuple(dict.fromkeys(item.claim_value or "" for item in claim_items))
            if len(values) < 2:
                continue
            conflicts.append(
                KnowledgeConflict(
                    claim_key=claim_key,
                    evidence_ids=tuple(item.evidence_id for item in claim_items),
                    claim_values=values,
                )
            )
        return tuple(conflicts)

    @staticmethod
    def _categorize(items: Sequence[EvidenceItem]) -> dict[str, list[EvidenceItem]]:
        categories: dict[str, list[EvidenceItem]] = {
            "facts": [],
            "relationships": [],
            "episodes": [],
            "priors": [],
            "policies_and_lessons": [],
        }
        for item in items:
            if item.layer == KnowledgeLayer.L3:
                categories["priors"].append(item)
            elif item.kind == MemoryKind.RELATIONSHIP:
                categories["relationships"].append(item)
            elif item.kind in {MemoryKind.EPISODE, MemoryKind.OUTCOME}:
                categories["episodes"].append(item)
            elif item.kind in {
                MemoryKind.POLICY,
                MemoryKind.DIRECTIVE,
                MemoryKind.CORRECTION,
                MemoryKind.SEED,
            }:
                categories["policies_and_lessons"].append(item)
            else:
                categories["facts"].append(item)
        return categories


__all__ = [
    "KnowledgeBroker",
    "KnowledgeContractError",
    "KnowledgeIsolationError",
    "RetrievalAdapter",
]
