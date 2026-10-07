"""Agora 進入點：python agora.py <指令>，說明見 README.md 或 python agora.py -h"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agoralib.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
