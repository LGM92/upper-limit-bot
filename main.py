import os
import time
from datetime import datetime
import pandas as pd
import OpenDartReader
import feedparser
from openai import OpenAI
import requests
from bs4 import BeautifulSoup

# 환경변수에서 키 불러오기
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
OPENAI_API_KEY = os.environ['OPENAI_API_KEY']
DART_API_KEY = os.environ['DART_API_KEY']

client = OpenAI(api_key=OPENAI_API_KEY)
dart = OpenDartReader(DART_API_KEY)

# 테스트용 날짜 고정 (실제 운영시 None으로 변경)
TEST_DATE = "20260605"  # None 으로 바꾸면 오늘 날짜 자동 적용


def get_today():
    if TEST_DATE:
        return TEST_DATE
    return datetime.now().strftime("%Y%m%d")


def get_upper_limit_stocks():
    """네이버 금융에서 상한가 종목 수집"""
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
                volume_text = cols[4].get_text(strip=True)

                if not name or name in seen_names:
                    continue

                # 종목코드 추출
                ticker = ''
                link = cols[3].select_one('a')
                if link and 'href' in link.attrs:
                    href = link['href']
                    ticker = href.split('code=')[-1] if 'code=' in href else ''

                # 등락률 파싱
                rate_clean = rate_text.replace('%', '').replace('+', '').strip()
                try:
                    rate = float(rate_clean)
                except:
                    continue

                # 거래량 파싱
                volume_clean = volume_text.replace(',', '').strip()
                try:
                    volume = int(volume_clean)
                except:
                    volume = 0

                seen_names.add(name)
                results.append({
                    'Code': ticker,
                    'Name': name,
                    'FLUC_RT': rate,
                    'ACC_TRDVOL': volume
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


def get_financial_data(ticker, stock_name):
    """재무 데이터 수집 (네이버 + DART)"""
    result = {
        '시가총액': '-', 'PER': '-', '매출성장률': '-',
        '영업이익률': '-', '영업이익성장률': '-',
        '부채비율': '-', '현금보유량': '-', 'ROE': '-', 'PEG': '-',
    }

    try:
        # 네이버 금융에서 시가총액, PER
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
            if per_text and per_text != 'N/A':
                result['PER'] = per_text

        print(f"[NAVER] {stock_name} 시총: {result['시가총액']} PER: {result['PER']}")
        time.sleep(0.5)

    except Exception as e:
        print(f"시가총액/PER 오류 ({stock_name}): {e}")

    try:
        # DART 재무데이터 - 종목코드로 조회
        current_year = str(datetime.now().year - 1)
        prev_year = str(datetime.now().year - 2)

        fs_current = dart.finstate(ticker, current_year)
        print(f"[DART] {stock_name} {current_year}년 재무제표:")
        if fs_current is not None and not fs_current.empty:
            print(fs_current[['account_nm', 'thstrm_amount']].head(10))
        time.sleep(0.5)

        fs_prev = dart.finstate(ticker, prev_year)

        def get_account(fs, label):
            try:
                if fs is None or fs.empty:
                    return None
                row = fs[fs['account_nm'].str.contains(label, na=False)]
                if row.empty:
                    return None
                val = row.iloc[0]['thstrm_amount']
                return float(str(val).replace(',', ''))
            except:
                return None

        revenue_cur = get_account(fs_current, '매출액')
        op_income_cur = get_account(fs_current, '영업이익')
        total_debt = get_account(fs_current, '부채총계')
        total_equity = get_account(fs_current, '자본총계')
        cash = get_account(fs_current, '현금및현금성자산')
        net_income = get_account(fs_current, '당기순이익')
        revenue_prev = get_account(fs_prev, '매출액')
        op_income_prev = get_account(fs_prev, '영업이익')

        if revenue_cur and op_income_cur:
            result['영업이익률'] = f"{(op_income_cur / revenue_cur * 100):.1f}%"

        if revenue_cur and revenue_prev and revenue_prev != 0:
            result['매출성장률'] = f"{((revenue_cur - revenue_prev) / abs(revenue_prev) * 100):.1f}%"

        if op_income_cur and op_income_prev and op_income_prev != 0:
            op_growth = (op_income_cur - op_income_prev) / abs(op_income_prev) * 100
            result['영업이익성장률'] = f"{op_growth:.1f}%"

        if total_debt and total_equity and total_equity != 0:
            result['부채비율'] = f"{(total_debt / total_equity * 100):.1f}%"

        if cash:
            result['현금보유량'] = f"{cash / 1e8:.0f}억원"

        if net_income and total_equity and total_equity != 0:
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
    """구글 뉴스 RSS로 수집"""
    try:
        query = f'"{stock_name}" 주가'.replace(" ", "+")
        url = (
            f"https://news.google.com/rss/search?"
            f"q={query}"
            f"&hl=ko&gl=KR&ceid=KR:ko"
        )
        feed = feedparser.parse(url)
        news_list = [entry.title for entry in feed.entries[:5]]

        if not news_list:
            return "뉴스 없음"

        return "\n".join(news_list)

    except Exception as e:
        print(f"뉴스 오류: {e}")
        return "뉴스 없음"


def get_dart_disclosure(ticker, stock_name):
    """DART 당일 공시 수집"""
    try:
        today = get_today()
        today_fmt = f"{today[:4]}-{today[4:6]}-{today[6:]}"
        disclosures = dart.list(ticker, bgnde=today_fmt, endde=today_fmt)
        if disclosures is None or disclosures.empty:
            return "당일 공시 없음"
        titles = disclosures['report_nm'].tolist()[:3]
        return "\n".join(titles)
    except Exception as e:
        print(f"공시 오류 ({stock_name}): {e}")
        return "공시 확인 불가"


def get_ai_summary(stock_name, news_text, disclosure_text, financial):
    """GPT 요약 - 근거 기반"""
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

[재무지표]
- 시가총액: {financial['시가총액']}
- PER: {financial['PER']} / PEG: {financial['PEG']} / ROE: {financial['ROE']}
- 매출성장률: {financial['매출성장률']} / 영업이익률: {financial['영업이익률']} / 영업이익성장률: {financial['영업이익성장률']}
- 부채비율: {financial['부채비율']} / 현금보유량: {financial['현금보유량']}

규칙:
1. 공시와 뉴스에 근거해서만 분석
2. 추측 금지
3. 공시/뉴스가 없으면 "상한가 원인 확인 불가" 라고 작성

아래 형식으로 답변:

[상한가 원인]
...

[원인 분류]
AI / 반도체 / 바이오 / 정책수혜 / 실적개선 / M&A / 수급 / 기타 중 하나

[재무 평가]
...
"""
            }]
        )
        return response.choices[0].message.content.strip()
    except:
        return "AI 요약 실패"


def send_telegram(message):
    """텔레그램 전송"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
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

        print(f"\n{name} 처리 중...")

        financial = get_financial_data(ticker, name)
        time.sleep(1)

        news = get_news(name)
        print(f"\n===== {name} 뉴스 =====")
        print(news)
        print("=====================\n")
        time.sleep(1)

        disclosure = get_dart_disclosure(ticker, name)
        print(f"[공시] {name}: {disclosure}")
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
