#!/usr/bin/env python3
"""Target-stack backends: everything that knows a concrete technology by name.

One package per `<axis>/<backend_id>` (`language/fortran`, `build_system/make`, ...). The
neutral core names an axis VALUE and asks `tools.backends.registry` for the backend; it never
imports a backend module directly. `docs/BACKEND_BOUNDARY.md` is the canonical rule.
"""
