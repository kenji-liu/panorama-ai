# -*- coding: utf-8 -*-
"""
現勘環景導覽系統 — 後端服務（多供應商可切換版）
=========================================================
- 提供靜態前端網頁（panorama-tour_5.html）
- 提供 AI 影像分析 API：
    POST /api/analyze   單張視角：損壞／異常辨識 + 現勘描述
    POST /api/compare   兩個時期視角：跨時期變化偵測 + 比對報告
    POST /api/calibrate 兩個時期視角：自動方位校準建議

只靠一個環境變數 AI_PROVIDER 切換供應商，端點與前端完全不變：
    AI_PROVIDER=gemini   Google Gemini 免費額度（額度較寬，預設）→ 設 GEMINI_API_KEY
    AI_PROVIDER=groq     Groq 免費額度（Llama 4 視覺）          → 設 GROQ_API_KEY
    AI_PROVIDER=claude   Anthropic Claude 付費（品質最高）       → 設 ANTHROPIC_API_KEY
    AI_PROVIDER=custom   任何 OpenAI 相容端點（OpenRouter/Ollama 等）
                         → 設 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY

各供應商可選的模型覆寫變數：GEMINI_MODEL / GROQ_MODEL / ANTHROPIC_MODEL / LLM_MODEL
日常用免費（gemini 或 groq），重要案件改 AI_PROVIDER=claude 即可，無需改程式。
"""

import os
import io
import json
import time
import hmac
import uuid
import hashlib
import base64
import binascii

from flask import Flask, request, jsonify, send_from_directory, Response, abort

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # 上傳上限 40MB（單張全景圖）

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = "panorama-tour_5.html"

MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1500"))
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # AI 分析的截圖上限，約 4MB
BUILD_ID = "2026-07-14-groq-plain-json-v2"

# =========================================================
# 雲端資料庫（Cloudflare R2，S3 相容）+ 管理者驗證
#   公開讀取：GET /api/state、GET /api/asset/<key>
#   管理者寫入（需密碼）：POST /api/state、POST /api/upload-image
# 環境變數：
#   R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET
#   ADMIN_PASSWORD（管理者上傳密碼）
# =========================================================
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
STATE_KEY = "state.json"
EXT_BY_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

_s3 = None


def storage_ready():
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET)


def get_s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config
        _s3 = boto3.client(
            "s3",
            endpoint_url="https://%s.r2.cloudflarestorage.com" % R2_ACCOUNT_ID,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    return _s3


ADMIN_COOKIE = "pano_admin"


def admin_token():
    return hashlib.sha256(("pano-admin:" + ADMIN_PASSWORD).encode("utf-8")).hexdigest()


def is_admin(req):
    if not ADMIN_PASSWORD:
        return False
    if hmac.compare_digest(req.headers.get("X-Admin-Password", ""), ADMIN_PASSWORD):
        return True
    # <img> 與 CSS 背景圖無法帶自訂標頭，改以登入時下發的 cookie 讓管理者看得到私人圖片
    return hmac.compare_digest(req.cookies.get(ADMIN_COOKIE, ""), admin_token())


EMPTY_STATE = {"version": 1, "scenes": [], "basemap": None, "points": []}


@app.route("/api/admin-check", methods=["POST"])
def api_admin_check():
    data = request.get_json(silent=True) or {}
    ok = bool(ADMIN_PASSWORD) and hmac.compare_digest(str(data.get("password", "")), ADMIN_PASSWORD)
    resp = jsonify({"ok": ok, "admin_enabled": bool(ADMIN_PASSWORD)})
    if ok:
        resp.set_cookie(ADMIN_COOKIE, admin_token(), max_age=60 * 60 * 24 * 90,
                        httponly=True, samesite="Lax", secure=request.is_secure)
    return resp


@app.route("/api/admin-logout", methods=["POST"])
def api_admin_logout():
    resp = jsonify({"ok": True})
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


def filter_public_state(state):
    """移除標為 isPrivate 的場景／點位／時期，以及只指向它們的熱點與 AI 紀錄（訪客用）。"""
    scenes = [s for s in state.get("scenes", []) if not s.get("isPrivate")]
    scene_ids = {s.get("id") for s in scenes}
    for s in scenes:
        s["hotspots"] = [h for h in s.get("hotspots", []) if h.get("targetId") in scene_ids]
    points = []
    for p in state.get("points", []):
        if p.get("isPrivate"):
            continue
        p["epochs"] = [e for e in p.get("epochs", []) if not e.get("isPrivate")]
        points.append(p)
    loc_ids = scene_ids | {p.get("id") for p in points}
    out = dict(state)
    out["scenes"] = scenes
    out["points"] = points
    out["analysisRecords"] = [r for r in state.get("analysisRecords", []) if r.get("locationId") in loc_ids]
    return out


def private_asset_keys(state):
    keys = set()

    def key_of(src):
        prefix = "/api/asset/"
        return src[len(prefix):] if isinstance(src, str) and src.startswith(prefix) else None

    for s in state.get("scenes", []):
        if s.get("isPrivate") and key_of(s.get("src")):
            keys.add(key_of(s["src"]))
    for p in state.get("points", []):
        for e in p.get("epochs", []):
            if (p.get("isPrivate") or e.get("isPrivate")) and key_of(e.get("src")):
                keys.add(key_of(e["src"]))
    return keys


_private_cache = {"at": 0.0, "keys": set()}


def get_private_keys():
    now = time.time()
    if now - _private_cache["at"] > 10:
        try:
            obj = get_s3().get_object(Bucket=R2_BUCKET, Key=STATE_KEY)
            _private_cache["keys"] = private_asset_keys(json.loads(obj["Body"].read()))
        except Exception:  # noqa: BLE001
            _private_cache["keys"] = set()
        _private_cache["at"] = now
    return _private_cache["keys"]


@app.route("/api/state", methods=["GET"])
def api_get_state():
    if not storage_ready():
        return jsonify(EMPTY_STATE)
    try:
        obj = get_s3().get_object(Bucket=R2_BUCKET, Key=STATE_KEY)
        state = json.loads(obj["Body"].read())
    except Exception:  # noqa: BLE001  尚無資料時回傳空狀態
        return jsonify(EMPTY_STATE)
    resp = jsonify(state if is_admin(request) else filter_public_state(state))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/state", methods=["POST"])
def api_put_state():
    if not is_admin(request):
        return jsonify({"error": "未授權，請以管理者身分登入"}), 401
    if not storage_ready():
        return jsonify({"error": "後端尚未設定 R2 儲存環境變數"}), 503
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "資料格式錯誤"}), 400
    try:
        get_s3().put_object(
            Bucket=R2_BUCKET, Key=STATE_KEY,
            Body=json.dumps(data).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "儲存失敗：" + str(e)}), 502
    return jsonify({"ok": True})


