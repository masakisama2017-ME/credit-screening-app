import streamlit as st
import json
import google.generativeai as genai
from tavily import TavilyClient
import requests
from datetime import datetime, timedelta
import urllib.parse
import fitz  # PyMuPDF
import io

# ==========================================
# 0. APIキーの設定
# ==========================================
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
TAVILY_API_KEY = st.secrets["TAVILY_API_KEY"]
EDINET_API_KEY = st.secrets["EDINET_API_KEY"]
NTA_API_ID = st.secrets.get("NTA_API_ID", "")

genai.configure(api_key=GEMINI_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
MODEL_NAME = 'gemini-2.5-pro'

# ==========================================
# 【提案B】 多言語クエリの自動生成 (Pre-Search AI)
# ==========================================
def generate_localized_query(company_name, country, region):
    model = genai.GenerativeModel(MODEL_NAME)
    prompt = f"""
    対象国「{country}」の最も一般的なビジネス言語を特定し、
    企業名「{company_name}」に対する以下のネガティブ検索用クエリを作成してください。
    【必須キーワードの意味】倒産, 訴訟, 詐欺, 行政処分, 不祥事
    
    出力は絶対に、検索クエリの「文字列のみ」としてください。
    """
    response = model.generate_content(prompt)
    localized_query = response.text.strip()
    return f"{localized_query} 「公式サイト」 「D-U-N-S」 「企業コード」"

# ==========================================
# 【提案C】 グローバル制裁リスト照会 (OpenSanctions API)
# ==========================================
def check_global_sanctions(company_name):
    sanction_result = {"status": "クリーン（該当なし）", "details": []}
    encoded_name = urllib.parse.quote(company_name)
    url = f"https://api.opensanctions.org/search/default?q={encoded_name}"
    
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            for res in results:
                if res.get("score", 0) > 0.8:
                    sanction_result["status"] = "⚠️ 制裁リスト・ウォッチリスト該当の可能性あり"
                    sanction_result["details"].append({
                        "name": res.get("caption", ""),
                        "dataset": ", ".join(res.get("datasets", [])),
                        "url": res.get("id")
                    })
    except Exception as e:
        sanction_result["status"] = f"照会エラー: {str(e)}"
    return sanction_result

# ==========================================
# 1-A. 情報収集機能 (国税庁 法人番号API)
# ==========================================
def search_nta_api(company_name, region):
    nta_result = {"status": "未照会（APIキー未設定）", "corporate_number": "", "official_name": "", "address": ""}
    if not NTA_API_ID: return nta_result

    encoded_name = urllib.parse.quote(company_name)
    url = f"https://api.houjin-bangou.nta.go.jp/4/name?id={NTA_API_ID}&name={encoded_name}&type=12&mode=2&history=0"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if "corporation" in data:
                corps = data["corporation"]
                for corp in corps:
                    address = corp.get("prefectureName", "") + corp.get("cityName", "") + corp.get("streetNumber", "")
                    if region in address or not region:
                        nta_result.update({"status": "取得成功", "corporate_number": corp.get("corporateNumber", ""), "official_name": corp.get("name", ""), "address": address})
                        break
                if nta_result["status"] != "取得成功" and len(corps) > 0:
                    corp = corps[0]
                    nta_result.update({"status": "取得成功（地域不一致の可能性あり）", "corporate_number": corp.get("corporateNumber", ""), "official_name": corp.get("name", ""), "address": corp.get("prefectureName", "") + corp.get("cityName", "")})
        else:
            nta_result["status"] = f"取得失敗（エラーコード: {response.status_code}）"
    except Exception as e:
        nta_result["status"] = f"通信エラー: {str(e)}"
    return nta_result

# ==========================================
# 1-B. 情報収集機能 (Web検索: 強化版)
# ==========================================
def search_web_info(localized_query):
    # include_raw_content=True を追加し、検索結果の「本文」を丸ごと取得してAIに読ませる
    response = tavily_client.search(
        query=localized_query,
        search_depth="advanced", 
        max_results=10, 
        include_raw_content=True
    )
    return response
# ==========================================
# 1-C. 情報収集機能 (EDINET API連携 過去100日分)
# ==========================================
def search_edinet_api(company_name):
    url = "https://disclosure.edinet-fsa.go.jp/api/v2/documents.json"
    edinet_result = {"company_found_in_api": False, "documents": [], "message": "直近100日以内に提出された書類は見つかりませんでした。"}

    for i in range(100):
        target_date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        params = {"date": target_date, "type": 2, "Subscription-Key": EDINET_API_KEY}
        try:
            response = requests.get(url, params=params, timeout=5)
            if response.status_code == 200:
                data = response.json()
                for doc in data.get("results", []):
                    filer_name = doc.get("filerName", "")
                    if filer_name and company_name in filer_name:
                        doc_id = doc.get("docID")
                        edinet_result["company_found_in_api"] = True
                        edinet_result["documents"].append({
                            "title": doc.get("docDescription", "書類名不明"),
                            "filing_date": target_date,
                            "direct_pdf_url": f"https://disclosure.edinet-fsa.go.jp/api/v2/documents/{doc_id}?type=2&Subscription-Key={EDINET_API_KEY}"
                        })
        except Exception:
            continue
    return edinet_result

# ==========================================
# 【海外強化】 米国SEC EDGAR API連携 (10-K 年次報告書検索)
# ==========================================
def search_sec_edgar_api(company_name):
    """米国SECのEDGAR APIにアクセスし、指定された企業の直近の10-K（年次報告書）を特定する"""
    sec_result = {"company_found_in_sec": False, "document_title": "", "filing_date": "", "url": "", "message": "SECに提出された直近の10-Kは見つかりませんでした。"}
    
    # SEC APIを利用するための必須ヘッダー（会社名とメールアドレスの形式が必要）
    headers = {"User-Agent": "CreditScreeningApp admin@creditscreening.com"}
    
    try:
        # 1. 企業名からSECのCIKコード（企業番号）を特定するためのマスターリストをダウンロード
        tickers_url = "https://data.sec.gov/files/company_tickers.json"
        response = requests.get(tickers_url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            tickers_data = response.json()
            cik = None
            official_name = ""
            
            # 部分一致で企業を検索
            for key, val in tickers_data.items():
                if company_name.lower() in val["title"].lower():
                    cik = str(val["cik_str"])
                    official_name = val["title"]
                    break
            
            if cik:
                # 2. CIKコードを使って、その企業の直近の提出書類リストを取得
                cik_padded = cik.zfill(10)
                submissions_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
                sub_res = requests.get(submissions_url, headers=headers, timeout=5)
                
                if sub_res.status_code == 200:
                    sub_data = sub_res.json()
                    filings = sub_data.get("filings", {}).get("recent", {})
                    
                    # 提出書類の中から「10-K（年次報告書）」を検索
                    for idx, form in enumerate(filings.get("form", [])):
                        if form == "10-K":
                            acc_num = filings["accessionNumber"][idx]
                            acc_num_no_dash = acc_num.replace("-", "")
                            doc_name = filings["primaryDocument"][idx]
                            
                            sec_result["company_found_in_sec"] = True
                            sec_result["document_title"] = f"Form 10-K (Annual Report) - {official_name}"
                            sec_result["filing_date"] = filings["filingDate"][idx]
                            # SECの公式HTML閲覧URLを生成
                            sec_result["url"] = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_num_no_dash}/{doc_name}"
                            sec_result["message"] = f"直近の10-K（年次報告書）を発見しました。"
                            return sec_result
    except Exception as e:
        sec_result["message"] = f"SEC API照会エラー: {str(e)}"
        
    return sec_result
    
# ==========================================
# 1-E. 【海外強化】グローバル財務PDF探索機能 (海外企業汎用)
# ==========================================
def search_global_financial_pdfs(company_name, country):
    """Web検索を通じて海外企業の公式IR資料（Annual Report等のPDF）を直接探し出す"""
    current_year = datetime.now().year
    last_year = current_year - 1
    
    # filetype:pdf を明示的に指定し、最新の年号と統合報告書も探索対象に含める
    query = f"{company_name} {country} (Annual Report OR Financial Report) ({current_year} OR {last_year}) filetype:pdf"
    try:
        response = tavily_client.search(
            query=query,
            search_depth="advanced", 
            max_results=5, 
            include_raw_content=False 
        )
        # URLの末尾の文字列制限を排除し、検索エンジンがPDFと判断したURLをすべて信頼して取得
        pdf_urls = [res['url'] for res in response.get('results', [])]
        return pdf_urls
    except Exception as e:
        return []

# ==========================================
# 1-F. 【追加】日本の非上場企業向け 財務ディープサーチ機能
# ==========================================
def search_unlisted_jp_finance(company_name, region):
    """EDINETにない非上場企業の決算公告、業績推移、会社概要の生データをWebから深掘り抽出する"""
    # 非上場企業が財務情報を出しやすいキーワードを狙い撃ち
    query = f"{company_name} {region} (決算公告 OR 貸借対照表 OR 損益計算書 OR 業績推移 OR 売上高) 会社概要 -site:edinet-fsa.go.jp"
    try:
        response = tavily_client.search(
            query=query,
            search_depth="advanced", # 検索時間をかけても深く探す
            max_results=3,           # 上位3サイト（公式サイトや官報ブログなど）を厳選
            include_raw_content=True # URLだけでなくHTMLの本文テキストを丸ごとぶっこ抜く
        )
        
        extracted_text = ""
        for res in response.get('results', []):
            content = res.get('raw_content', res.get('content', ''))
            if content:
                # 本文が長すぎる場合は15000文字でカットして結合
                extracted_text += f"\n\n--- 【非上場Webデータ抽出: {res['url']}】 ---\n{content[:15000]}"
        return extracted_text
    except Exception as e:
        return ""

# ==========================================
# 【提案D】 有価証券報告書等の自動読み込みとテキスト抽出 (欧州ファイアウォール突破型)
# ==========================================
def extract_text_from_edinet_pdf(pdf_url):
    try:
        # 欧州超巨大企業の強固なBot検知を回避するための完全なブラウザ偽装ヘッダー
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/pdf,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive"
        }
        
        response = requests.get(pdf_url, headers=headers, stream=True, timeout=15)
        
        if response.status_code == 200:
            # URLに.pdfがなくても、ダウンロードした実体のContent-TypeをチェックしてHTMLなら弾く
            content_type = response.headers.get('Content-Type', '').lower()
            if 'pdf' not in content_type and not pdf_url.lower().endswith('.pdf'):
                return ""
                
            pdf_file = response.content
            doc = fitz.open(stream=pdf_file, filetype="pdf")
            
            relevant_text = ""
            fallback_text = ""
            risk_keywords = [
                "事業等のリスク", "訴訟", "継続企業", "疑義", "ゴーイングコンサーン", "不確実性", 
                "債務超過", "営業損失", "赤字", "重大な損失", "行政処分", "法令違反", "不正",
                "net loss", "going concern", "insolvency", "restructuring", "litigation", "risk factors"
            ]
            
            num_pages = min(150, doc.page_count)
            extracted_pages = set()
            
            for i in range(num_pages):
                page_text = doc.load_page(i).get_text()
                if i < 10:
                    fallback_text += f"\n--- {i+1}ページ目 ---\n{page_text}"
                clean_text = page_text.lower().replace(" ", "").replace(" ", "").replace("\n", "")
                if any(keyword.replace(" ", "") in clean_text for keyword in risk_keywords):
                    extracted_pages.update([i, i+1, i+2])
                    
            for i in sorted(list(extracted_pages)):
                if i < doc.page_count:
                    relevant_text += f"\n--- {i+1}ページ目 ---\n{doc.load_page(i).get_text()}"
                    
            doc.close()
            final_text = relevant_text if relevant_text.strip() else fallback_text
            return final_text[:200000]
            
    except Exception as e:
        return f"PDF読み込みエラー: {str(e)}"
    return ""

