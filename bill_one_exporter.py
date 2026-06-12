"""
bill_one_exporter.py
COLUMNS準拠DataFrameをBill One形式CSVに変換するモジュール
Excelマスタ（billone登録内容のチェック）に基づく正式カラム順
"""

import pandas as pd
import re
import io

# =====================================================================
# Bill One CSVカラム順（固定・Excelマスタ準拠）
# =====================================================================
BILL_ONE_COLUMNS = [
    "prorated_amount",      # 按分金額(税込)
    "tax_rate",             # 税率コード
    "tax_amount",           # 税額
    "description",          # 備考（任意・空欄）
    "extension_field_1",    # ブランドID (0120/0100/0175)
    "extension_field_5",    # SCコード (SC1480等)
    "extension_field_6",    # コストセンター (00331/00780)
    "extension_field_7",    # PT番号 (PT133/PT003)
    "extension_field_8",    # 正式備考名
    "extension_field_9",    # リソースタイプ
    "extension_field_10",   # 費用集計セグメント
    "extension_field_11",   # 空欄固定
    "extension_field_12",   # ジョブコード
    "extension_field_13",   # 発生源 (OG001)
    "extension_field_14",   # 数量
    "extension_field_15",   # 単位 (EA)
]

# =====================================================================
# 税率コード（Bill One CSVインポート用）
# =====================================================================
# アプリ内表記 → Bill One CSVコード
TAX_RATE_MAP = {
    "10%":      "JPN_1000_CTax",
    "8%":       "JPN_800_CTax",
    "0%不課税":  "JPN_000_CTax_Untaxable",
}

# 不課税・免税・非課税コード一覧（税額=0にするもの）
ZERO_TAX_CODES = {
    "JPN_000_CTax_Untaxable",
    "JPN_000_CTax_Non_Taxable",
    "JPN_000_CTax_Exemption",
}

# =====================================================================
# ブランド関連定数
# =====================================================================
BRAND_KEYWORD_MAP = {
    "GQ":    "0120",
    "VOGUE": "0100",
    "WIRED": "0175",
}
DEFAULT_BRAND_ID = "0120"  # 判定できない場合はGQ

# 広告費の正式備考名キーワード → ブランドID（通常のブランド判定より優先）
AD_FORMAL_NAME_BRAND_MAP = {
    "Google AD出稿費":  "0175",   # WIRED
    "Meta AD出稿費":    "0100",   # VOGUE
}

# Google広告専用SC・PT（他の費用と異なる）
GOOGLE_AD_SC = "SC1118"
GOOGLE_AD_PT = "PT003"

# ジョブコードを固定で入れる備考名キーワード
JOB_CODE_MAP = {
    "GOOGLE ONE利用料":                  "JP_FIN000056",
    "GQ SHOP Power My Analytics利用料":  "JP_FIN000056",
    "GQ SHOP SHOPIFY 利用料":            "JP_FIN000056",
    "GQ SHOP PADDLE NETアカウント利用料": "JP_FIN000056",
    "PAYPAL *CANVA利用料":               "JP_FIN000056",
}

# ブランドIDに対応するコストセンター（Excelマスタより）
BRAND_COST_CENTER_MAP = {
    "0120": "00331",   # GQ    → Other Consumer COGs
    "0100": "00780",   # VOGUE → Vogue Collection Cost of Sales
    "0175": "00331",   # WIRED → Other Consumer COGs
}

# ブランドIDに対応するPT番号（Excelマスタより）
BRAND_PT_MAP = {
    "0120": "PT133",   # GQ
    "0100": "PT133",   # VOGUE
    "0175": "PT003",   # WIRED
}

# =====================================================================
# ヘルパー関数
# =====================================================================

def _extract_brand_id(row: pd.Series) -> str:
    """備考の正式名称からブランドIDを判定する。広告費は専用マップを優先。"""
    desc = str(row.get("備考", "") or "").strip()

    # ① 広告費専用マップ（正式備考名の末尾キーワードで判定）
    for keyword, brand_id in AD_FORMAL_NAME_BRAND_MAP.items():
        if keyword in desc:
            return brand_id

    # ② 通常ブランドキーワード（先頭一致）
    desc_upper = desc.upper()
    for keyword, brand_id in BRAND_KEYWORD_MAP.items():
        if desc_upper.startswith(keyword):
            return brand_id

    # ③ ブランド列フォールバック
    brand = str(row.get("ブランド", "") or "").strip().upper()
    for keyword, brand_id in BRAND_KEYWORD_MAP.items():
        if keyword in brand:
            return brand_id

    return DEFAULT_BRAND_ID


def _extract_sc_code(value: str) -> str:
    """"SC1480 - VC Non Circ Costs" → "SC1480" """
    if not value:
        return "SC1480"
    match = re.match(r"(SC\d+)", str(value).strip())
    return match.group(1) if match else str(value).strip()


def _extract_og_code(value: str) -> str:
    """"OG001 Local Market" → "OG001" """
    if not value:
        return "OG001"
    match = re.match(r"(OG\d+)", str(value).strip())
    return match.group(1) if match else str(value).strip()


