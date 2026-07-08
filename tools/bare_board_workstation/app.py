from __future__ import annotations

from pathlib import Path
import sys


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bare_board_workstation.ui_main import main
else:
    from .ui_main import main


if __name__ == "__main__":
    main()
