"""Every test runs against a throwaway dev root: nothing here may read or write the live Queue, whatever a test forgets."""
import os, tempfile
from pathlib import Path

_root = Path(tempfile.mkdtemp(prefix="switchboard-tests-"))
os.environ["SWITCHBOARD_ROOT"] = str(_root)