def _extract_unit(value: str) -> str:
    """"EA-Each/件" → "EA" """
    if not value:
        return "EA"
    match = re.match(r"(EA|PC|SET|BOX)", str(value).strip(), re.IGNORECASE)
    return match.group(1).upper() if match else "EA"


def _get_tax_code(tax_rate_input: str) -> str:
    """
    税率入力（アプリ内表記またはBill Oneラベル）をCSVコードに変換。
    例:
      "10%"                          → "JPN_1000_CTax"
      "消費税 10％"                   → "JPN_1000_CTax"
      "消費税 10％-非適格事業者(80%控除)" → "JPN_Unqualified_1000_CTax"
    """
    from master import TAX_RATE_LABEL_TO_CODE, TAX_RATE_APP_TO_BILLONE
    val = str(tax_rate_input or "").strip()

    # ① まずラベル→コードを試みる（Step2.5確定値）
    if val in TAX_RATE_LABEL_TO_CODE:
        return TAX_RATE_LABEL_TO_CODE[val]

    # ② アプリ内表記→ラベル→コード
    label = TAX_RATE_APP_TO_BILLONE.get(val, val)
    if label in TAX_RATE_LABEL_TO_CODE:
        return TAX_RATE_LABEL_TO_CODE[label]

    # ③ 直接マップ
    if val in TAX_RATE_MAP:
        return TAX_RATE_MAP[val]

    return "JPN_1000_CTax"  # フォールバック


def _calc_tax_amount(amount: int, tax_code: str) -> int:
    """
    税込金額と税率コードから税額を計算する。
    不課税・非課税・免税コードは0を返す。
    """
    if tax_code in ZERO_TAX_CODES:
        return 0
    rate_map = {
        "JPN_1000_CTax": 10,
        "JPN_800_CTax": 8,
        "JPN_Unqualified_1000_CTax": 10,
        "JPN_Unqualified_800_CTax": 8,
        "JPN_1000_CTax_Postage_Stamps": 10,
        "JPN_1000_CTax_Public_Transportation": 10,
    }
    rate = rate_map.get(tax_code, 10)
    return round(amount * rate / (100 + rate))


# =====================================================================
# メイン変換関数
# =====================================================================

def to_bill_one_df(source_df: pd.DataFrame) -> pd.DataFrame:
    """
    COLUMNS準拠DataFrameをBill One形式DataFrameに変換する。
    """
    rows = []

    for _, row in source_df.iterrows():
        amount    = int(row.get("按分金額(税込)", 0) or 0)
        tax_rate_input = str(row.get("税率", "10%") or "10%")

        # 税率コード取得
        tax_code = _get_tax_code(tax_rate_input)

        # 税額計算（コードに基づいて正確に計算）
        tax_amount = _calc_tax_amount(amount, tax_code)

        # ブランドID
        brand_id = _extract_brand_id(row)

        備考str = str(row.get("備考", "") or "")

        # Google広告はSC・PTが専用値
        is_google_ad = "Google AD出稿費" in 備考str
        sc_code = GOOGLE_AD_SC if is_google_ad else _extract_sc_code(str(row.get("按分仕訳勘定(税込)", "") or ""))
        pt_code = GOOGLE_AD_PT if is_google_ad else BRAND_PT_MAP.get(brand_id, "PT133")

        # ジョブコード：マスタに定義されていれば固定値、なければDataFrameの値
        job_code = ""
        for kw, jc in JOB_CODE_MAP.items():
            if kw in 備考str:
                job_code = jc
                break
        if not job_code:
            job_code = str(row.get("ジョブコード", "") or "")

        bill_row = {
            "prorated_amount":   amount,
            "tax_rate":          tax_code,
            "tax_amount":        tax_amount,
            "description":       "",
            "extension_field_1": str(brand_id),
            "extension_field_5": sc_code,
            "extension_field_6": str(BRAND_COST_CENTER_MAP.get(brand_id, "00331")),
            "extension_field_7": pt_code,
            "extension_field_8": str(row.get("備考", "") or ""),
            "extension_field_9": str(row.get("リソースタイプ(SC0267選択時必須)", "") or ""),
            "extension_field_10": str(row.get("費用集計セグメント(任意)", "") or ""),
            "extension_field_11": "",
            "extension_field_12": job_code,
            "extension_field_13": _extract_og_code(str(row.get("発生源", "") or "")),
            "extension_field_14": int(row.get("数量", 1) or 1),
            "extension_field_15": _extract_unit(str(row.get("単位", "EA") or "EA")),
        }
        rows.append(bill_row)

    return pd.DataFrame(rows, columns=BILL_ONE_COLUMNS)


def to_bill_one_csv_bytes(source_df: pd.DataFrame) -> bytes:
    """Bill One形式DataFrameをUTF-8 CSVのバイト列として返す"""
    bill_df = to_bill_one_df(source_df)
    buf = io.StringIO()
    bill_df.to_csv(buf, index=False, encoding="utf-8")
    return buf.getvalue().encode("utf-8")
