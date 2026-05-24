import os
import streamlit as st
import pandas as pd

# ai_extractorは同ディレクトリに配置
from ai_extractor import extract_and_aggregate, COLUMNS

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
            df_result = extract_and_aggregate(
                file_bytes=uploaded_file.read(),
                filename=uploaded_file.name,
                month_label=month_label,
                api_key=api_key,
            )
            st.session_state.df = df_result
            st.session_state.ocr_done = True
            st.session_state.confirmed = False
            st.session_state.receipts = {}
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
    edited_df = st.data_editor(
        st.session_state.df,
        num_rows="dynamic",
        column_config={
            "按分金額(税込)": st.column_config.NumberColumn(
                "按分金額(税込)", format="¥%d", min_value=0
            ),
            "税額": st.column_config.NumberColumn(
                "税額", format="¥%d", min_value=0
            ),
            "税率": st.column_config.SelectboxColumn(
                "税率", options=["10%", "8%", "0%不課税"]
            ),
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
    # 金額集計セクション
    # ------------------------------------------------------------------
    st.markdown("---")
    st.subheader("📊 Step 3 ｜ 金額集計の検証（差額 ¥0 で確定可能）")

    total_calculated = int(edited_df["按分金額(税込)"].fillna(0).sum())

    col_input, col_spacer = st.columns([1, 2])
    with col_input:
        total_invoice = st.number_input(
            "AMEX 全体の総請求金額（税込）",
            min_value=0,
            value=total_calculated,
            step=1,
            help="AMEX明細の合計金額を入力してください",
        )

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
                        type=["pdf", "png", "jpg", "jpeg"],
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

        # 保存ボタン
        save_btn = st.button(
            "💾 確定データをデータベースに保存",
            type="primary",
            use_container_width=True,
        )
        if save_btn:
            # TODO: Phase②でSQLite保存を実装
            st.success(
                "✅ データを保存しました。"
                "（Phase② で SQLite への永続化と Bill One CSV エクスポートを実装予定）"
            )
            st.balloons()

# =====================================================================
# フッター
# =====================================================================
st.markdown("---")
st.caption(
    "AMEX FAX 明細データ化システム v0.2  ·  "
    "Powered by Anthropic Claude Vision API  ·  "
    "Phase②: SQLite DB / Bill One CSV エクスポート 実装予定"
)
