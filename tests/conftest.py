"""Test configuration: make the integration importable as a package."""

from __future__ import annotations

import sys
from pathlib import Path

# The integration lives under custom_components/ as Home Assistant expects; tests import it
# from the repository root rather than from an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))