@app.route("/api/upload-image", methods=["POST"])
def api_upload_image():
    if not is_admin(request):
        return jsonify({"error": "未授權，請以管理者身分登入"}), 401
    if not storage_ready():
        return jsonify({"error": "後端尚未設定 R2 儲存環境變數"}), 503
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "缺少檔案"}), 400
    mime = (f.mimetype or "").lower()
    ext = EXT_BY_MIME.get(mime)
    if not ext:
        return jsonify({"error": "不支援的影像格式：" + mime}), 400
    key = "assets/%s.%s" % (uuid.uuid4().hex, ext)
    try:
        get_s3().upload_fileobj(
            f, R2_BUCKET, key, ExtraArgs={"ContentType": mime},
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "上傳失敗：" + str(e)}), 502
    return jsonify({"key": key, "url": "/api/asset/" + key})


@app.route("/api/asset/<path:key>", methods=["GET"])
def api_asset(key):
    if not storage_ready():
        abort(404)
    if ".." in key or key.startswith("/"):
        abort(400)
    private = key in get_private_keys()
    if private and not is_admin(request):
        abort(404)
    try:
        obj = get_s3().get_object(Bucket=R2_BUCKET, Key=key)
        data = obj["Body"].read()
        ctype = obj.get("ContentType", "application/octet-stream")
    except Exception:  # noqa: BLE001
        abort(404)
    resp = Response(data, mimetype=ctype)
    resp.headers["Cache-Control"] = (
        "private, no-store" if private else "public, max-age=31536000, immutable"
    )
    return resp

# ---- 供應商設定表 ----
PROVIDERS = {
    "gemini": {
        "kind": "openai",
        "base": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        "keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY"],
    },
    "groq": {
        "kind": "openai",
        "base": "https://api.groq.com/openai/v1",
        "model": os.environ.get("GROQ_MODEL", "qwen/qwen3.6-27b"),
        "keys": ["GROQ_API_KEY", "LLM_API_KEY"],
    },
    "claude": {
        "kind": "anthropic",
        "base": None,
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8"),
        "keys": ["ANTHROPIC_API_KEY", "LLM_API_KEY"],
    },
    "custom": {
        "kind": "openai",
        "base": os.environ.get("LLM_BASE_URL", ""),
        "model": os.environ.get("LLM_MODEL", ""),
        "keys": ["LLM_API_KEY"],
    },
}

PROVIDER = os.environ.get("AI_PROVIDER", "gemini").lower()
if PROVIDER not in PROVIDERS:
    PROVIDER = "gemini"
