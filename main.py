import os
import time
from datetime import datetime, timedelta
import pandas as pd
import OpenDartReader
import feedparser
from openai import OpenAI
import requests
from bs4 import BeautifulSoup

# 환경변수
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
OPENAI_API_KEY = os.environ['OPENAI_API_KEY']
DART_API_KEY = os.environ['DART_API_KEY']

client = OpenAI(api_key=OPENAI_API_KEY)
dart = OpenDartReader(DART_API_KEY)

TEST_DATE = "20260605"  # None으로 바꾸면 오늘 날짜 자동 적용

# 의미없는 뉴스 필터
BAD_KEYWORDS = ["투자분석", "주달", "톺아보기", "민낯", "수급포착", "주가분석"]

# 중요 공시 키워드
IMPORTANT_DISCLOSURES = [
    "투자판단", "공급계약", "수주", "신규시설", "유상증자", "무상증자",
    "전환사채", "교환사채", "영업양수", "영업양도", "주요사항보고서",
    "자기주식취득", "합병", "분할", "임상", "특허", "계약"
]


def get_today():
    if TEST_DATE:
        return TEST_DATE
    return datetime.now().strftime("%Y%m%d")


def get_upper_limit_stocks():
    today = get_today()
    print(f"[{today}] 상한가 종목 수집 시작...")

    try:
        results = []
        seen_names = set()
        headers = {"User-Agent": "Mozilla/5.0"}

        for page in range(1, 20):
            url = f"https://finance.naver.com/sise/sise_upper.naver?page={page}"
            res = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, 'html.parser')
            rows = soup.select('table.type_5 tr')
            if not rows:
                break

            found_this_page = False
            for row in rows:
                cols = row.select("td")
                if len(cols) < 7:
                    continue

                name = cols[3].get_text(strip=True)
                rate_text = cols[6].get_text(strip=True)
                volume_text = cols[8].get_text(strip=True)

                if not name or name in seen_names:
                    continue

                ticker = ''
                link = cols[3].select_one('a')
                if link and 'href' in link.attrs:
                    href = link['href']
                    ticker = href.split('code=')[-1] if 'code=' in href else ''

                rate_clean = rate_text.replace('%', '').replace('+', '').strip()
                try:
                    rate = float(rate_clean)
                except:
                    continue

                volume_clean = volume_text.replace(',', '').strip()
                try:
                    volume = int(volume_clean)
                except:
                    volume = 0

                seen_names.add(name)
                results.append({
                    'Code': ticker, 'Name': name,
                    'FLUC_RT': rate, 'ACC_TRDVOL': volume
                })
                found_this_page = True

            if not found_this_page:
                break
            time.sleep(0.3)

        df = pd.DataFrame(results)
        print(f"상한가 종목 {len(df)}개 발견")
        return df

    except Exception as e:
        print(f"상한가 수집 오류: {e}")
        return pd.DataFrame()


def get_account(fs, label):
    """DART 재무제표에서 특정 항목 숫자 추출 (Python 계산용)"""
    try:
        if fs is None or len(fs) == 0:
            return None
        row = fs[fs['account_nm'].str.contains(label, na=False)]
        if row.empty:
            return None
        val = row.iloc[0]['thstrm_amount']
        if pd.isna(val):
            return None
        val = str(val).replace(',', '').strip()
        if val in ['', '-', 'nan']:
            return None
        return float(val)
    except:
        return None


