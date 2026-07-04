"""自有代码文件行数门禁(CODE_GUIDELINES §2;评审#9 把 review 项工具化)。

读 ourcode.txt 清单:>500 行报错(硬上限,exit 1);400<行数<=500 警告(软目标)。
由 `make lint-ours` 调用;独立可跑:`python scripts/check_line_counts.py`。
"""

from __future__ import annotations

import sys
from pathlib import Path

HARD_CAP = 500
SOFT_CAP = 400


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    files = [
        line.strip()
        for line in (repo / "ourcode.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    over: list[tuple[str, int]] = []
    for f in files:
        path = repo / f
        if not path.exists():
            print(f"MISSING: {f}(ourcode.txt 有残留条目?)")
            continue
        n = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
        if n > HARD_CAP:
            over.append((f, n))
        elif n > SOFT_CAP:
            print(f"WARN >{SOFT_CAP}: {n:4d} {f}")
    for f, n in over:
        print(f"FAIL >{HARD_CAP}: {n:4d} {f}")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
