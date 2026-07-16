"""First-party agent-framework MODULES (the migrated in-tree agents).

Each file here is a concrete ``agent_framework`` module (an ``AgentManifest`` + the role method(s)
its manifest declares) that ADAPTS an existing in-tree agent onto the framework contract WITHOUT
editing the agent it wraps. Importing this subpackage — or an individual module in it — wires NOTHING
live: a module graduates into a live seam (supervisor node / coordinator registry / activation
registry) only through a deliberate, Fazal-authorized cutover step, never at import (mirrors
``agent_framework``'s "importing the framework changes no routing" invariant). This subpackage is
intentionally NOT imported by ``orchestrator.agent_framework.__init__`` so the framework's import
surface stays inert + dep-less-smoke safe.
"""

from __future__ import annotations

__all__: list[str] = []
