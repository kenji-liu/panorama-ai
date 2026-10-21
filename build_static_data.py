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

# 環景導覽路線：沿高屏溪由上游到下游。
# 每站 = (Kuula ID, 場景名稱, 拍攝點緯度, 經度, 全景圖正中央的羅盤方位角 HEADING)
# 熱點位置 = 由「本站座標 → 下一站／上一站座標」算出的方位角，再減去 HEADING 換算成畫面水平位置。
# HEADING 是用 OpenStreetMap 上的橋樑座標（里嶺大橋、高屏溪斜張橋、高屏大橋、萬大大橋）與
# 畫面中橋塔／橋身實際出現的位置反推而得（第 2、3、4、5 站直接對到橋；第 1、6 站對山形、河道與工業區）。
ROUTE = [
    ("52mWd", "里港大橋下游", 22.78333, 120.46667, 200),
    ("L8K4R", "里嶺大橋上游", 22.77515, 120.45093, 120),
    ("5qh6V", "高屏溪斜張橋下游", 22.75445, 120.44890, 188),
    ("L8crR", "高屏大橋下游", 22.63638, 120.43402, 78),
    ("hv4SS", "萬大橋下游", 22.57946, 120.43961, 73),
    ("L8cSt", "萬丹堰／固床工下游", 22.55332, 120.43676, 20),
]
# 相鄰兩站太近或中間隔著橋時，熱點改瞄準途經的地標（OSM 座標），這樣才會落在河道上
VIA = {
    ("L8K4R", "5qh6V"): (22.7620, 120.4610),  # 高屏溪斜張橋
    ("5qh6V", "L8K4R"): (22.7620, 120.4610),
}
HOTSPOT_PITCH = -0.12  # 略低於地平線，落在河面／地面上


def bearing(a, b):
    """由座標 a 看向座標 b 的羅盤方位角（度，北=0、順時針）。"""
    import math
    la1, lo1, la2, lo2 = (math.radians(v) for v in (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def yaw_to(src, dst, heading):
    """換算成導覽引擎的 yaw（弳度，0=畫面正中央，往右為正）。"""
    import math
    d = ((bearing(src, dst) - heading + 180) % 360) - 180
    return round(math.radians(d), 4)


def build_scenes(epoch_note):
    """依 ROUTE 產生導覽場景、縮圖與前後跳轉熱點。"""
    from PIL import Image

    route = [r for r in ROUTE if os.path.exists(os.path.join(ASSETS, "%s.jpg" % r[0]))]
    scenes = []
    for i, (kid, name, lat, lon, heading) in enumerate(route):
        here = (lat, lon)

        def aim(other):
            return VIA.get((kid, other[0]), (other[2], other[3]))
        thumb = "t_%s.jpg" % kid
        tpath = os.path.join(ASSETS, thumb)
        if not os.path.exists(tpath):
            im = Image.open(os.path.join(ASSETS, "%s.jpg" % kid)).convert("RGB")
            im.thumbnail((480, 240))
            im.save(tpath, quality=75)
        hotspots = []
        if i + 1 < len(route):
            nxt = route[i + 1]
            hotspots.append({"id": "hs-%s-down" % kid, "yaw": yaw_to(here, aim(nxt), heading),
                             "pitch": HOTSPOT_PITCH, "targetId": "sc-" + nxt[0],
                             "label": "往下游：" + nxt[1]})
        if i > 0:
            prv = route[i - 1]
            hotspots.append({"id": "hs-%s-up" % kid, "yaw": yaw_to(here, aim(prv), heading),
                             "pitch": HOTSPOT_PITCH, "targetId": "sc-" + prv[0],
                             "label": "往上游：" + prv[1]})
        scenes.append({
            "id": "sc-" + kid, "name": name,
            "note": "拍攝日期 " + epoch_note.get(kid, ""),
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