# ==========================================
# 2. AIモデル (Geminiによる全統合・【文脈解釈型】厳格判定)
# ==========================================
def analyze_with_gemini(company_name, country, region, search_results, edinet_results, nta_results, sanction_results, pdf_text, sec_results):
    model = genai.GenerativeModel(MODEL_NAME)
    
    negative_dictionary = """
    【日本語キーワード】
    「倒産・経営破綻」: 倒産, 破産, 民事再生, 会社更生, 特別清算, 経営破綻, 経営難, 事業停止, 廃業, 夜逃げ
    「財務・支払不安」: 滞納, 未払い, 支払遅延, 債務不履行, 資金ショート, デフォルト, リスケ, 貸し倒れ, 焦げ付き, 取り立て, 債務超過, 巨額赤字
    「訴訟・法的トラブル」: 訴訟, 提訴, 裁判, 敗訴, 損害賠償, 係争中, 法的措置, 差し止め, 仮処分, 刑事告発, 法的紛争
    「犯罪・不祥事」: 不祥事, 詐欺, 横領, 背任, 脱税, 粉飾決算, 隠蔽, 偽装, 改ざん, 贈収賄, 不正会計
    「行政処分・違反」: 行政処分, 業務停止, 業務改善命令, 立ち入り検査, 指導, 免許取り消し, 排除措置命令, 課徴金, 申告漏れ, 追徴課税, 法令違反
    「労働・雇用問題」: ブラック企業, パワハラ, セクハラ, 不当解雇, 労働争議, ストライキ, 労基署, 未払い残業代, 過労死, 内部告発
    「品質・サービス問題」: リコール, 情報漏洩, サイバー攻撃, 欠陥, 事故, クレーム, 産地偽装, 异物混入, データ偽造, 炎上
    「反社・コンプライアンス」: 反社会的勢力, 暴力団, フロント企業, マネーロンダリング, 資金洗浄, 談合, カルテル, インサイダー取引, コンプライアンス違反, 違法
    「経営陣・組織の混乱」: 辞任, 解任, 内紛, 派閥争い, 経営陣刷新, 監査法人交代, 意見不表明, 上場廃止, 監理銘柄, 整理銘柄
    「警察・社会問題」: 疑惑, スキャンダル, 逮捕, 家宅捜索, 送検, 事情聴取, 謝罪, 批判, トラブル, 注意喚起
    
    【英語キーワード】
    「Bankruptcy & Insolvency」: bankruptcy, insolvency, liquidation, Chapter 11, receivership, restructuring, winding up, insolvent, dissolved, defunct
    「Financial & Payment Issues」: default, arrears, unpaid, late payment, non-payment, debt crisis, illiquidity, cash flow issue, bad debt, write-off
    「Litigation & Legal Actions」: lawsuit, litigation, sued, plaintiff, defendant, court case, damages, injunction, legal action, settlement
    「Crime & Fraud」: scandal, fraud, embezzlement, bribery, corruption, tax evasion, accounting fraud, cover-up, falsification, forgery
    "Regulatory & Administrative": sanction, penalty, fine, suspension, revoked, warning letter, inspection, antitrust, regulatory action, debarred
    "Labor & Employment": strike, labor dispute, harassment, discrimination, wrongful termination, toxic workplace, walkout, wage theft, whistleblower, union busting
    "Product & Cyber Issues": recall, data breach, cyberattack, defect, fatality, malfunction, outage, privacy violation, safety violation, product failure
    "Compliance & Sanctions": money laundering, OFAC, cartel, price fixing, insider trading, banned, blocked, embargo, KYC violation, compliance failure
    "Management Turmoil": resignation, ousted, boardroom battle, auditor resignation, delisted, going concern, corporate governance, proxy fight, hostile takeover, shareholder revolt
    "Reputation & Controversy": arrest, investigation, raided, subpoena, indicted, guilty, boycott, controversy, backlash, outrage
    """

    prompt = f"""
    あなたは極めて優秀で厳格な与信審査マネージャーです。
    以下の【国税庁API】【Web検索（本文含む）】【EDINET API】【制裁リストAPI】および【有報PDF抽出テキスト】から、
    対象企業（所在: {country} {region}、企業名: {company_name}）の与信情報をJSONで出力してください。
    
    【厳守ルール】
    1. 推測は一切禁止。明確な情報とURLが存在する場合のみ「有り」とすること。
    2. 【制裁リストAPI結果】の内容を必ず JSON の sanction_info に反映させること。
    3. ネガティブ情報については、【ネガティブキーワード辞書】に記載された日本語・英語のワードが検索結果内の対象企業の文脈で1つでも使用されている場合、「該当有り」とすること。
    5. 【公式開示書類・非上場Webデータ・外部IR資料抽出テキスト】が存在する場合、そこから「事業等のリスク」「訴訟」「継続企業の前提に関する重要な疑義」「重大な赤字や債務超過」に関する記述を探すこと。また非上場企業の場合は「決算公告の数値（純利益や剰余金）」「売上高の推移」「資本金」などの断片的な財務データがあればそれも拾い上げること。これらを要約して edinet_risk_summary に詳細に記載すること。該当記述がなければ「特筆すべきリスク・財務記載なし」とすること。文字数の制限はないので、複数の書類の情報を見つけた場合は詳細かつ具体的に記載すること。

    【出力JSONスキーマ】
    {{
      "corporate_info": {{ "corporate_number": "番号", "official_name": "名称", "address": "所在地", "api_status": "ステータス" }},
      "sanction_info": {{ "status": "クリーン 等", "details": ["制裁詳細の配列"] }},
      "official_website": {{ "status": "有り/なし", "url": "URL" }},
      "securities_report": {{ "status": "有り/なし", "reports": [{{ "date": "提出日", "title": "書類名", "url": "URL" }}] }},
      "edinet_risk_summary": "有報から抽出したリスクの詳細な要約（AI生成）",
      "official_gazette": {{ "status": "有り/なし", "url": "URL" }},
      "tdb_code": {{ "status": "有り/なし", "url": "URL" }},
      "tsr_code": {{ "status": "有り/なし", "url": "URL" }},
      "duns_number": {{ "status": "有り/なし", "url": "URL" }},
      "negative_info": {{ "status": "該当有り/なし", "details": [{{ "category": "分類", "matched_keywords": ["抽出したワードまたは言い換え表現"], "urls": ["URL"] }}] }}
    }}

    【ネガティブキーワード辞書】
    {negative_dictionary}

    【制裁リストAPI結果】
    {json.dumps(sanction_results, ensure_ascii=False)}
    
    【米国SEC EDGAR結果】
    {json.dumps(sec_results, ensure_ascii=False)}
    
    【国税庁API結果】
    {json.dumps(nta_results, ensure_ascii=False)}

    【EDINET API結果】
    {json.dumps(edinet_results, ensure_ascii=False)}

    【公式開示書類・外部IR資料抽出テキスト（最大300ページ分）】
    {pdf_text}

    【Web検索結果（記事本文含む）】
    {json.dumps(search_results, ensure_ascii=False)}
    """

    response = model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)

