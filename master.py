"""
master.py
支払先マスタ管理モジュール
- OCR備考名 → 正式備考名・税率の自動マッピング
- 未登録支払先の検出
- マスタのCRUD（将来のDB化に備えてインターフェースを統一）
"""

import re
from dataclasses import dataclass
from typing import Optional

# =====================================================================
# マスタデータ定義
# =====================================================================
@dataclass
class MasterEntry:
    keyword: str        # OCR結果とマッチさせるキーワード（部分一致）
    formal_name: str    # Bill One extension_field_8 に入れる正式備考名
    tax_rate: str       # "消費税 10％" / "消費税 0％ 不課税" / "消費税 8％"
    note: str = ""      # 備考（管理用）

# =====================================================================
# 初期マスタ（確定分）
# キーワードは上から順に評価される（より具体的なものを上に書く）
# =====================================================================
INITIAL_MASTER: list[MasterEntry] = [
    # --- Google系（広告以外） ---
    MasterEntry("GOOGLE*GOOGLE ONE",    "GOOGLE ONE利用料",                      "消費税 10％",      "Googleサービス"),
    MasterEntry("GOOGLE ONE",           "GOOGLE ONE利用料",                      "消費税 10％",      "Googleサービス"),

    # --- 広告費（ai_extractorで集約済みの備考名でマッチ） ---
    MasterEntry("VOGUE.*Google広告",    "VOGUE Collection Google AD出稿費",      "消費税 0％ 不課税", "広告費"),
    MasterEntry("WIRED.*Google広告",    "WIRED SZ Google AD出稿費",              "消費税 10％",      "広告費"),
    MasterEntry("GQ.*Google広告",       "GQ SHOP Google AD出稿費",               "消費税 0％ 不課税", "広告費"),
    MasterEntry("Google広告",           "Google AD出稿費",                       "消費税 0％ 不課税", "広告費・ブランド不明"),
    MasterEntry("Meta広告",             "Meta AD出稿費",                         "消費税 0％ 不課税", "広告費"),

    # --- 通信・電話 ---
    MasterEntry("NTT",                  "VOGUE Collection NTT電話利用料",        "消費税 10％",      "通信費"),

    # --- SaaSツール ---
    MasterEntry("POWER MY ANALYTICS",  "GQ SHOP Power My Analytics利用料",      "消費税 0％ 不課税", "ツール"),
    MasterEntry("LINE",                 "LINEアカウント料",                       "消費税 10％",      "ツール"),
    MasterEntry("SHOPIFY",              "GQ SHOP SHOPIFY 利用料",                "消費税 0％ 不課税", "ツール"),
    MasterEntry("PADDLE",               "GQ SHOP PADDLE NETアカウント利用料",    "消費税 10％",      "ツール"),
    MasterEntry("PAYPAL",               "PAYPAL *CANVA利用料",                   "消費税 0％ 不課税", "ツール"),
]

# =====================================================================
# セッション上のマスタ（実行時に追加エントリを追記可能）
# =====================================================================
_runtime_master: list[MasterEntry] = []


def get_master() -> list[MasterEntry]:
    """現在有効なマスタ全件を返す（初期 + 実行時追加）"""
    return INITIAL_MASTER + _runtime_master


def add_to_runtime_master(keyword: str, formal_name: str, tax_rate: str, note: str = "手動追加"):
    """実行時にマスタへエントリを追加する（セッション内のみ有効）"""
    _runtime_master.append(MasterEntry(keyword, formal_name, tax_rate, note))


# =====================================================================
# マッチング関数
# =====================================================================

# アプリ内表記（10%等）→ 表示ラベル（消費税 10％等）
TAX_RATE_APP_TO_BILLONE = {
    "10%":      "消費税 10％",
    "8%":       "消費税 8％",
    "0%不課税":  "消費税 0％ 不課税",
}

# 表示ラベル → アプリ内表記
TAX_RATE_BILLONE_TO_APP = {
    "消費税 10％":                        "10%",
    "消費税 8％":                         "8%",
    "消費税 0％ 不課税":                   "0%不課税",
    "消費税 0％ 非課税":                   "0%不課税",
    "消費税 0％ 免税":                     "0%不課税",
    "消費税 10％-郵便切手等特例":           "10%",
    "消費税 10％-出張旅費特例":             "10%",
    "消費税 10％-非適格事業者(80%控除)":   "10%",
    "消費税 8％-非適格事業者(80%控除)":    "8%",
}


@dataclass
class MatchResult:
    matched: bool
    formal_name: str
    tax_rate_billone: str   # "10%" / "8%" / "0%不課税"（app内表記）
    tax_rate_display: str   # "消費税 10％" 等（Bill One表記）
    keyword_matched: str


def lookup(ocr_text: str) -> MatchResult:
    """
    OCRで取得した備考テキストをマスタと照合する。

    Parameters
    ----------
    ocr_text : OCR結果の備考文字列

    Returns
    -------
    MatchResult : マッチ結果。matched=Falseなら未登録。
    """
    text = str(ocr_text or "").strip()

    for entry in get_master():
        try:
            if re.search(entry.keyword, text, re.IGNORECASE):
                billone_tax = TAX_RATE_APP_TO_BILLONE.get(entry.tax_rate, "10%")
                return MatchResult(
                    matched=True,
                    formal_name=entry.formal_name,
                    tax_rate_billone=billone_tax,
                    tax_rate_display=entry.tax_rate,
                    keyword_matched=entry.keyword,
                )
        except re.error:
            # 正規表現エラー時は単純部分一致にフォールバック
            if entry.keyword.lower() in text.lower():
                billone_tax = TAX_RATE_APP_TO_BILLONE.get(entry.tax_rate, "10%")
                return MatchResult(
                    matched=True,
                    formal_name=entry.formal_name,
                    tax_rate_billone=billone_tax,
                    tax_rate_display=entry.tax_rate,
                    keyword_matched=entry.keyword,
                )

    # 未登録
    return MatchResult(
        matched=False,
        formal_name=text,           # 元テキストをそのまま使用
        tax_rate_billone="10%",     # デフォルト（要確認フラグを立てる）
        tax_rate_display="消費税 10％",
        keyword_matched="",
    )


# =====================================================================
# Bill One税率選択肢・CSVインポートコード対応表
# =====================================================================

# 表示ラベル → Bill One CSVインポートコード
TAX_RATE_LABEL_TO_CODE = {
    "消費税 10％":                        "JPN_1000_CTax",
    "消費税 8％":                         "JPN_800_CTax",
    "消費税 0％ 不課税":                   "JPN_000_CTax_Untaxable",
    "消費税 0％ 非課税":                   "JPN_000_CTax_Non_Taxable",
    "消費税 0％ 免税":                     "JPN_000_CTax_Exemption",
    "消費税 10％-郵便切手等特例":           "JPN_1000_CTax_Postage_Stamps",
    "消費税 10％-出張旅費特例":             "JPN_1000_CTax_Public_Transportation",
    "消費税 10％-非適格事業者(80%控除)":   "JPN_Unqualified_1000_CTax",
    "消費税 8％-非適格事業者(80%控除)":    "JPN_Unqualified_800_CTax",
}

TAX_RATE_OPTIONS_BILLONE = list(TAX_RATE_LABEL_TO_CODE.keys())
TAX_RATE_OPTIONS_APP     = ["10%", "0%不課税", "8%"]
