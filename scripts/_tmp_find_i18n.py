import re
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "QuantDinger-Vue-src" / "src" / "locales" / "lang"
for name in ("zh-CN.js", "en-US.js"):
    p = root / name
    if not p.exists():
        print(f"missing {p}")
        continue
    t = p.read_text(encoding="utf-8")
    print(f"\n=== {name} ===")
    for pat in ("tradeReason", "restingPurpose", "grid_initial", "long_entry"):
        hits = re.findall(rf"'([^']*{pat}[^']*)': '([^']*)'", t)
        print(f"  {pat}: {len(hits)}")
        for k, v in hits[:20]:
            print(f"    {k} = {v}")
