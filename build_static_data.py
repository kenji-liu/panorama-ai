# -*- coding: utf-8 -*-
"""把 Kuula 下載的全景圖整理成「隨程式部署」的靜態資料庫（不需要 R2、不需要密碼）。

    python build_static_data.py                 # 產生 data/state.json 與 data/assets/*.jpg
    python build_static_data.py --exclude hjTtt,hWNsl   # 這些相片不公開（不複製、不寫入）

之後 git add data / commit / push，Render 會自動重新部署並顯示。
"""
import argparse
import json
import os
import shutil

from import_kuula import TITLE_FIX, parse_title, point_name

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
ASSETS = os.path.join(DATA, "assets")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(os.path.expanduser("~"), "Downloads"))
    ap.add_argument("--exclude", default="", help="以逗號分隔的 Kuula ID，這些相片不公開")
    args = ap.parse_args()
    excluded = {x.strip() for x in args.exclude.split(",") if x.strip()}

    items = json.load(open(os.path.join(args.dir, "kuula_manifest.json"), encoding="utf-8-sig"))
    os.makedirs(ASSETS, exist_ok=True)

    groups, skipped = {}, []
    for it in items:
        if it["id"] in excluded:
            skipped.append((it["id"], it["title"], "已排除（不公開）"))
            continue
        src = os.path.join(args.dir, "kuula_%s.jpg" % it["id"])
        if not os.path.exists(src):
            skipped.append((it["id"], it["title"], "檔案不存在"))
            continue
        it["title"] = TITLE_FIX.get(it["id"], it["title"])
        y, mo, d, rest = parse_title(it["title"])
        if y is None:
            skipped.append((it["id"], it["title"], "標題無法解析日期"))
            continue
        groups.setdefault(point_name(rest), []).append(dict(it, y=y, mo=mo, d=d, rest=rest, src=src))

    points = []
    for n, (name, eps) in enumerate(groups.items(), 1):
        lats = sorted(e["lat"] for e in eps if e.get("lat"))
        lngs = sorted(e["lng"] for e in eps if e.get("lng"))
        epochs = []
        for e in sorted(eps, key=lambda x: (x["y"], x["mo"], x["d"])):
            dst = os.path.join(ASSETS, "%s.jpg" % e["id"])
            if not (os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(e["src"])):
                shutil.copyfile(e["src"], dst)
            epochs.append({
                "id": "ep-" + e["id"], "year": e["y"], "month": e["mo"],
                "note": "%d/%02d/%02d %s" % (e["y"], e["mo"], e["d"], e["rest"] or e["title"]),
                "src": "/data/assets/%s.jpg" % e["id"], "yawOffset": 0, "isPrivate": False,
            })
        points.append({
            "id": "pt-%02d" % n, "kind": "geo", "name": name,
            "note": "來源：Kuula（kenji700808）", "isPrivate": False,
            "lat": lats[len(lats) // 2] if lats else 22.7,
            "lon": lngs[len(lngs) // 2] if lngs else 120.5,
            "epochs": epochs,
        })

    state = {"version": 1, "scenes": [], "basemap": None, "points": points, "analysisRecords": []}
    with open(os.path.join(DATA, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    total = sum(len(p["epochs"]) for p in points)
    mb = sum(os.path.getsize(os.path.join(ASSETS, x)) for x in os.listdir(ASSETS)) / 1048576
    print("已產生 data/state.json：%d 個點位、%d 個時期，圖片共 %.1f MB" % (len(points), total, mb))
    for s in skipped:
        print("略過 %s %s：%s" % s)


if __name__ == "__main__":
    main()