def get_financial_data(ticker, stock_name):
    """재무 데이터 수집 - Python에서 직접 계산, AI 추정 없음"""
    result = {
        '시가총액': '-', 'PER': '-', '매출성장률': '-',
        '영업이익률': '-', '영업이익성장률': '-',
        '부채비율': '-', '현금보유량': '-', 'ROE': '-', 'PEG': '-',
    }

    # 1. 네이버 금융: 시가총액, PER
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')

        cap_tag = soup.select_one('em#_market_sum')
        if cap_tag:
            result['시가총액'] = cap_tag.get_text(strip=True) + '억원'

        per_tag = soup.select_one('em#_per')
        if per_tag:
            per_text = per_tag.get_text(strip=True)
            if per_text and per_text not in ['N/A', '-', '']:
                result['PER'] = per_text

        print(f"[NAVER] {stock_name} 시총: {result['시가총액']} PER: {result['PER']}")
        time.sleep(0.5)

    except Exception as e:
        print(f"네이버 오류 ({stock_name}): {e}")

    # 2. DART: 재무제표에서 Python 직접 계산
    try:
        current_year = str(datetime.now().year - 1)
        prev_year = str(datetime.now().year - 2)

        fs_current = dart.finstate(ticker, current_year)
        time.sleep(0.5)
        fs_prev = dart.finstate(ticker, prev_year)

        # DART가 오류 딕셔너리를 반환하는 경우 처리
        if isinstance(fs_current, dict):
            print(f"DART 오류응답 ({stock_name}) {current_year}: {fs_current}")
            fs_current = None
        if isinstance(fs_prev, dict):
            print(f"DART 오류응답 ({stock_name}) {prev_year}: {fs_prev}")
            fs_prev = None

        # 현재연도 수치
        revenue_cur    = get_account(fs_current, '매출액')
        op_income_cur  = get_account(fs_current, '영업이익')
        total_debt     = get_account(fs_current, '부채총계')
        total_equity   = get_account(fs_current, '자본총계')
        net_income     = get_account(fs_current, '당기순이익')
        cash = (
            get_account(fs_current, '현금및현금성자산')
            or get_account(fs_current, '현금성자산')
            or get_account(fs_current, '현금')
        )

        # 전년도 수치
        revenue_prev   = get_account(fs_prev, '매출액')
        op_income_prev = get_account(fs_prev, '영업이익')

        # 디버그: 실제 타입과 값 출력
        print(f"[DART계산] {stock_name}")
        print(f"  매출(현재): {revenue_cur}, 매출(전년): {revenue_prev}")
        print(f"  영업이익(현재): {op_income_cur}, 영업이익(전년): {op_income_prev}")
        print(f"  당기순이익: {net_income}, 자본총계: {total_equity}")
        print(f"  부채총계: {total_debt}, 현금: {cash}")

        # Python에서 직접 계산 (값 없으면 '-' 유지)
        if isinstance(revenue_cur, float) and isinstance(op_income_cur, float) and revenue_cur != 0:
            result['영업이익률'] = f"{(op_income_cur / revenue_cur * 100):.1f}%"

        if isinstance(revenue_cur, float) and isinstance(revenue_prev, float) and revenue_prev != 0:
            result['매출성장률'] = f"{((revenue_cur - revenue_prev) / abs(revenue_prev) * 100):.1f}%"

        if isinstance(op_income_cur, float) and isinstance(op_income_prev, float) and op_income_prev != 0:
            op_growth = (op_income_cur - op_income_prev) / abs(op_income_prev) * 100
            result['영업이익성장률'] = f"{op_growth:.1f}%"

        if isinstance(total_debt, float) and isinstance(total_equity, float) and total_equity != 0:
            result['부채비율'] = f"{(total_debt / total_equity * 100):.1f}%"

        if isinstance(cash, float):
            result['현금보유량'] = f"{cash / 1e8:.0f}억원"

        if isinstance(net_income, float) and isinstance(total_equity, float) and total_equity != 0:
            result['ROE'] = f"{(net_income / total_equity * 100):.1f}%"

        if result['PER'] != '-' and result['영업이익성장률'] != '-':
            try:
                per_val = float(result['PER'].replace(',', ''))
                op_growth_val = float(result['영업이익성장률'].replace('%', ''))
                if op_growth_val > 0:
                    result['PEG'] = f"{per_val / op_growth_val:.2f}"
            except:
                pass

    except Exception as e:
        print(f"DART 오류 ({stock_name}): {e}")

    return result


def get_news(stock_name):
    """구글 뉴스 RSS + 블로그성 기사 필터"""
    try:
        query = f'"{stock_name}" 주가'.replace(" ", "+")
        url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)

        news_list = []
        for entry in feed.entries[:10]:
            title = entry.title
            if not any(k in title for k in BAD_KEYWORDS):
                news_list.append(title)
            if len(news_list) >= 5:
                break

        return "\n".join(news_list) if news_list else "뉴스 없음"

    except Exception as e:
        print(f"뉴스 오류: {e}")
        return "뉴스 없음"


