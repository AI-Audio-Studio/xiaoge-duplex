from pathlib import Path
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

source = ROOT / 'sdk/contract_replay.py'
code = compile(source.read_text(encoding="utf-8"), str(source), "exec")
exec(code, {"__name__": "__main__", "__file__": str(source)})
