"""Convenience launcher used during development.

For end users, the .exe built by build_exe.bat is the main entry point.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.main import main


if __name__ == "__main__":
    sys.exit(main())
