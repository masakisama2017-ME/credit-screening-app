import streamlit as st
import json
import google.generativeai as genai
from tavily import TavilyClient
import requests
from datetime import datetime, timedelta

# ==========================================
# 0. APIキーの設定
# ==========================================
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
TAVILY_API_KEY = st.secrets["TAVILY_API_KEY"]
EDINET_API_KEY = st.secrets["EDINET_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

# ==========================================
# 1-A. 情報収集機能 (Web検索)
# ==========================================
def search_web_info(company_name, country, region):
    """指定された国・地域と企業名で、Web上からエビデンスを探す"""
    # 7項目を網羅的に拾えるよう検索クエリを調整
    query = f"国:{country} 地域:{region} 企業名:{company_name} 「公式サイト」 「官報」 「帝国企業コード」 「TSR企業コード」 「DUNSナンバー」 「倒産 OR 訴訟 OR 不祥事 OR 行政処分 OR bankruptcy OR fraud」"
    
    response = tavily_client.search(
        query=query,
        search_depth="advanced", 
        max_results=15
    )
    return response

# ==========================================
# 1-B. 情報収集機能 (EDINET API連携: 過去100日分検索)
# ==========================================
def search_edinet_api(company_name):
    """
    金融庁 EDINET API (v2) にアクセスし、直近の有価証券報告書・四半期報告書の有無を確認する。
    ※四半期報告書の提出サイクルをカバーするため、過去100日間をループして探します。
    """
    url = "https://disclosure.edinet-fsa.go.jp/api/v2/documents.json"
    
    edinet_result = {
        "company_found_in_api": False,
        "document_title": "",
        "filing_date": "",
        "direct_pdf_url": "",
        "message": "直近100日以内に提出された書類は見つかりませんでした。"
    }

    # 過去100日間を1日ずつ遡ってAPIに問い合わせる
    for i in range(100):
        target_date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        
        # ▼ ここを修正：APIキー（Subscription-Key）を通信パラメータに追加します
        params = {
            "date": target_date, 
            "type": 2,
            "Subscription-Key": EDINET_API_KEY
        }
        
        try:
            response = requests.get(url, params=params, timeout=5)
            if response.status_code == 200:
                data = response.json()
                
                # その日に提出された全書類の中から、対象企業を探す
                for doc in data.get("results", []):
                    filer_name = doc.get("filerName", "")
                    
                    # 会社名が一致するか確認
                    if filer_name and company_name in filer_name:
                        doc_id = doc.get("docID")
                        
                        # 見つかった場合、結果を格納して即座に終了する
                        edinet_result["company_found_in_api"] = True
                        edinet_result["document_title"] = doc.get("docDescription", "書類名不明")
                        edinet_result["filing_date"] = target_date
                        edinet_result["direct_pdf_url"] = f"https://disclosure.edinet-fsa.go.jp/api/v2/documents/{doc_id}?type=2&Subscription-Key={EDINET_API_KEY}"
                        edinet_result["message"] = f"{target_date} 提出の書類を発見しました。"
                        
                        return edinet_result 
                        
        except Exception as e:
            # 通信エラーが起きてもプログラムを止めず、次の日の検索へ進む
            continue 
            
    return edinet_result

# ==========================================
# 2. AIモデル (Gemini 2.5による厳格な有無判定)
# ==========================================
def analyze_with_gemini(company_name, country, region, search_results, edinet_results):
    """検索結果とEDINET API結果をGeminiに渡し、辞書に基づく厳密なJSON出力をさせる"""
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    # ユーザー指定のネガティブキーワード辞書
    negative_dictionary = """
    【日本語キーワード】
    「倒産・経営破綻」: 倒産, 破産, 民事再生, 会社更生, 特別清算, 経営破綻, 経営難, 事業停止, 廃業, 夜逃げ
    「財務・支払不安」: 滞納, 未払い, 支払遅延, 債務不履行, 資金ショート, デフォルト, リスケ, 貸し倒れ, 焦げ付き, 取り立て
    「訴訟・法的トラブル」: 訴訟, 提訴, 裁判, 敗訴, 損害賠償, 係争中, 法的措置, 差し止め, 仮処分, 刑事告発
    「犯罪・不祥事」: 不祥事, 詐欺, 横領, 背任, 脱税, 粉飾決算, 隠蔽, 偽装, 改ざん, 贈収賄
    「行政処分・違反」: 行政処分, 業務停止, 業務改善命令, 立ち入り検査, 指導, 免許取り消し, 排除措置命令, 課徴金, 申告漏れ, 追徴課税
    「労働・雇用問題」: ブラック企業, パワハラ, セクハラ, 不当解雇, 労働争議, ストライキ, 労基署, 未払い残業代, 過労死, 内部告発
    「品質・サービス問題」: リコール, 情報漏洩, サイバー攻撃, 欠陥, 事故, クレーム, 産地偽装, 異物混入, データ偽造, 炎上
    「反社・コンプライアンス」: 反社会的勢力, 暴力団, フロント企業, マネーロンダリング, 資金洗浄, 談合, カルテル, インサイダー取引, コンプライアンス違反, 違法
    「経営陣・組織の混乱」: 辞任, 解任, 内紛, 派閥争い, 経営陣刷新, 監査法人交代, 意見不表明, 上場廃止, 監理銘柄, 整理銘柄
    「警察・社会問題」: 疑惑, スキャンダル, 逮捕, 家宅捜索, 送検, 事情聴取, 謝罪, 批判, トラブル, 注意喚起

    【英語キーワード】
    「Bankruptcy & Insolvency」: bankruptcy, insolvency, liquidation, Chapter 11, receivership, restructuring, winding up, insolvent, dissolved, defunct
    「Financial & Payment Issues」: default, arrears, unpaid, late payment, non-payment, debt crisis, illiquidity, cash flow issue, bad debt, write-off
    「Litigation & Legal Actions」: lawsuit, litigation, sued, plaintiff, defendant, court case, damages, injunction, legal action, settlement
    「Crime & Fraud」: scandal, fraud, embezzlement, bribery, corruption, tax evasion, accounting fraud, cover-up, falsification, forgery
    「Regulatory & Administrative」: sanction, penalty, fine, suspension, revoked, warning letter, inspection, antitrust, regulatory action, debarred
    「Labor & Employment」: strike, labor dispute, harassment, discrimination, wrongful termination, toxic workplace, walkout, wage theft, whistleblower, union busting
    「Product & Cyber Issues」: recall, data breach, cyberattack, defect, fatality, malfunction, outage, privacy violation, safety violation, product failure
    「Compliance & Sanctions」: money laundering, OFAC, cartel, price fixing, insider trading, banned, blocked, embargo, KYC violation, compliance failure
    「Management Turmoil」: resignation, ousted, boardroom battle, auditor resignation, delisted, going concern, corporate governance, proxy fight, hostile takeover, shareholder revolt
    「Reputation & Controversy」: arrest, investigation, raided, subpoena, indicted, guilty, boycott, controversy, backlash, outrage
    """

    prompt = f"""
    あなたは極めて厳格な与信審査マネージャーです。
    以下の【Web検索結果】および【EDINET API結果】から、対象企業（所在: {country} {region}、企業名: {company_name}）に関する指定された7項目の有無を判定し、JSON形式で出力してください。
    
    【厳守する絶対ルール】
    1. 推測や想像での補完は一切禁止。検索結果内に明確な情報と「リンク(URL)」が存在する場合のみ「有り」とし、確認できない場合は必ず「なし」とすること。
    2. 同名他社の情報は絶対に除外すること。
    3. 「帝国企業コード」「TSR企業コード」「D-U-N-S® Number」については、企業概要ページやデータベースサイト等で対象企業のコードとして明記されているURLがあれば「有り」とすること。番号自体の推測は絶対に行わないこと。
    4. ネガティブ情報については、以下の【ネガティブキーワード辞書】に記載されたワードが検索結果内の対象企業の文脈で1つでも使用されている場合、「該当有り」とすること。
    5. 以下のJSONスキーマに完全に従って出力すること。純粋なJSON文字列のみを出力し、マークダウンを含めないこと。

    【ネガティブキーワード辞書】
    {negative_dictionary}

    【出力JSONスキーマ】
    {{
      "official_website": {{ "status": "有り" または "なし", "url": "有りの場合はURL、なしは空文字" }},
      "securities_report": {{ "status": "有り" または "なし", "url": "有りの場合はURL、なしは空文字" }},
      "official_gazette": {{ "status": "有り" または "なし", "url": "有りの場合はURL、なしは空文字" }},
      "tdb_code": {{ "status": "有り" または "なし", "url": "有りの場合はURL、なしは空文字" }},
      "tsr_code": {{ "status": "有り" または "なし", "url": "有りの場合はURL、なしは空文字" }},
      "duns_number": {{ "status": "有り" または "なし", "url": "有りの場合はURL、なしは空文字" }},
      "negative_info": {{
        "status": "該当有り" または "なし",
        "details": [
          {{
            "category": "ヒットした辞書の分類名",
            "matched_keywords": ["ワード1", "ワード2"],
            "urls": ["URL1", "URL2"]
          }}
        ]
      }}
    }}

    【EDINET API結果】
    {json.dumps(edinet_results, ensure_ascii=False, indent=2)}

    【Web検索結果】
    {json.dumps(search_results, ensure_ascii=False, indent=2)}
    """

    response = model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"}
    )
    
    return json.loads(response.text)

