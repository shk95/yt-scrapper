"""Direct access to YouTube's own internal API.

Everything here reads a surface YouTube does not version for us, and it is the
most fragile code in the project. It is separated so that fragility is visible
rather than spread through the sources.
"""

from __future__ import annotations