CFG = PROVIDERS[PROVIDER]


def _api_key():
    for name in CFG["keys"]:
        v = os.environ.get(name)
        if v:
            return v
    return None


_openai_client = None
_anthropic_client = None


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        key = _api_key()
        if not key:
            raise RuntimeError("尚未設定 API 金鑰（%s）" % " / ".join(CFG["keys"]))
        _openai_client = OpenAI(api_key=key, base_url=CFG["base"] or None)
    return _openai_client


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        if not _api_key():
            raise RuntimeError("尚未設定 ANTHROPIC_API_KEY")
        _anthropic_client = anthropic.Anthropic(api_key=_api_key())
    return _anthropic_client


# ---- 輸出結構（以提示告訴模型，並用於前端渲染）----
ANALYZE_SHAPE = (
    '{'
    '"summary": "一句話總結畫面狀況", '
    '"overall_condition": "良好｜輕微異常｜中度損壞｜嚴重損壞｜無法判定 其中之一", '
    '"confidence": 0.0 到 1.0 的數值, '
    '"observations": [{"item": "觀察到的項目或異常", "severity": "資訊｜注意｜警告｜嚴重 其中之一", "location": "畫面中相對位置"}], '
    '"recommended_action": "巡查、量測、封鎖、修復或持續觀測等具體建議", '
    '"report": "可貼入現勘報告的中文敘述段落"'
    '}'
)
COMPARE_SHAPE = (
    '{'
    '"summary": "一句話總結兩期主要差異", '
    '"change_level": "無明顯變化｜輕微變化｜中度變化｜顯著變化｜無法判定 其中之一", '
    '"confidence": 0.0 到 1.0 的數值, '
    '"changes": [{"description": "變化內容", "change_type": "沖刷｜淤積｜結構位移｜植被變化｜新增設施｜損壞｜其他 其中之一", "severity": "資訊｜注意｜警告｜嚴重 其中之一", "location": "相對位置"}], '
    '"recommended_action": "針對變化提出的具體複核或處置建議", '
    '"report": "可貼入現勘報告的跨時期比對敘述段落"'
    '}'
)
CALIBRATE_SHAPE = (
    '{'
    '"aligned": true 或 false, '
    '"suggested_direction": "不需調整｜將第二期向左旋轉｜將第二期向右旋轉｜無法判定 其中之一", '
    '"landmark": "用來對齊的共同地標描述", '
    '"note": "給操作者的校準建議"'
    '}'
)

SYS_ANALYZE = (
    "你是台灣水利／水土保持工程的現勘判釋專家，協助判讀治理設施（魚道、固床工、防砂壩、護岸、平台等）"
    "的環景影像。請以繁體中文，客觀、具體地辨識畫面中的損壞或異常（裂縫、掏空、護坡破損、堆積物、"
    "結構傾斜、沖刷等），標明相對位置與嚴重度。信心值應反映遮蔽、解析度、光線及單一視角限制；若畫面資訊不足，誠實說明無法判定，不要臆測。\n"
    "務必只輸出符合以下結構的 JSON，不要加任何說明文字或程式碼框：\n" + ANALYZE_SHAPE
)
SYS_COMPARE = (
    "你是台灣水利／水土保持工程的現勘判釋專家。以下是同一監測點位、兩個不同時期的環景視角"
    "（第一張為較早時期，第二張為較晚時期）。請以繁體中文逐項比對，偵測跨時期變化"
    "（沖刷、淤積、結構位移、植被變化、新增設施、損壞等），標明位置與嚴重度。"
    "若方位未對齊或資訊不足請於報告說明，不要臆測無法佐證的變化。\n"
    "務必只輸出符合以下結構的 JSON，不要加任何說明文字或程式碼框：\n" + COMPARE_SHAPE
)
SYS_CALIBRATE = (
    "你是影像對位助手。以下是同一地點、兩個時期的環景視角。請找出可作為對齊基準的共同地標"
    "（電線桿、橋墩、田埂交界、固定構造物等），判斷第二張相對第一張需向左或向右旋轉才能對齊。\n"
    "務必只輸出符合以下結構的 JSON，不要加任何說明文字或程式碼框：\n" + CALIBRATE_SHAPE
)


def _split_data_url(data_url):
    """檢查並拆解 data:image/...;base64,xxx，回傳 (media_type, base64_data)。"""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        raise ValueError("影像格式不正確（需為 data URL）")
    try:
        header, b64 = data_url.split(",", 1)
        media_type = header.split(";")[0].split(":", 1)[1]
    except (ValueError, IndexError):
        raise ValueError("影像 data URL 解析失敗")
    if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        raise ValueError("不支援的影像格式：" + media_type)
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("影像 base64 解碼失敗")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("單張影像過大（請降低解析度）")
    return media_type, b64


