#!/usr/bin/env python3
"""VT-70 — L4 skill-corpus seed CLI (thin wrapper).

Loads hand-authored markdown docs into ``l4_documents`` via
``orchestrator.knowledge.l4_corpus.seed_l4_corpus``. The real ≥30-doc corpus is
a Fazal/Clau authoring deliverable (VT-313).

CLI: python scripts/l4_seed.py <seed_dir>   (VOYAGE_API_KEY required)
"""

from __future__ import annotations

import sys

from orchestrator.knowledge.l4_corpus import seed_l4_corpus

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/l4_seed.py <seed_dir>", file=sys.stderr)
        raise SystemExit(2)
    result = seed_l4_corpus(sys.argv[1])
    print(f"l4_seed: {result['seeded']} document(s) loaded.")
