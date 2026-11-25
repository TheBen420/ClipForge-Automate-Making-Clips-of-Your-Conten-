import json, pathlib, sys
sys.path.insert(0, r"C:\Users\Ben\clipforge")
import clipforge as cf
import clipforge_gui as gui

cfg = cf.load_config()
rows, seen = [], set()
for path in sorted(pathlib.Path(r"D:\ClipForge\sessions").glob("*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data.get("clips") or []:
        if pathlib.Path(row["path"]).exists() and row["path"] not in seen:
            seen.add(row["path"])
            rows.append(dict(row))

rows.sort(key=lambda row: pathlib.Path(row["path"]).name)
gui.open_review_window(cfg, rows, f"clips from both tests ({len(rows)} clips)")