# ==========================================
# 3. UIとオーケストレーター (画面表示)
# ==========================================
st.set_page_config(page_title="グローバル与信リスク・インテリジェンス", layout="centered")
st.title("🛡️ グローバル与信リスク・インテリジェンス")
st.caption("AI多言語検索 × 制裁リスト照合 × 有報自動解析 (RAG)")

with st.sidebar:
    st.header("調査対象企業の入力")
    input_country = st.text_input("国", value="日本", placeholder="例: 日本, 米国, 中国")
    input_region = st.text_input("地域（都道府県・州）", placeholder="例: 東京都, カリフォルニア")
    input_name = st.text_input("会社名", placeholder="例: 株式会社〇〇")
    submit_button = st.button("AIディープスクリーニングを実行", type="primary")

if submit_button and input_name:
    with st.spinner("STEP 1: 多言語検索クエリの生成とグローバル制裁リストを照会中..."):
        localized_query = generate_localized_query(input_name, input_country, input_region)
        sanction_data = check_global_sanctions(input_name)
        
    with st.spinner(f"STEP 2: 現地言語でのWeb検索と国税庁APIの照会中... \n(検索クエリ: {localized_query})"):
        nta_data = search_nta_api(input_name, input_region)
        search_data = search_web_info(localized_query)
        
    with st.spinner("STEP 3: 公式開示API（EDINET/SEC）および非上場Webからの財務データ探索を実行中..."):
        edinet_data = search_edinet_api(input_name)
        sec_data = search_sec_edgar_api(input_name)
        
        pdf_extracted_text = ""
        
        # 1. 日本の上場企業向け処理（EDINETからPDFを抽出）
        if edinet_data["company_found_in_api"] and len(edinet_data["documents"]) > 0:
            for idx, doc in enumerate(edinet_data["documents"]):
                if idx >= 5: break
                title = doc.get("title", "")
                target_pdf_url = doc.get("direct_pdf_url")
                if "確認書" in title or "内部統制" in title or "自己株" in title: continue
                
                extracted_text = extract_text_from_edinet_pdf(target_pdf_url)
                if extracted_text:
                    pdf_extracted_text += f"\n\n======================\n【EDINET書類: {title}】\n======================\n{extracted_text}"
        
        # 2. 【追加】日本の非上場企業向け処理（EDINETに見当たらない場合）
        elif input_country in ["日本", "Japan"] and not edinet_data["company_found_in_api"]:
            unlisted_text = search_unlisted_jp_finance(input_name, input_region)
            if unlisted_text:
                pdf_extracted_text += f"\n\n======================\n【非上場企業 財務・業績生データ (Web抽出)】\n======================\n{unlisted_text}"
        
        # 3. 【追加】海外企業向け処理（EDINETで見つからなかった場合、WebからPDFリンクを探して解析）
        if not edinet_data["company_found_in_api"]:
            global_pdf_urls = search_global_financial_pdfs(input_name, input_country)
            
            # 発見した海外企業のIR資料（PDF）を最大2件まで読み込む
            for pdf_url in global_pdf_urls[:2]:
                extracted_text = extract_text_from_edinet_pdf(pdf_url)
                # エラーではない有効なテキストが抽出できた場合のみ追加
                if extracted_text and "PDF読み込みエラー" not in extracted_text:
                    pdf_extracted_text += f"\n\n======================\n【外部IR資料 (Web抽出): {pdf_url}】\n======================\n{extracted_text}"

    with st.spinner("STEP 4: Gemini 2.5 による全データの統合分析・リスク判定中..."):
        try:
            report_data = analyze_with_gemini(input_name, input_country, input_region, search_data, edinet_data, nta_data, sanction_data, pdf_extracted_text, sec_data)
            st.success("ディープスクリーニングが完了しました。")
            st.markdown("---")
            
            col1, col2 = st.columns([1, 1])
            
            with col1:
                st.subheader("＜法人基本情報＞")
                corp_info = report_data.get("corporate_info", {})
                if corp_info.get("corporate_number"):
                    st.write(f"**法人番号:** `{corp_info.get('corporate_number')}`")
                    st.write(f"**正式名称:** {corp_info.get('official_name')}")
                    st.write(f"**所在地:** {corp_info.get('address')}")
                else:
                    st.write("※取得できませんでした。")

                st.subheader("＜企業コード＞")
                tdb = report_data.get("tdb_code", {})
                if tdb.get("status") == "有り":
                    st.markdown(f"**帝国DB:** 有り 🔗 [{tdb.get('url')}]({tdb.get('url')})")
                else:
                    st.write("**帝国DB:** なし")
                    
                tsr = report_data.get("tsr_code", {})
                if tsr.get("status") == "有り":
                    st.markdown(f"**TSR:** 有り 🔗 [{tsr.get('url')}]({tsr.get('url')})")
                else:
                    st.write("**TSR:** なし")
                    
                duns = report_data.get("duns_number", {})
                if duns.get("status") == "有り":
                    st.markdown(f"**D-U-N-S:** 有り 🔗 [{duns.get('url')}]({duns.get('url')})")
                else:
                    st.write("**D-U-N-S:** なし")

                st.subheader("＜公式サイト / 官報＞")
                ow = report_data.get("official_website", {})
                if ow.get("status") == "有り":
                    st.markdown(f"**公式サイト:** 有り 🔗 [{ow.get('url')}]({ow.get('url')})")
                else:
                    st.write("**公式サイト:** なし")
                    
                og = report_data.get("official_gazette", {})
                if og.get("status") == "有り":
                    st.markdown(f"**官報:** 有り 🔗 [{og.get('url')}]({og.get('url')})")
                else:
                    st.write("**官報:** なし")

            with col2:
                st.subheader("🌍 ＜グローバル制裁・ウォッチリスト＞")
                sanc_info = report_data.get("sanction_info", {})
                if "該当の可能性あり" in sanc_info.get("status", ""):
                    st.error(f"**{sanc_info.get('status')}**")
                    for s_detail in sanc_info.get("details", []):
                        st.markdown(f"- {s_detail.get('name')} (リスト: {s_detail.get('dataset')})")
                else:
                    st.info(sanc_info.get("status", "クリーン"))

                st.subheader("🚨 ＜ネガティブ情報＞")
                neg_info = report_data.get("negative_info", {})
                if neg_info.get("status") == "該当有り":
                    st.error("⚠️ **該当有り**")
                    for detail in neg_info.get("details", []):
                        with st.expander(f"分類: {detail.get('category', '不明')}", expanded=True):
                            st.markdown(f"**ワード:** {', '.join(detail.get('matched_keywords', []))}")
                            st.markdown("**確認元URL:**")
                            for url in detail.get("urls", []):
                                st.markdown(f"- 🔗 [{url}]({url})")
                else:
                    st.write("なし")

            st.markdown("---")
            
            st.subheader("📄 ＜有価証券報告書 ＆ AI自動解析＞")
            sr_info = report_data.get("securities_report", {})
            if sr_info.get("status") == "有り" and sr_info.get("reports"):
                for rep in sr_info.get("reports", []):
                    st.markdown(f"- 🔗 [{rep.get('date')} 提出 : {rep.get('title')}]({rep.get('url')})")
                st.info("💡 **AIによる有報リスク要約 (RAG)**")
                st.write(report_data.get("edinet_risk_summary", "解析できませんでした。"))
            else:
                st.write("過去100日以内の提出なし")

        except Exception as e:
            st.error(f"処理中にエラーが発生しました: {e}")
