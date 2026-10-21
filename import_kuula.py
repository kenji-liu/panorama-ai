# -*- coding: utf-8 -*-
"""
把從 Kuula 下載的全景圖匯入 panorama-ai 雲端資料庫（需管理者密碼，執行時會在視窗內詢問）。

    python import_kuula.py            # 預設讀 Downloads、傳到 https://panorama-ai.onrender.com
    python import_kuula.py --dry-run  # 只顯示分組結果，不上傳

可重複執行：已匯入的相片（備註含 [kuula:ID]）會自動略過。
"""
import argparse
import getpass
import json
import os
import re
import sys
import time

import requests

SITE_DEFAULT = "https://panorama-ai.onrender.com"

# (關鍵字, 點位名稱)：依序比對，先命中者優先
POINT_RULES = [
    ("里嶺大橋", "里嶺大橋上游"),
    ("高屏大橋", "高屏大橋下游"),
    ("斜張", "高屏溪斜張橋下游"),
    ("里港大橋", "里港大橋下游"),
    ("萬大", "萬大橋下游"),
    ("萬丹", "萬丹堰／固床工下游"),
    ("潮寮里", "萬丹堰／固床工下游"),
]


# 標題日期明顯打錯的相片：id -> 正確標題（11301111 應為 113/11/11，與同日其他兩張一致）
TITLE_FIX = {"hhdrY": "1131111高屏溪潮寮里下游"}


def parse_title(title):
    """回傳 (民國年, 月, 日, 地點文字)；解析不出日期則年為 None。"""
    t = title.strip()
    m = re.match(r"^(\d{3})/+0*(\d{1,2})/+0*(\d{1,2})(.*)$", t)
    if not m:
        m = re.match(r"^(\d{3})(\d{2})(\d{2})(.*)$", t)
    if not m:
        m = re.match(r"^(\d{3})(\d{2})(\d{2,3})(.*)$", t)
    if not m:
        return None, None, None, t
    y, mo, d, rest = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if mo < 1 or mo > 12 or d < 1 or d > 31:
        return None, None, None, t
    return y, mo, d, rest.strip()


def point_name(rest):
    for kw, name in POINT_RULES:
        if kw in rest:
            return name
    return "其他"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=SITE_DEFAULT)
    ap.add_argument("--dir", default=os.path.join(os.path.expanduser("~"), "Downloads"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    site = args.site.rstrip("/")

    manifest_path = os.path.join(args.dir, "kuula_manifest.json")
    if not os.path.exists(manifest_path):
        sys.exit("找不到 kuula_manifest.json：" + manifest_path)
    items = json.load(open(manifest_path, encoding="utf-8-sig"))

    groups = {}
    skipped = []
    for it in items:
        path = os.path.join(args.dir, "kuula_%s.jpg" % it["id"])
        if not os.path.exists(path):
            skipped.append((it["id"], it["title"], "檔案不存在"))
            continue
        it["title"] = TITLE_FIX.get(it["id"], it["title"])
        y, mo, d, rest = parse_title(it["title"])
        if y is None:
            skipped.append((it["id"], it["title"], "標題無法解析日期，請手動處理"))
            continue
        groups.setdefault(point_name(rest), []).append(dict(it, y=y, mo=mo, d=d, rest=rest, path=path))

    print("分組結果：")
    for name, eps in groups.items():
        print("  %s（%d 期）" % (name, len(eps)))
        for e in sorted(eps, key=lambda x: (x["y"], x["mo"], x["d"])):
            print("     %d/%02d/%02d  %s" % (e["y"], e["mo"], e["d"], e["title"]))
    for s in skipped:
        print("  略過 %s %s：%s" % s)
    if args.dry_run:
        return

    pwd = getpass.getpass("請輸入管理者密碼（輸入時不會顯示）：")
    headers = {"X-Admin-Password": pwd}
    print("喚醒網站（免費方案休眠後需 30–60 秒）…")
    h = requests.get(site + "/api/health", timeout=120).json()
    if not h.get("storage_ready") or not h.get("admin_enabled"):
        sys.exit("網站尚未設定好 R2 或管理者密碼：%s" % h)
    ok = requests.post(site + "/api/admin-check", json={"password": pwd}, timeout=60).json().get("ok")
    if not ok:
        sys.exit("管理者密碼錯誤")

    state = requests.get(site + "/api/state", headers=headers, timeout=120).json()
    state.setdefault("points", [])
    state.setdefault("scenes", [])
    existing_notes = " ".join(
        e.get("note", "") for p in state["points"] for e in p.get("epochs", [])
    )

    added = 0
    for name, eps in groups.items():
        point = next((p for p in state["points"] if p.get("name") == name), None)
        if point is None:
            lats = sorted(e["lat"] for e in eps if e.get("lat"))
            lngs = sorted(e["lng"] for e in eps if e.get("lng"))
            point = {
                "id": "pt-kuula-%d" % int(time.time() * 1000),
                "kind": "geo",
                "name": name,
                "note": "來源：Kuula（kenji700808）",
                "isPrivate": False,
                "lat": lats[len(lats) // 2] if lats else 22.7,
                "lon": lngs[len(lngs) // 2] if lngs else 120.5,
                "epochs": [],
            }
            state["points"].append(point)
            time.sleep(0.01)
        for e in sorted(eps, key=lambda x: (x["y"], x["mo"], x["d"])):
            tag = "[kuula:%s]" % e["id"]
            if tag in existing_notes:
                print("已存在，略過 " + e["title"])
                continue
            for attempt in range(3):
                try:
                    with open(e["path"], "rb") as f:
                        r = requests.post(
                            site + "/api/upload-image", headers=headers,
                            files={"file": (os.path.basename(e["path"]), f, "image/jpeg")}, timeout=300,
                        )
                    r.raise_for_status()
                    url = r.json()["url"]
                    break
                except Exception as ex:  # noqa: BLE001
                    print("  上傳失敗（第 %d 次）%s：%s" % (attempt + 1, e["title"], ex))
                    time.sleep(3)
            else:
                continue
            point["epochs"].append({
                "id": "ep-kuula-" + e["id"],
                "year": e["y"], "month": e["mo"],
                "note": "%d/%02d/%02d %s %s" % (e["y"], e["mo"], e["d"], e["rest"], tag),
                "src": url, "yawOffset": 0, "isPrivate": False,
            })
            added += 1
            print("已上傳 %s" % e["title"])
            # 每張傳完就存一次狀態，中斷後重跑不會遺失進度
            requests.post(site + "/api/state", headers=headers, json=state, timeout=120).raise_for_status()

    requests.post(site + "/api/state", headers=headers, json=state, timeout=120).raise_for_status()
    print("完成，新增 %d 個時期。請重新整理網站查看。" % added)


if __name__ == "__main__":
    main()
