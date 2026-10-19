"""Launch the packaged TUI using Core's module environment."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_search.tui import main

if __name__ == "__main__":
    main(sys.argv[1:])