def get_dart_disclosure(ticker, stock_name):
    """DART 최근 7일 공시 - 중요 공시만 필터"""
    try:
        today = get_today()
        today_dt = datetime.strptime(today, "%Y%m%d")
        week_ago = (today_dt - timedelta(days=7)).strftime("%Y%m%d")

        today_fmt = f"{today[:4]}-{today[4:6]}-{today[6:]}"
        week_fmt = f"{week_ago[:4]}-{week_ago[4:6]}-{week_ago[6:]}"

        disclosures = dart.list(ticker, start=week_fmt, end=today_fmt)

        if disclosures is None or disclosures.empty:
            return "최근 7일 공시 없음"

        all_titles = disclosures['report_nm'].tolist()

        # 중요 공시 우선 필터
        important = [t for t in all_titles if any(k in t for k in IMPORTANT_DISCLOSURES)]
        result_titles = important[:3] if important else all_titles[:3]

        print(f"[공시] {stock_name}: {result_titles}")
        return "\n".join(result_titles)

    except Exception as e:
        print(f"공시 오류 ({stock_name}): {e}")
        return "공시 확인 불가"


def get_ai_summary(stock_name, news_text, disclosure_text, financial):
    """GPT 요약 - 근거 기반, 재무 추정 금지"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"""
종목명: {stock_name}

[당일 공시]
{disclosure_text}

[관련 뉴스]
{news_text}

[재무지표 (Python 직접 계산값, '-'는 데이터 없음)]
- 시가총액: {financial['시가총액']}
- PER: {financial['PER']} / PEG: {financial['PEG']} / ROE: {financial['ROE']}
- 매출성장률: {financial['매출성장률']} / 영업이익률: {financial['영업이익률']} / 영업이익성장률: {financial['영업이익성장률']}
- 부채비율: {financial['부채비율']} / 현금보유량: {financial['현금보유량']}

규칙:
1. 반드시 제공된 뉴스 제목과 공시에 근거해서만 작성
2. 명확한 근거가 없으면 "원인 불명" 작성
3. 추측 금지. 절대 단정하지 말 것
4. 불확실한 내용은 "추정", "가능성" 표현 사용
5. "시장 기대감", "투자심리", "수급", "매수세" 같은 일반론 표현 근거 없으면 사용 금지
6. 재무비율을 절대로 추정하지 말 것. 제공된 숫자만 사용. 값이 없으면 '-' 그대로 출력

아래 형식으로만 답변:

[상한가 원인]
(뉴스/공시 근거 또는 "원인 불명")

[원인 분류]
AI / 반도체 / 바이오 / 정책수혜 / 실적개선 / M&A / 수급 / 기타 중 하나

[재무 평가]
(제공된 수치만 사용. 없는 값은 언급하지 말 것)
"""
            }]
        )
        return response.choices[0].message.content.strip()
    except:
        return "AI 요약 실패"


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    requests.post(url, data=data)


def main():
    today = get_today()
    today_display = f"{today[:4]}년 {today[4:6]}월 {today[6:]}일"

    upper_df = get_upper_limit_stocks()

    if upper_df.empty:
        send_telegram(f"📊 {today_display}\n오늘 상한가 종목이 없습니다.")
        print("상한가 종목 없음. 종료.")
        return

    msg = f"📈 *{today_display} 상한가 종목*\n총 {len(upper_df)}개\n━━━━━━━━━━━━━━\n\n"

    for _, row in upper_df.iterrows():
        ticker = row.get('Code', '')
        name = row.get('Name', ticker)
        rate = row.get('FLUC_RT', 0)
        volume = row.get('ACC_TRDVOL', 0)

        print(f"\n{'='*30}")
        print(f"{name} 처리 중...")

        financial = get_financial_data(ticker, name)
        time.sleep(1)

        news = get_news(name)
        print(f"\n[뉴스] {name}:\n{news}\n")
        time.sleep(1)

        disclosure = get_dart_disclosure(ticker, name)
        time.sleep(1)

        summary = get_ai_summary(name, news, disclosure, financial)

        msg += f"*{name}* (+{rate:.1f}%)\n"
        msg += f"거래량: {int(volume):,}주\n"
        msg += f"시가총액: {financial['시가총액']} | PER: {financial['PER']} | PEG: {financial['PEG']}\n"
        msg += f"ROE: {financial['ROE']} | 부채비율: {financial['부채비율']} | 현금: {financial['현금보유량']}\n"
        msg += f"매출성장률: {financial['매출성장률']} | 영업이익률: {financial['영업이익률']} | 영업이익성장률: {financial['영업이익성장률']}\n"
        msg += f"💬 {summary}\n\n"

    for i in range(0, len(msg), 4000):
        send_telegram(msg[i:i+4000])
        time.sleep(1)

    print("전송 완료!")


if __name__ == "__main__":
    main()
