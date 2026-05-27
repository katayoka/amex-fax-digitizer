"""
ai_extractor.py
Anthropic Vision APIを使ったFAX明細OCR＆広告費自動集計モジュール
"""

import anthropic
import base64
import json
import re
import os
from pathlib import Path
import fitz  # PyMuPDF
import pandas as pd
from typing import Optional

# =====================================================================
# 定数
# =====================================================================
COLUMNS = [
    "按分金額(税込)", "税率", "税額", "備考", "ブランド", "商品コード",
    "按分仕訳勘定(税込)", "費用集計セグメント(任意)", "コストセンター",
    "リソースタイプ(SC0267選択時必須)", "プラットフォーム", "ジョブコード",
    "発生源", "数量", "単位"
]

# 広告パターン定義（正規表現）
# Google広告として判定するパターン：
#   ① "GOOGLE" 単体（広告代理店カテゴリ前提）
#   ② "GOOGLE*ADS〇〇〇 GOOGLE JAPAN G." 形式
#   ③ 従来の Google Ads / Google*Ads 等
AD_PATTERNS = {
    "Google広告": re.compile(
        r"GOOGLE\*ADS\w*"
        r"|GOOGLE\s+JAPAN"
        r"|Google\s*[\*\-\s]?\s*(Ads?|広告|Adwords?|AdSense)"
        r"|GOOGLE\s*[\*\-]\s*(ADS?|ADWORDS?)"
        r"|(?<![A-Z0-9\*])GOOGLE(?!\s*(ONE|PLAY|MAPS|DRIVE|PAY|\*GOOGLE))",
        re.IGNORECASE,
    ),
    "Meta広告": re.compile(
        r"(Meta\s*[\*\-\s]?\s*(Ads?|広告)|"
        r"META\s*[\*\-\s]?\s*(ADS?)|"
        r"FACEBK\s*[\*\-\s]?\w*|"                  # FACEBK *HAXQGHDJE2 等
        r"Facebook\s*[\*\-\s]?\s*(Ads?|広告)|"
        r"FACEBOOK\s*[\*\-\s]?\s*(ADS?))",
        re.IGNORECASE,
    ),
}

# =====================================================================
# Step 1: PDF/画像 → base64画像リスト
# =====================================================================

def file_to_images_b64(file_bytes: bytes, filename: str) -> list[dict]:
    """
    PDF or 画像ファイルを受け取り、
    [{"media_type": "image/jpeg", "data": "<b64>"}] のリストを返す。
    PDFは全ページをJPEGにラスタライズ（150 DPI）。
    """
    ext = Path(filename).suffix.lower()
    images = []

    if ext == ".pdf":
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page in doc:
            mat = fitz.Matrix(150 / 72, 150 / 72)  # 150 DPI
            pix = page.get_pixmap(matrix=mat, alpha=False)
            jpeg_bytes = pix.tobytes("jpeg")
            images.append({
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(jpeg_bytes).decode("utf-8"),
            })
    else:
        # PNG / JPG / JPEG はそのままbase64
        media_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
        media_type = media_map.get(ext, "image/jpeg")
        images.append({
            "media_type": media_type,
            "data": base64.standard_b64encode(file_bytes).decode("utf-8"),
        })

    return images


# =====================================================================
# Step 2: Anthropic Vision API でOCR
# =====================================================================

SYSTEM_PROMPT = """あなたはAMEX（アメリカン・エキスプレス）のFAX明細を読み取る経理専門AIです。
与えられた明細画像を正確に読み取り、以下のJSON形式で全明細行を出力してください。

出力ルール：
1. 必ずJSON配列のみを出力し、前後に余分なテキストを付けないこと。
2. 各行は以下のキーを持つオブジェクトとする：
   - vendor: 加盟店名（原文のまま）
   - amount: 税込金額（数値、円）
   - date: 利用日（YYYY-MM-DD形式、不明な場合はnull）
   - tax_rate: 税率（"10%"/"8%"/"0%不課税" のいずれか、不明なら"10%"）
   - raw_description: 明細の生テキスト（原文のまま）
3. 金額は数値型（カンマ・円記号なし）で出力すること。
4. 1枚の明細画像に複数行ある場合はすべて出力すること。
5. 合計行・小計行は除外すること。

出力例：
[
  {"vendor": "Google*Ads1234567", "amount": 55000, "date": "2024-05-03", "tax_rate": "10%", "raw_description": "Google*Ads1234567 55,000円"},
  {"vendor": "〇〇事務用品", "amount": 15400, "date": "2024-05-10", "tax_rate": "10%", "raw_description": "〇〇事務用品店 15,400円"}
]"""


