"""
bill_one_exporter.py
COLUMNS準拠DataFrameをBill One形式CSVに変換するモジュール
"""

import pandas as pd
import re
import io

# =====================================================================
# 定数
# =====================================================================

# Bill One CSVのカラム順（固定）
BILL_ONE_COLUMNS = [
    "prorated_amount",
    "tax_rate",
    "tax_amount",
    "description",
    "extension_field_1",
    "extension_field_5",
    "extension_field_6",
    "extension_field_7",
    "extension_field_8",
    "extension_field_9",
    "extension_field_10",
    "extension_field_11",
    "extension_field_12",
    "extension_field_13",
    "extension_field_14",
    "extension_field_15",
]

# 税率変換：アプリ内表記 → Bill One CSVインポート用コード
TAX_RATE_MAP = {
    "10%":      "JPN_1000_CTax",
    "8%":       "JPN_800_CTax",
    "0%不課税":  "JPN_000_CTax_Untaxable",
}

# ブランド名 → extension_field_1 ID（0埋め4桁テキスト）
# extension_field_8の先頭文字列で判定
BRAND_KEYWORD_MAP = {
    "GQ":    "0120",
    "VOGUE": "0100",
    "WIRED": "0175",
}

# extension_field_1（ブランドID）のデフォルト
DEFAULT_BRAND_ID = "0120"  # 判定できない場合はGQ


# =====================================================================
# ヘルパー関数
# =====================================================================

def _extract_brand_id(row: pd.Series) -> int:
    """
    extension_field_8（備考）の先頭キーワードからブランドIDを判定する。
    備考の例：
      "GQ SHOP SHOPIFY利用料"   → 120
      "VOGUE Collection ..."    → 100
      "WIRED SZ ..."            → 175
    判定できない場合はextension_field_1（ブランド列）の値を使う。
    それも空なら DEFAULT_BRAND_ID を返す。
    """
    desc = str(row.get("備考", "") or "").strip().upper()
    for keyword, brand_id in BRAND_KEYWORD_MAP.items():
        if desc.startswith(keyword):
            return brand_id

    # 備考で判定できない場合はブランド列を参照
    brand = str(row.get("ブランド", "") or "").strip().upper()
    for keyword, brand_id in BRAND_KEYWORD_MAP.items():
        if keyword in brand:
            return brand_id

    return DEFAULT_BRAND_ID


def _extract_sc_code(value: str) -> str:
    """
    "SC1480 - VC Non-Chrg Costs" → "SC1480"
    "SC1118" → "SC1118"
    """
    if not value:
        return ""
    match = re.match(r"(SC\d+)", str(value).strip())
    return match.group(1) if match else str(value).strip()


def _extract_cost_center(value: str) -> str:
    """
    "00331 Other Consumer COGs" → "00331"
    "331" → "00331"
    数値部分を抽出し、常に先頭に"00"を付けた文字列で返す
    """
    if not value:
        return ""
    match = re.search(r"(\d+)", str(value).strip())
    if match:
        digits = match.group(1).lstrip("0") or "0"  # 純粋な数値部分
        return "00" + digits
    return str(value).strip()


def _extract_pt_code(value: str) -> str:
    """
    "PT133 Vogue collection, GQ Shop" → "PT133"
    "PT003" → "PT003"
    """
    if not value:
        return ""
    match = re.match(r"(PT\d+)", str(value).strip())
    return match.group(1) if match else str(value).strip()


def _extract_og_code(value: str) -> str:
    """
    "OG001 Local Market" → "OG001"
    "OG001" → "OG001"
    """
    if not value:
        return ""
    match = re.match(r"(OG\d+)", str(value).strip())
    return match.group(1) if match else str(value).strip()


def _calc_tax_amount(amount: int, tax_rate_str: str) -> int:
    """
    税込金額と税率文字列から税額を計算する。
    ・10% → round(amount * 10 / 110)
    ・8%  → round(amount * 8  / 108)
    ・0%不課税 → 0
    """
    rate_map = {"10%": 10, "8%": 8, "0%不課税": 0}
    rate = rate_map.get(tax_rate_str, 10)
    if rate == 0:
        return 0
    return round(amount * rate / (100 + rate))


