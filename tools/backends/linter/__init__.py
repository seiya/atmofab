#!/usr/bin/env python3
"""Backends of the `linter` axis.

Each subpackage is one registered `linter` value whose invocation and rule set have been
extracted out of the neutral core. `docs/BACKEND_BOUNDARY.md` states which values are still
inlined in `mcp_servers/build_runtime_server.py`; `TODO.md`'s compiler / linter adapters area
owns the rest of the move.
"""
