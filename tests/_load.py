"""Load the toolkit's hyphenated scripts as modules for the tests."""
import importlib.util, sys
from pathlib import Path
TK = Path(__file__).resolve().parent.parent / "switchboard"
sys.path.insert(0, str(TK))

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TK / filename)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod
