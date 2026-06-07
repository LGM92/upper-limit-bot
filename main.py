import os
import time
from datetime import datetime
import pandas as pd
import OpenDartReader
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
        seen_names = set()  # 중복 방지
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

            # 새로운 종목이 없으면 마지막 페이지
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
    """재무 데이터 수집 (DART)"""
    result = {
        '시가총액': '-', 'PER': '-', '매출성장률': '-',
        '영업이익률': '-', '영업이익성장률': '-',
        '부채비율': '-', '현금보유량': '-', 'ROE': '-', 'PEG': '-',
    }

    try:
        # 네이버 금융에서 시가총액, PER 가져오기
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')

        # 시가총액
        cap_tag = soup.select_one('em#_market_sum')
        if cap_tag:
            result['시가총액'] = cap_tag.get_text(strip=True) + '억원'

        # PER
        per_tag = soup.select_one('em#_per')
        if per_tag:
            per_text = per_tag.get_text(strip=True)
            if per_text and per_text != 'N/A':
                result['PER'] = per_text

        time.sleep(0.5)
    except Exception as e:
        print(f"시가총액/PER 오류 ({stock_name}): {e}")

    try:
        # DART 재무데이터 - load_fs → fs 로 수정
        current_year = str(datetime.now().year - 1)
        prev_year = str(datetime.now().year - 2)

        # 연결재무제표 우선, 없으면 별도재무제표
        fs_current = dart.finstate(stock_name, current_year, fs_div='CFS')
        if fs_current is None or fs_current.empty:
            fs_current = dart.finstate(stock_name, current_year, fs_div='OFS')
        time.sleep(0.5)

        fs_prev = dart.finstate(stock_name, prev_year, fs_div='CFS')
        if fs_prev is None or fs_prev.empty:
            fs_prev = dart.finstate(stock_name, prev_year, fs_div='OFS')

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
    """네이버 뉴스 수집"""
    try:
        url = f"https://search.naver.com/search.naver?where=news&query={stock_name}+주가+상한가"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        titles = soup.select('.news_tit')[:3]
        news_list = [t.get_text() for t in titles]
        return ' / '.join(news_list) if news_list else "관련 뉴스 없음"
    except:
        return "뉴스 수집 실패"
    print(name, news)


def get_ai_summary(stock_name, news_text, financial):
    """GPT 요약"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": f"""
종목명: {stock_name}
관련 뉴스: {news_text}
재무지표:
- 시가총액: {financial['시가총액']}
- PER: {financial['PER']} / PEG: {financial['PEG']} / ROE: {financial['ROE']}
- 매출성장률: {financial['매출성장률']} / 영업이익률: {financial['영업이익률']} / 영업이익성장률: {financial['영업이익성장률']}
- 부채비율: {financial['부채비율']} / 현금보유량: {financial['현금보유량']}

1) 상한가 이유 1줄 요약
2) 재무 상태 1줄 평가
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
    date_str = get_today()
    today_display = f"{date_str[:4]}년 {date_str[4:6]}월 {date_str[6:]}일"

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

        print(f"{name} 처리 중...")
        financial = get_financial_data(ticker, name)
        time.sleep(1)
        news = get_news(name)
        print(f"[DEBUG] {name}")
        print(f"[NEWS] {news}")
        
        time.sleep(1)
        
        summary = get_ai_summary(name, news, financial)

        msg += f"*{name}* (+{rate:.1f}%)\n"
        msg += f"거래량: {volume}주\n"
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
