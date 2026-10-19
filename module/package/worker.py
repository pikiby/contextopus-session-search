"""Core service entry point; Core supplies its existing child SDK."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_search.module_service import create_service