def text_item(t):
    return {"type": "text", "text": t}


def image_item(data_url):
    _split_data_url(data_url)  # 先驗證；無效則丟 ValueError
    return {"type": "image", "data_url": data_url}


def _extract_json(text):
    """從模型輸出中萃取 JSON 物件（容忍 ```json 圍欄或前後雜訊）。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    try:
        return json.loads(s)
    except ValueError:
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(s[start:end + 1])
        raise ValueError("模型未回傳有效 JSON")


def _run_openai(system, items):
    client = _get_openai()
    content = []
    for it in items:
        if it["type"] == "text":
            content.append({"type": "text", "text": it["text"]})
        else:
            content.append({"type": "image_url", "image_url": {"url": it["data_url"]}})
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    # Groq 的 Qwen Vision 雖標示支援 JSON Mode，但複雜雙影像提示可能在
    # 服務端被 failed_generation / Failed to validate JSON 拒絕。Groq 改採
    # 一般模式並由 _extract_json 擷取，避免請求在模型完成前直接回傳 400。
    if PROVIDER == "groq":
        resp = client.chat.completions.create(
            model=CFG["model"], max_tokens=MAX_TOKENS, temperature=0.2, messages=messages,
        )
        return _extract_json(resp.choices[0].message.content)

    try:
        resp = client.chat.completions.create(
            model=CFG["model"], max_tokens=MAX_TOKENS, temperature=0.2,
            messages=messages, response_format={"type": "json_object"},
        )
    except Exception as e:  # 僅在端點不支援 JSON 模式時退回，避免 429 等錯誤被重送
        message = str(e).lower()
        if not any(token in message for token in (
            "response_format", "json_object", "json mode", "structured output", "not supported",
            "failed to validate json", "failed_generation"
        )):
            raise
        resp = client.chat.completions.create(
            model=CFG["model"], max_tokens=MAX_TOKENS, temperature=0.2, messages=messages,
        )
    return _extract_json(resp.choices[0].message.content)


def _run_anthropic(system, items):
    client = _get_anthropic()
    content = []
    for it in items:
        if it["type"] == "text":
            content.append({"type": "text", "text": it["text"]})
        else:
            mt, b64 = _split_data_url(it["data_url"])
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
    resp = client.messages.create(
        model=CFG["model"], max_tokens=MAX_TOKENS, system=system,
        messages=[{"role": "user", "content": content}],
    )
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError("模型基於安全考量婉拒此請求")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return _extract_json(text)


def run_llm(system, items):
    if CFG["kind"] == "anthropic":
        return _run_anthropic(system, items)
    return _run_openai(system, items)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json(silent=True) or {}
    try:
        img = image_item(data.get("image", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    point = (data.get("point_name") or "").strip()
    epoch = (data.get("epoch_label") or "").strip()
    items = [
        text_item("監測點位：%s。時期：%s。請辨識此畫面的損壞／異常並產生現勘描述。"
                  % (point or "未提供", epoch or "未提供")),
        img,
    ]
    try:
        return jsonify(run_llm(SYS_ANALYZE, items))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


@app.route("/api/compare", methods=["POST"])
def api_compare():
    data = request.get_json(silent=True) or {}
    try:
        img_a = image_item(data.get("image_a", ""))
        img_b = image_item(data.get("image_b", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    point = (data.get("point_name") or "").strip()
    label_a = (data.get("label_a") or "較早時期").strip()
    label_b = (data.get("label_b") or "較晚時期").strip()
    items = [
        text_item("監測點位：%s。第一張＝%s，第二張＝%s。請比對兩期並偵測變化。"
                  % (point or "未提供", label_a, label_b)),
        img_a, img_b,
    ]
    try:
        return jsonify(run_llm(SYS_COMPARE, items))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


@app.route("/api/calibrate", methods=["POST"])
def api_calibrate():
    data = request.get_json(silent=True) or {}
    try:
        img_a = image_item(data.get("image_a", ""))
        img_b = image_item(data.get("image_b", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    items = [
        text_item("第一張為基準時期，第二張為待對齊時期。請判斷第二張需向左或向右旋轉以對齊第一張。"),
        img_a, img_b,
    ]
    try:
        return jsonify(run_llm(SYS_CALIBRATE, items))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "build": BUILD_ID,
        "provider": PROVIDER,
        "kind": CFG["kind"],
        "model": CFG["model"],
        "key_set": bool(_api_key()),
        "storage_ready": storage_ready(),
        "admin_enabled": bool(ADMIN_PASSWORD),
    })


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, INDEX_FILE)


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(BASE_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
