import os
import streamlit as st
import pandas as pd

# ai_extractorは同ディレクトリに配置
from ai_extractor import extract_and_aggregate, COLUMNS
from bill_one_exporter import to_bill_one_csv_bytes
from master import lookup, add_to_runtime_master, TAX_RATE_OPTIONS_BILLONE, TAX_RATE_BILLONE_TO_APP

# =====================================================================
# ページ設定
# =====================================================================
st.set_page_config(layout="wide", page_title="AMEX FAX 明細データ化システム")

st.title("📄 AMEX FAX 明細データ化・管理システム")
st.caption("FAX明細の自動データ化、広告費の自動集計、および領収書の紐づけ管理")

# =====================================================================
# セッション状態の初期化
# =====================================================================
if "df" not in st.session_state:
    st.session_state.df = pd.DataFrame(columns=COLUMNS)
if "ocr_done" not in st.session_state:
    st.session_state.ocr_done = False
if "receipts" not in st.session_state:
    st.session_state.receipts = {}   # {idx: uploaded_file}
if "confirmed" not in st.session_state:
    st.session_state.confirmed = False
if "ad_details" not in st.session_state:
    st.session_state.ad_details = {}
if "new_charges_total" not in st.session_state:
    st.session_state.new_charges_total = None
if "tax_review" not in st.session_state:
    # {idx: {"formal_name": str, "tax_rate_display": str, "unmatched": bool}}
    st.session_state.tax_review = {}

# =====================================================================
# サイドバー：Step 1 FAX明細アップロード
# =====================================================================
st.sidebar.header("Step 1 ｜ FAX明細のアップロード")

# APIキー確認（環境変数優先、なければサイドバーで入力）
api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if not api_key:
    api_key = st.sidebar.text_input(
        "Anthropic APIキー（環境変数未設定の場合）",
        type="password",
        help="ANTHROPIC_API_KEY 環境変数が設定されていれば空欄でOKです",
    )

month_label = st.sidebar.text_input(
    "対象月（表示用）",
    value="2024年5月",
    help="例: 2024年5月  → Google広告 2024年5月度合算、と表示されます",
)

uploaded_file = st.sidebar.file_uploader(
    "AMEX明細ファイル（PDF / 画像）を選択",
    type=["pdf", "png", "jpg", "jpeg"],
)

run_btn = st.sidebar.button(
    "🤖 AI文字起こし ＆ 広告費自動集計を実行",
    disabled=(uploaded_file is None or not api_key),
    use_container_width=True,
)

if uploaded_file is None:
    st.sidebar.info("ファイルを選択してください")
elif not api_key:
    st.sidebar.warning("APIキーが必要です")

# =====================================================================
# Step 2 実行：OCR ＆ 集計
# =====================================================================
if run_btn and uploaded_file is not None and api_key:
    with st.spinner(
        f"🔍 Claude が '{uploaded_file.name}' を読み取っています…"
        "（ページ数によって数秒〜数十秒かかります）"
    ):
        try:
            df_result, ad_details, new_charges_total = extract_and_aggregate(
                file_bytes=uploaded_file.read(),
                filename=uploaded_file.name,
                month_label=month_label,
                api_key=api_key,
            )
            st.session_state.df = df_result
            st.session_state.ad_details = ad_details
            st.session_state.new_charges_total = new_charges_total
            st.session_state.ad_manual = {"Google広告": [], "Meta広告": []}
            st.session_state.ocr_done = True
            st.session_state.confirmed = False
            st.session_state.receipts = {}
            # マスタ照合：税率・正式備考名を自動セット
            tax_review = {}
            for idx, row in df_result.iterrows():
                result = lookup(str(row.get("備考", "") or ""))
                tax_review[idx] = {
                    "formal_name": result.formal_name,
                    "tax_rate_display": result.tax_rate_display,
                    "unmatched": not result.matched,
                }
            st.session_state.tax_review = tax_review
            st.sidebar.success(
                f"✅ データ化完了！  {len(df_result)} 行を抽出しました。"
            )
        except EnvironmentError as e:
            st.sidebar.error(f"APIキーエラー: {e}")
        except ValueError as e:
            st.sidebar.error(f"解析エラー: {e}")
        except Exception as e:
            st.sidebar.error(f"予期しないエラー: {e}")
            raise

# =====================================================================
# Step 2 表示：明細データの確認・修正
# =====================================================================
st.header("Step 2 ｜ 明細データの確認・修正")

if st.session_state.df.empty:
    st.info("← 左のサイドバーからFAX明細をアップロードし、AIを実行してください。")