def _extract_unit(value: str) -> str:
    """
    "EA-Each/件" → "EA"
    "EA" → "EA"
    """
    if not value:
        return "EA"
    match = re.match(r"(EA|PC|SET|BOX)", str(value).strip(), re.IGNORECASE)
    return match.group(1).upper() if match else "EA"


# =====================================================================
# メイン変換関数
# =====================================================================

def to_bill_one_df(source_df: pd.DataFrame) -> pd.DataFrame:
    """
    COLUMNS準拠DataFrameをBill One形式DataFrameに変換する。

    Parameters
    ----------
    source_df : app.py の st.session_state.df（確定済み）

    Returns
    -------
    pd.DataFrame : Bill One CSVカラム順のDataFrame
    """
    rows = []

    for _, row in source_df.iterrows():
        amount    = int(row.get("按分金額(税込)", 0) or 0)
        tax_rate  = str(row.get("税率", "10%") or "10%")
        備考       = str(row.get("備考", "") or "")

        # 税額：アプリ上の値を優先、0なら再計算
        tax_amount_raw = row.get("税額", None)
        if tax_amount_raw is not None and str(tax_amount_raw).strip() not in ("", "nan"):
            tax_amount = int(tax_amount_raw)
        else:
            tax_amount = _calc_tax_amount(amount, tax_rate)

        # 税率が0%不課税なら税額を強制0
        if tax_rate == "0%不課税":
            tax_amount = 0

        # ラベル（"消費税 10％"等）またはアプリ内表記（"10%"等）の両方に対応
        # まずアプリ内表記→ラベル→コードの順で変換を試みる
        from master import TAX_RATE_LABEL_TO_CODE, TAX_RATE_APP_TO_BILLONE
        label = TAX_RATE_APP_TO_BILLONE.get(tax_rate, tax_rate)  # "10%" → "消費税 10％"
        tax_code = TAX_RATE_LABEL_TO_CODE.get(label,
                   TAX_RATE_LABEL_TO_CODE.get(tax_rate,
                   TAX_RATE_MAP.get(tax_rate, "JPN_1000_CTax")))  # 最終フォールバック

        bill_row = {
            "prorated_amount":  amount,
            "tax_rate":         tax_code,
            "tax_amount":       tax_amount,
            "description":      "",                                         # 任意項目（空欄）
            "extension_field_1":  _extract_brand_id(row),                  # ブランドID
            "extension_field_5":  _extract_sc_code(str(row.get("按分仕訳勘定(税込)", "") or "")),
            "extension_field_6":  _extract_cost_center(str(row.get("コストセンター", "") or "")),
            "extension_field_7":  _extract_pt_code(str(row.get("プラットフォーム", "") or "")),
            "extension_field_8":  備考,                                     # 備考をそのまま
            "extension_field_9":  str(row.get("リソースタイプ(SC0267選択時必須)", "") or ""),
            "extension_field_10": str(row.get("費用集計セグメント(任意)", "") or ""),
            "extension_field_11": "",                                       # 常に空
            "extension_field_12": str(row.get("ジョブコード", "") or ""),
            "extension_field_13": _extract_og_code(str(row.get("発生源", "") or "")),
            "extension_field_14": int(row.get("数量", 1) or 1),
            "extension_field_15": _extract_unit(str(row.get("単位", "EA") or "EA")),
        }
        rows.append(bill_row)

    return pd.DataFrame(rows, columns=BILL_ONE_COLUMNS)


def to_bill_one_csv_bytes(source_df: pd.DataFrame) -> bytes:
    """
    Bill One形式DataFrameをUTF-8 CSVのバイト列として返す。
    Streamlit の st.download_button に渡す用。
    """
    bill_df = to_bill_one_df(source_df)
    buf = io.StringIO()
    bill_df.to_csv(buf, index=False, encoding="utf-8")
    return buf.getvalue().encode("utf-8")
