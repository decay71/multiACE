import sys
from pathlib import Path

# Put the repository root on sys.path so tests can import multiace.*
root_dir = str(Path(__file__).resolve().parents[1])
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)