else:
    st.write(
        "AIが抽出・集計したデータです。**画面上で直接修正可能**です。"
        "広告費は種別ごとに1行に自動集計されています。"
    )

    # カラム幅のカスタム設定
    # 税率・税額はStep2.5で確定するためStep2では非表示
    edited_df = st.data_editor(
        st.session_state.df,
        num_rows="dynamic",
        column_config={
            "按分金額(税込)": st.column_config.NumberColumn(
                "按分金額(税込)", format="¥%d", min_value=0
            ),
            "税率": None,
            "税額": None,
            "数量": st.column_config.NumberColumn(
                "数量", min_value=1, default=1
            ),
            "按分仕訳勘定(税込)": st.column_config.TextColumn(
                "按分仕訳勘定(税込)", width="medium"
            ),
            "備考": st.column_config.TextColumn("備考", width="large"),
            "リソースタイプ(SC0267選択時必須)": st.column_config.TextColumn(
                "リソースタイプ", width="small"
            ),
        },
        use_container_width=True,
        key="main_editor",
    )

    # 編集内容をセッションに反映
    st.session_state.df = edited_df

    # ------------------------------------------------------------------
    # Step 2.5：税率・正式備考名の確認
    # ------------------------------------------------------------------
    st.markdown("---")
    st.subheader("🏷️ Step 2.5 ｜ 税率・正式備考名の確認")

    if st.session_state.tax_review:
        unmatched_count = sum(1 for v in st.session_state.tax_review.values() if v["unmatched"])

        if unmatched_count > 0:
            st.warning(f"⚠️ {unmatched_count} 件がマスタ未登録です。税率と正式備考名を確認・選択してください。")
        else:
            st.success("✅ 全件マスタ照合済みです。内容を確認してください。")

        review_data = []
        for idx, row in edited_df.iterrows():
            rev = st.session_state.tax_review.get(idx, {})
            review_data.append({
                "idx": idx,
                "OCR備考（元データ）": str(row.get("備考", "")),
                "正式備考名（Bill One用）": rev.get("formal_name", str(row.get("備考", ""))),
                "税率": rev.get("tax_rate_display", "消費税 10％"),
                "未登録": rev.get("unmatched", False),
            })

        for item in review_data:
            idx = item["idx"]
            is_unmatched = item["未登録"]
            border_color = "#ff4b4b" if is_unmatched else "#21c354"
            label_icon = "🔴 未登録" if is_unmatched else "✅ 照合済"

            with st.expander(
                f"{label_icon}  {item['OCR備考（元データ）']}  →  {item['正式備考名（Bill One用）']}",
                expanded=True,
            ):
                col_name, col_tax = st.columns([2, 1])

                with col_name:
                    new_name = st.text_input(
                        "正式備考名（Bill One用）",
                        value=item["正式備考名（Bill One用）"],
                        key=f"fname_{idx}",
                    )

                with col_tax:
                    current_tax = item["税率"]
                    default_idx = TAX_RATE_OPTIONS_BILLONE.index(current_tax) if current_tax in TAX_RATE_OPTIONS_BILLONE else 0
                    new_tax = st.selectbox(
                        "税率",
                        options=TAX_RATE_OPTIONS_BILLONE,
                        index=default_idx,
                        key=f"taxsel_{idx}",
                    )

                # セッションに反映
                st.session_state.tax_review[idx] = {
                    "formal_name": new_name,
                    "tax_rate_display": new_tax,
                    "unmatched": is_unmatched,
                }

                if is_unmatched:
                    if st.button("📌 マスタに登録（このセッション内有効）", key=f"reg_{idx}"):
                        ocr_text = item["OCR備考（元データ）"]
                        # 最初の単語をキーワードとして登録
                        kw = ocr_text.split()[0] if ocr_text else ocr_text
                        add_to_runtime_master(kw, new_name, new_tax)
                        st.session_state.tax_review[idx]["unmatched"] = False
                        st.success(f"✅ 「{kw}」をマスタに登録しました")
                        st.rerun()

    # ------------------------------------------------------------------
    # Step 2.6：広告費内訳CSVダウンロード
    # ------------------------------------------------------------------
    st.markdown("---")
    st.subheader("📋 Step 2.6 ｜ 広告費内訳CSVダウンロード")

    # 広告内訳の手動補完データをセッションで管理
    if "ad_manual" not in st.session_state:
        st.session_state.ad_manual = {"Google広告": [], "Meta広告": []}

    def render_ad_detail(ad_type, file_suffix):
        import io as _io
        import pandas as _pd

        ocr_rows  = st.session_state.ad_details.get(ad_type, []) if st.session_state.ad_details else []
        manual_rows = st.session_state.ad_manual.get(ad_type, [])
        all_rows  = ocr_rows + manual_rows

        st.write(f"**{ad_type}内訳** （{len(all_rows)}件 / 合計 ¥{sum(r[1] for r in all_rows):,}）")

        if all_rows:
            df_view = _pd.DataFrame(all_rows, columns=["日付", "金額(税込)"])
            df_view["金額(税込)"] = df_view["金額(税込)"].apply(lambda x: f"¥{int(x):,}")
            st.dataframe(df_view, use_container_width=True, hide_index=True)

        # 手動追加フォーム
        with st.expander(f"➕ {ad_type}の取りこぼし行を手動追加"):
            c1, c2, c3 = st.columns([2, 2, 1])
            with c1:
                add_date = st.text_input("日付（YYYY-MM-DD）", key=f"add_date_{file_suffix}",
                                         placeholder="例: 2026-03-21")
            with c2:
                add_amount = st.number_input("金額（税込）", min_value=0, step=1,
                                             key=f"add_amt_{file_suffix}", format="%d")
            with c3:
                st.write("")
                st.write("")
                if st.button("追加", key=f"add_btn_{file_suffix}"):
                    if add_date and add_amount > 0:
                        st.session_state.ad_manual[ad_type].append((add_date, add_amount))

                        # Step2のdf内の合算行も金額を更新
                        for idx, row in st.session_state.df.iterrows():
                            if ad_type in str(row.get("備考", "")):
                                new_total = sum(r[1] for r in st.session_state.ad_details.get(ad_type, []))                                           + sum(r[1] for r in st.session_state.ad_manual[ad_type])
                                st.session_state.df.at[idx, "按分金額(税込)"] = new_total
                                # 税額再計算
                                tax_rate_str = str(row.get("税率", "10%"))
                                rate = {"10%": 10, "8%": 8, "0%不課税": 0}.get(tax_rate_str, 10)
                                st.session_state.df.at[idx, "税額"] = round(new_total * rate / (100 + rate)) if rate else 0
                                # 件数表示を更新
                                import re as _re
                                new_count = len(st.session_state.ad_details.get(ad_type, []))                                           + len(st.session_state.ad_manual[ad_type])
                                備考 = str(row.get("備考", ""))
                                備考 = _re.sub(r"計\d+回分", f"計{new_count}回分", 備考)
                                st.session_state.df.at[idx, "備考"] = 備考
                        st.success(f"追加しました（{add_date} / ¥{add_amount:,}）")
                        st.rerun()

        # 手動追加分の削除ボタン
        if manual_rows:
            with st.expander(f"🗑️ 手動追加分を削除"):
                for i, (d, a) in enumerate(manual_rows):
                    col_info, col_del = st.columns([3, 1])
                    col_info.write(f"{d}　¥{int(a):,}")
                    if col_del.button("削除", key=f"del_{file_suffix}_{i}"):
                        st.session_state.ad_manual[ad_type].pop(i)
                        st.rerun()

        # CSVダウンロード
        if all_rows:
            dl_df = _pd.DataFrame(all_rows, columns=["日付", "金額(税込)"])
            # 金額にコンマを付ける
            dl_df["金額(税込)"] = dl_df["金額(税込)"].apply(lambda x: f"{int(x):,}")
            # 合計行を追加
            total = sum(r[1] for r in all_rows)
            total_row = _pd.DataFrame([["合計", f"{total:,}"]], columns=["日付", "金額(税込)"])
            dl_df = _pd.concat([dl_df, total_row], ignore_index=True)
            dl_buf = _io.StringIO()
            dl_df.to_csv(dl_buf, index=False, encoding="utf-8")
            st.download_button(
                label=f"⬇️ {ad_type}内訳CSVをダウンロード",
                data=dl_buf.getvalue().encode("utf-8"),
                file_name=f"{file_suffix}_detail_{month_label.replace('年','').replace('月','')}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        else:
            st.info(f"{ad_type}の明細データがありません")

    col_g, col_m = st.columns(2)
    with col_g:
        render_ad_detail("Google広告", "google_ad")
    with col_m:
        render_ad_detail("Meta広告", "meta_ad")

    # ------------------------------------------------------------------
    # 金額集計セクション
    # ------------------------------------------------------------------
    st.markdown("---")
    st.subheader("📊 Step 3 ｜ 金額集計の検証（差額 ¥0 で確定可能）")

    total_calculated = int(edited_df["按分金額(税込)"].fillna(0).sum())

    # OCRで取得した新規ご利用金額を初期値にセット
    ocr_total = st.session_state.get("new_charges_total", None)
    invoice_default = int(ocr_total) if ocr_total is not None else total_calculated

    col_input, col_spacer = st.columns([1, 2])
    with col_input:
        total_invoice = st.number_input(
            "新規ご利用金額（税込）",
            min_value=0,
            value=invoice_default,
            step=1,
            help="PDFヘッダーの「新規ご利用金額」をAIが自動読み取りします。修正も可能です。",
        )
    if ocr_total is not None:
        st.caption(f"💡 AIがPDFから自動読み取り: ¥{int(ocr_total):,}")
    else:
        st.caption("💡 AIが「新規ご利用金額」を読み取れなかった場合は手動で入力してください")

    diff = total_invoice - total_calculated

    col1, col2, col3 = st.columns(3)
    col1.metric("請求金額（AMEX全体）", f"¥{total_invoice:,}")
    col2.metric("按分合計（表の合計）", f"¥{total_calculated:,}")
    col3.metric(
        "差額",
        f"¥{diff:,}",
        delta=diff,
        delta_color="inverse",
    )

    if diff == 0:
        st.success("✅ 差額が ¥0 になりました！確定できます。")
    else:
        st.warning(
            f"⚠️ 差額が ¥{abs(diff):,} あります。"
            "表の金額を修正するか、総請求金額を確認してください。"
        )

    # ------------------------------------------------------------------
    # 確定ボタン（差額¥0のときのみ有効）
    # ------------------------------------------------------------------
    confirm_btn = st.button(
        "✔️ 明細を確定する",
        disabled=(diff != 0),
        type="primary",
    )
    if confirm_btn:
        st.session_state.confirmed = True
        st.rerun()

    # ------------------------------------------------------------------
    # Step 4：帳票（領収書）アップロード
    # ------------------------------------------------------------------
    st.markdown("---")
    st.subheader("📂 Step 4 ｜ 帳票（領収書）のアップロード")

    if not st.session_state.confirmed:
        st.info("Step 3 で差額を ¥0 にして「確定する」ボタンを押すと、帳票アップロードが開放されます。")
    else:
        st.success("✅ 明細が確定済みです。各行に帳票（領収書）をアップロードしてください。")

        upload_count = 0
        for idx, row in edited_df.iterrows():
            amount_str = f"¥{int(row['按分金額(税込)']):,}"
            label = f"【明細 {idx + 1}】 {row['備考']}  {amount_str}"

            with st.expander(label, expanded=(idx == 0)):
                meta_col, upload_col = st.columns([1, 1])

                with meta_col:
                    st.write(f"**ブランド:** {row['ブランド'] or '—'}")
                    st.write(f"**ジョブコード:** {row['ジョブコード'] or '—'}")
                    st.write(f"**コストセンター:** {row['コストセンター'] or '—'}")
                    st.write(f"**按分仕訳勘定:** {row['按分仕訳勘定(税込)'] or '—'}")

                with upload_col:
                    receipt = st.file_uploader(
                        "領収書をアップロード",
                        type=["pdf", "png", "jpg", "jpeg", "csv", "xlsx", "xls"],
                        key=f"receipt_{idx}",
                        label_visibility="collapsed",
                    )
                    if receipt is not None:
                        st.session_state.receipts[idx] = receipt.name
                        st.success(f"📎 {receipt.name}")
                        upload_count += 1
                    elif idx in st.session_state.receipts:
                        st.info(f"📎 {st.session_state.receipts[idx]}（アップロード済み）")
                        upload_count += 1

        st.markdown("---")
        total_rows = len(edited_df)
        st.write(f"**アップロード済み:** {upload_count} / {total_rows} 件")

        # Bill One CSV ダウンロード
        st.markdown("---")
        st.subheader("📥 Step 5 ｜ Bill One形式 CSVダウンロード")

        try:
            # tax_reviewの内容をDataFrameに反映してからCSV化
            export_df = edited_df.copy()
            for idx, rev in st.session_state.tax_review.items():
                if idx in export_df.index:
                    export_df.at[idx, "備考"] = rev.get("formal_name", export_df.at[idx, "備考"])
                    tax_app = TAX_RATE_BILLONE_TO_APP.get(rev.get("tax_rate_display", "消費税 10％"), "10%")
                    export_df.at[idx, "税率"] = tax_app
            csv_bytes = to_bill_one_csv_bytes(export_df)
            st.download_button(
                label="⬇️ Bill One CSV をダウンロード",
                data=csv_bytes,
                file_name=f"billone_{month_label.replace('年','').replace('月','')}.csv",
                mime="text/csv",
                type="primary",
                use_container_width=True,
            )
            st.warning(
                "⚠️ 作成したCSVファイルをExcelで開くとID番号の先頭の0が外れ、"
                "取り込み時にエラーが発生します。\n"
                "CSVファイルはExcelで開かず、そのままBill Oneにアップロードしてください。"
            )
            st.caption(
                "※ tax_rateのIDは経理確認後に更新予定  ·  "
                "文字コード: UTF-8  ·  "
                f"出力行数: {len(edited_df)}行"
            )
        except Exception as e:
            st.error(f"CSV生成エラー: {e}")

# =====================================================================
# フッター
# =====================================================================
st.markdown("---")
st.caption(
    "AMEX FAX 明細データ化システム v0.2  ·  "
    "Powered by Anthropic Claude Vision API  ·  "
    "Phase②: SQLite DB / Bill One CSV エクスポート 実装予定"
)