def ocr_with_claude(images_b64: list[dict], api_key: str, month_label: str) -> list[dict]:
    """
    画像リストをClaude Vision APIに送り、生の明細リストを取得する。
    """
    client = anthropic.Anthropic(api_key=api_key)

    # マルチページの場合は全ページを1リクエストに詰め込む
    content = []
    for i, img in enumerate(images_b64):
        content.append({"type": "text", "text": f"--- ページ {i+1} ---"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["data"],
            },
        })
    content.append({
        "type": "text",
        "text": f"上記のAMEX明細（{month_label}）を読み取り、全明細行をJSONで出力してください。"
    })

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    raw_text = response.content[0].text.strip()

    # JSON抽出（```json ... ``` ブロックへの対応）
    json_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw_text, re.DOTALL)
    if json_match:
        raw_text = json_match.group(1)

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        # フォールバック：配列部分だけ取り出す
        arr_match = re.search(r"\[.*\]", raw_text, re.DOTALL)
        if arr_match:
            return json.loads(arr_match.group(0))
        raise ValueError(f"Claude APIのレスポンスをJSONとして解析できませんでした:\n{raw_text}")


# =====================================================================
# Step 3: 広告費の自動集計（マージ）ロジック
# =====================================================================

def _detect_ad_type(vendor: str) -> Optional[str]:
    """加盟店名から広告種別を返す。非広告はNone。"""
    for ad_type, pattern in AD_PATTERNS.items():
        if pattern.search(vendor):
            return ad_type
    return None


def _derive_month(records: list[dict]) -> str:
    """レコード群から代表月ラベル（例: "5月"）を導出する。"""
    months = []
    for r in records:
        if r.get("date"):
            try:
                m = int(r["date"].split("-")[1])
                months.append(m)
            except Exception:
                pass
    if months:
        from collections import Counter
        most_common = Counter(months).most_common(1)[0][0]
        return f"{most_common}月"
    return "当月"


def merge_ad_records(raw_records: list[dict]) -> list[dict]:
    """
    生の明細レコードリストを受け取り、広告費を種別ごとに1行に集計して返す。
    非広告行はそのまま保持。
    """
    ad_groups: dict[str, list[dict]] = {}
    non_ad: list[dict] = []

    for rec in raw_records:
        ad_type = _detect_ad_type(rec.get("vendor", ""))
        if ad_type:
            ad_groups.setdefault(ad_type, []).append(rec)
        else:
            non_ad.append(rec)

    merged = []

    # 広告費グループを1行に集計
    for ad_type, recs in ad_groups.items():
        month_label = _derive_month(recs)
        total_amount = sum(r.get("amount", 0) for r in recs)
        count = len(recs)
        tax_rate = recs[0].get("tax_rate", "10%")

        # 税額計算
        tax_rate_num = {"10%": 10, "8%": 8, "0%不課税": 0}.get(tax_rate, 10)
        tax_amount = round(total_amount * tax_rate_num / (100 + tax_rate_num))

        merged.append({
            "_type": "ad",
            "vendor": ad_type,
            "amount": total_amount,
            "tax_amount": tax_amount,
            "tax_rate": tax_rate,
            "count": count,
            "month_label": month_label,
            "date": recs[0].get("date"),
        })

    # 非広告行
    for rec in non_ad:
        tax_rate = rec.get("tax_rate", "10%")
        tax_rate_num = {"10%": 10, "8%": 8, "0%不課税": 0}.get(tax_rate, 10)
        amount = rec.get("amount", 0)
        tax_amount = round(amount * tax_rate_num / (100 + tax_rate_num))

        merged.append({
            "_type": "other",
            "vendor": rec.get("vendor", ""),
            "amount": amount,
            "tax_amount": tax_amount,
            "tax_rate": tax_rate,
            "count": 1,
            "month_label": None,
            "date": rec.get("date"),
        })

    return merged


# =====================================================================
# Step 4: マージ済みレコード → COLUMNS 準拠 DataFrame
# =====================================================================

DEFAULT_VALUES = {
    "ブランド": "",
    "商品コード": "",
    "按分仕訳勘定(税込)": "SC1480 - VC Non-Chrg Costs",
    "費用集計セグメント(任意)": "",
    "コストセンター": "00331 Other Consumer COGs",
    "リソースタイプ(SC0267選択時必須)": "",
    "プラットフォーム": "",
    "ジョブコード": "",
    "発生源": "OG001 Local Market",
    "数量": 1,
    "単位": "EA-Each/件",
}

