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

# 環景導覽路線：由上游到下游。每筆 = (Kuula ID, 場景名稱, 熱點位置)
# 熱點位置是在全景圖水平方向的比例 0~1（0.5 = 圖片正中央），"down"=往下一站、"up"=回上一站。
# 位置是依畫面中河川流向目測的近似值，需要微調時改這裡的數字後重新執行即可。
ROUTE = [
    ("52mWd", "里港大橋下游", {"down": 0.22}),
    ("L8K4R", "里嶺大橋上游", {"up": 0.13, "down": 0.55}),
    ("hMbdw", "高屏溪斜張橋下游", {"up": 0.12, "down": 0.52}),
    ("L8crR", "高屏大橋下游", {"up": 0.30, "down": 0.70}),
    ("hv4SS", "萬大橋下游", {"up": 0.12, "down": 0.93}),
    ("L8cSt", "萬丹堰／固床工下游", {"up": 0.62}),
]
HOTSPOT_PITCH = -0.12  # 略低於地平線，落在河面／地面上


def build_scenes(epoch_note):
    """依 ROUTE 產生導覽場景、縮圖與前後跳轉熱點。"""
    from PIL import Image

    route = [r for r in ROUTE if os.path.exists(os.path.join(ASSETS, "%s.jpg" % r[0]))]
    scenes = []
    for i, (kid, name, pos) in enumerate(route):
        thumb = "t_%s.jpg" % kid
        tpath = os.path.join(ASSETS, thumb)
        if not os.path.exists(tpath):
            im = Image.open(os.path.join(ASSETS, "%s.jpg" % kid)).convert("RGB")
            im.thumbnail((480, 240))
            im.save(tpath, quality=75)
        hotspots = []
        if i + 1 < len(route) and "down" in pos:
            hotspots.append({"id": "hs-%s-down" % kid, "yaw": round((pos["down"] - 0.5) * 6.283185, 4),
                             "pitch": HOTSPOT_PITCH, "targetId": "sc-" + route[i + 1][0],
                             "label": "往下游：" + route[i + 1][1]})
        if i > 0 and "up" in pos:
            hotspots.append({"id": "hs-%s-up" % kid, "yaw": round((pos["up"] - 0.5) * 6.283185, 4),
                             "pitch": HOTSPOT_PITCH, "targetId": "sc-" + route[i - 1][0],
                             "label": "往上游：" + route[i - 1][1]})
        scenes.append({
            "id": "sc-" + kid, "name": name,
            "note": "最新影像 " + epoch_note.get(kid, ""),
            "src": "/data/assets/%s.jpg" % kid, "thumb": "/data/assets/%s" % thumb,
            "isPrivate": False, "hotspots": hotspots,
            "startYaw": hotspots[0]["yaw"] if hotspots else 0,
        })
    return scenes


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

    epoch_note = {}
    for p in points:
        for e in p["epochs"]:
            epoch_note[e["id"][3:]] = "%d/%02d/%02d" % tuple(int(x) for x in e["note"].split()[0].split("/"))
    scenes = build_scenes(epoch_note)

    state = {"version": 1, "scenes": scenes, "basemap": None, "points": points, "analysisRecords": []}
    with open(os.path.join(DATA, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    total = sum(len(p["epochs"]) for p in points)
    mb = sum(os.path.getsize(os.path.join(ASSETS, x)) for x in os.listdir(ASSETS)) / 1048576
    print("已產生 data/state.json：%d 個導覽場景、%d 個點位、%d 個時期，圖片共 %.1f MB" % (len(scenes), len(points), total, mb))
    for s in skipped:
        print("略過 %s %s：%s" % s)


if __name__ == "__main__":
    main()
