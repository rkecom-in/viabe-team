"""VT-7.1-R fix-forward — verify the voyage-4-lite embedding dimension.

PR #44's post-merge audit found that ``vector(1024)`` on
``l1_entities.embedding`` (migrations/019_l1_knowledge_graph.sql:30) was
locked from a session decision and never cross-checked against a real
Voyage API response. This test closes the gap: one real billed call,
asserts the returned embedding length == 1024, asserts a server-side
usage attribute proves the call was not mocked / cached.

Skip-gating
-----------
Two layers; the test SKIPs cleanly when either is absent so CI without
the key stays green and never fails on this surface:

- ``VOYAGE_API_KEY`` env var (the voyageai SDK's documented default —
  named here because the repo had no Voyage env-var convention before
  this PR; surfacing the name explicitly per the brief).
- ``voyageai`` Python package: ``pytest.importorskip`` skips when the
  SDK isn't installed (it isn't on main today — adding it is a
  separate decision once the verification path becomes routine).

Proof-of-call discipline (CL-272 / #40 thread)
----------------------------------------------
The voyage SDK's ``embed`` response carries ``total_tokens`` — a
server-returned integer that a real billed call produces. The
assertion asserts it is a positive int; a mocked / cached response
that returned a hand-built ``EmbeddingsObject`` without populating
``total_tokens`` would fail this check.

Expected outcome
----------------
- Returned dimension == 1024 → ``vector(1024)`` is VERIFIED against
  the live model.
- Returned dimension != 1024 → assertion fails loudly with the actual
  dimension in the message. Migration 019 is NOT changed in this PR;
  the real dimension is reported back and the schema decision is
  re-opened separately.
"""

from __future__ import annotations

import os

import pytest

# Skip if the SDK is unavailable. ``voyageai`` is not yet a project
# dependency — installing it is the local-runner's responsibility
# until/unless a future PR pins it.
voyageai = pytest.importorskip("voyageai")

pytestmark = pytest.mark.skipif(
    not os.environ.get("VOYAGE_API_KEY"),
    reason=(
        "VOYAGE_API_KEY not set — voyage-4-lite dimension verification "
        "skipped. CI does not set this key; run locally to verify."
    ),
)


_PROBE_INPUT = "dimension probe"
_PROBE_MODEL = "voyage-4-lite"
_EXPECTED_DIM = 1024


def test_voyage_4_lite_returns_1024_dim_embedding() -> None:
    """Real billed call to ``voyage-4-lite`` returns a 1024-dim vector.

    Three load-bearing assertions:
      (1) ``total_tokens > 0`` — server-returned usage attribute. A
          mock that constructed an ``EmbeddingsObject`` locally without
          populating this would fail; only a real Voyage round-trip
          fills it.
      (2) Single-input call returns exactly one embedding.
      (3) Embedding length == ``_EXPECTED_DIM`` (1024). On mismatch,
          the assertion message includes the actual dimension so the
          schema decision can be re-opened with concrete evidence.
    """
    client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
    response = client.embed([_PROBE_INPUT], model=_PROBE_MODEL)

    # (1) Proof-of-call — server-returned usage.
    total_tokens = getattr(response, "total_tokens", None)
    assert isinstance(total_tokens, int), (
        f"voyage-4-lite response missing integer total_tokens; "
        f"got {total_tokens!r} — likely a mock leak"
    )
    assert total_tokens > 0, (
        "voyage-4-lite total_tokens=0 — not a real billed call"
    )

    # (2) Single input -> single embedding.
    embeddings = response.embeddings
    assert isinstance(embeddings, list), (
        f"expected embeddings as list; got {type(embeddings).__name__}"
    )
    assert len(embeddings) == 1, (
        f"single-input call returned {len(embeddings)} embeddings"
    )

    embedding = embeddings[0]

    # (3) Embedding dimension matches the migration's vector(1024).
    assert isinstance(embedding, list), (
        f"expected embedding as list of floats; got "
        f"{type(embedding).__name__}"
    )
    assert all(isinstance(x, float) for x in embedding), (
        "voyage-4-lite embedding contained non-float values"
    )
    assert len(embedding) == _EXPECTED_DIM, (
        f"voyage-4-lite returned dim={len(embedding)}; migration 019 "
        f"declares vector({_EXPECTED_DIM}). Schema decision must be "
        "re-opened — do NOT modify the migration in this PR."
    )