AD_ACCOUNT_MAP = {
    "Google広告": "SC1480 - VC Non-Chrg Costs",
    "Meta広告":   "SC1480 - VC Non-Chrg Costs",
}


def records_to_dataframe(merged_records: list[dict]) -> pd.DataFrame:
    """
    マージ済みレコードリストをCOLUMNS準拠のDataFrameに変換する。
    """
    rows = []
    for rec in merged_records:
        if rec["_type"] == "ad":
            備考 = f"{rec['vendor']} {rec['month_label']}度合算 (計{rec['count']}回分)"
            勘定 = AD_ACCOUNT_MAP.get(rec["vendor"], DEFAULT_VALUES["按分仕訳勘定(税込)"])
        else:
            備考 = rec["vendor"]
            勘定 = DEFAULT_VALUES["按分仕訳勘定(税込)"]

        row = {
            "按分金額(税込)": rec["amount"],
            "税率": rec["tax_rate"],
            "税額": rec["tax_amount"],
            "備考": 備考,
            "ブランド": DEFAULT_VALUES["ブランド"],
            "商品コード": DEFAULT_VALUES["商品コード"],
            "按分仕訳勘定(税込)": 勘定,
            "費用集計セグメント(任意)": DEFAULT_VALUES["費用集計セグメント(任意)"],
            "コストセンター": DEFAULT_VALUES["コストセンター"],
            "リソースタイプ(SC0267選択時必須)": DEFAULT_VALUES["リソースタイプ(SC0267選択時必須)"],
            "プラットフォーム": DEFAULT_VALUES["プラットフォーム"],
            "ジョブコード": DEFAULT_VALUES["ジョブコード"],
            "発生源": DEFAULT_VALUES["発生源"],
            "数量": DEFAULT_VALUES["数量"],
            "単位": DEFAULT_VALUES["単位"],
        }
        rows.append(row)

    df = pd.DataFrame(rows, columns=COLUMNS)

    # 型を強制
    df["按分金額(税込)"] = df["按分金額(税込)"].astype(int)
    df["税額"] = df["税額"].astype(int)
    df["数量"] = df["数量"].astype(int)

    return df


# =====================================================================
# 広告内訳データ取得
# =====================================================================

def get_ad_details(raw_records: list[dict]) -> dict[str, list[tuple]]:
    """
    生レコードから広告種別ごとの内訳（日付・金額）を返す。
    Returns: {"Google広告": [("2024-05-01", 30000), ...], "Meta広告": [...]}
    """
    details: dict[str, list[tuple]] = {}
    for rec in raw_records:
        ad_type = _detect_ad_type(rec.get("vendor", ""))
        if ad_type:
            date_str = rec.get("date") or "日付不明"
            amount   = rec.get("amount", 0)
            details.setdefault(ad_type, []).append((date_str, amount))
    return details


# =====================================================================
# 公開エントリーポイント
# =====================================================================

def extract_and_aggregate(
    file_bytes: bytes,
    filename: str,
    month_label: str = "当月",
    api_key: Optional[str] = None,
) -> pd.DataFrame:
    """
    app.py から呼び出すメインエントリーポイント。

    Parameters
    ----------
    file_bytes  : アップロードされたファイルのバイト列
    filename    : 元のファイル名（拡張子判定に使用）
    month_label : 表示用の月ラベル（例: "2024年5月"）
    api_key     : Anthropic APIキー（Noneなら環境変数 ANTHROPIC_API_KEY を参照）

    Returns
    -------
    pd.DataFrame : COLUMNS準拠の集計済みDataFrame
    """
    if api_key is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY が環境変数に設定されていません。"
            ".envファイルまたはshellで export ANTHROPIC_API_KEY=sk-... を実行してください。"
        )

    # 1. ファイル → base64画像
    images_b64 = file_to_images_b64(file_bytes, filename)

    # 2. Claude Vision でOCR
    raw_records = ocr_with_claude(images_b64, api_key, month_label)

    # 3. 広告費を自動集計
    merged = merge_ad_records(raw_records)

    # 4. COLUMNS準拠DataFrameに変換
    df = records_to_dataframe(merged)

    # 5. 広告内訳データ（Step 2.6用）
    ad_details = get_ad_details(raw_records)

    return df, ad_details