# ==========================================
# 3. UIとオーケストレーター (画面表示)
# ==========================================
st.set_page_config(page_title="ファクトベース与信スクリーニング", layout="centered")
st.title("🛡️ 与信スクリーニング (事実確認ビュー)")

with st.sidebar:
    st.header("調査対象企業の入力")
    input_country = st.text_input("国", value="日本", placeholder="例: 日本")
    input_region = st.text_input("地域（都道府県）", placeholder="例: 東京都")
    input_name = st.text_input("会社名", placeholder="例: 株式会社〇〇")
    
    submit_button = st.button("検索を実行", type="primary")

if submit_button:
    if not input_name:
        st.error("会社名を入力してください。")
    else:
        with st.spinner(f"「{input_name}」に関する事実情報を検索・精査しています..."):
            try:
                search_data = search_web_info(input_name, input_country, input_region)
                edinet_data = search_edinet_api(input_name)
                report_data = analyze_with_gemini(input_name, input_country, input_region, search_data, edinet_data)
                
                st.success("スクリーニングが完了しました。")
                
                st.markdown("---")
                
                # 1. 公式サイト
                st.subheader("＜公式サイト＞")
                if report_data.get("official_website", {}).get("status") == "有り":
                    st.markdown(f"**有り** 🔗 [{report_data['official_website']['url']}]({report_data['official_website']['url']})")
                else:
                    st.write("なし")
                
                # 2. 有価証券報告書
                st.subheader("＜有価証券報告書＞")
                if report_data.get("securities_report", {}).get("status") == "有り":
                    st.markdown(f"**有り** 🔗 [{report_data['securities_report']['url']}]({report_data['securities_report']['url']})")
                else:
                    st.write("なし")

                # 3. 官報検索結果
                st.subheader("＜官報検索結果＞")
                if report_data.get("official_gazette", {}).get("status") == "有り":
                    st.markdown(f"**有り** 🔗 [{report_data['official_gazette']['url']}]({report_data['official_gazette']['url']})")
                else:
                    st.write("なし")

                # 4. 帝国企業コード
                st.subheader("＜帝国企業コード＞")
                if report_data.get("tdb_code", {}).get("status") == "有り":
                    st.markdown(f"**有り** 🔗 [{report_data['tdb_code']['url']}]({report_data['tdb_code']['url']})")
                else:
                    st.write("なし")

                # 5. TSR企業コード
                st.subheader("＜TSR企業コード＞")
                if report_data.get("tsr_code", {}).get("status") == "有り":
                    st.markdown(f"**有り** 🔗 [{report_data['tsr_code']['url']}]({report_data['tsr_code']['url']})")
                else:
                    st.write("なし")

                # 6. D-U-N-S Number
                st.subheader("＜D-U-N-S® Number＞")
                if report_data.get("duns_number", {}).get("status") == "有り":
                    st.markdown(f"**有り** 🔗 [{report_data['duns_number']['url']}]({report_data['duns_number']['url']})")
                else:
                    st.write("なし")

                # 7. ネガティブ情報
                st.subheader("＜ネガティブ情報＞")
                neg_info = report_data.get("negative_info", {})
                if neg_info.get("status") == "該当有り":
                    st.error("⚠️ **該当有り**")
                    details = neg_info.get("details", [])
                    if not details:
                        st.write("※詳細は取得できませんでした。")
                    
                    for detail in details:
                        with st.expander(f"分類: {detail.get('category', '不明')}", expanded=True):
                            keywords = ", ".join(detail.get('matched_keywords', []))
                            st.markdown(f"**該当ワード:** {keywords}")
                            st.markdown("**確認元URL:**")
                            for url in detail.get("urls", []):
                                st.markdown(f"- 🔗 [{url}]({url})")
                else:
                    st.write("なし")
                    
                st.markdown("---")
                            
            except Exception as e:
                st.error(f"処理中にエラーが発生しました: {e}")
