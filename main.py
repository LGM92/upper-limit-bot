import os
import time
from datetime import datetime
import pandas as pd
from pykrx import stock
from openai import OpenAI
import requests
from bs4 import BeautifulSoup
import OpenDartReader

# 환경변수에서 키 불러오기
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
OPENAI_API_KEY = os.environ['OPENAI_API_KEY']
DART_API_KEY = os.environ['DART_API_KEY']

client = OpenAI(api_key=OPENAI_API_KEY)
dart = OpenDartReader.OpenDartReader(DART_API_KEY)


def get_upper_limit_stocks():
    """상한가 종목 수집"""
    today = datetime.now().strftime("%Y%m%d")
    print(f"[{today}] 상한가 종목 수집 시작...")

    kospi = stock.get_market_ohlcv_by_ticker(today, market="KOSPI")
    time.sleep(1)
    kosdaq = stock.get_market_ohlcv_by_ticker(today, market="KOSDAQ")

    df = pd.concat([kospi, kosdaq])
    upper = df[df['등락률'] >= 29.5].copy()
    upper = upper.reset_index()

    print(f"상한가 종목 {len(upper)}개 발견")
    return upper


def get_financial_data(ticker, stock_name):
    """재무 데이터 수집 (DART + pykrx)"""
    result = {
        '시가총액': '-',
        'PER': '-',
        '매출성장률': '-',
        '영업이익률': '-',
        '영업이익성장률': '-',
        '부채비율': '-',
        '현금보유량': '-',
        'ROE': '-',
        'PEG': '-',
    }

    try:
        # pykrx에서 시가총액, PER 가져오기
        today = datetime.now().strftime("%Y%m%d")
        fundamental = stock.get_market_fundamental_by_ticker(today, market="ALL")
        if ticker in fundamental.index:
            per = fundamental.loc[ticker, 'PER']
            result['PER'] = f"{per:.1f}" if per > 0 else '-'

        cap_df = stock.get_market_cap_by_ticker(today, market="ALL")
        if ticker in cap_df.index:
            cap = cap_df.loc[ticker, '시가총액']
            result['시가총액'] = f"{cap / 1e8:.0f}억원"

        time.sleep(0.5)
    except Exception as e:
        print(f"pykrx 오류 ({stock_name}): {e}")

    try:
        # DART에서 재무 데이터 가져오기
        corp_code = dart.find_corp_code(stock_name)
        if not corp_code:
            return result

        current_year = str(datetime.now().year - 1)  # 전년도 결산 기준
        prev_year = str(datetime.now().year - 2)

        # 현재연도 재무제표
        fs_current = dart.finstate(corp_code, current_year, fs_div='CFS')
        # 전년도 재무제표
        fs_prev = dart.finstate(corp_code, prev_year, fs_div='CFS')

        if fs_current is not None and not fs_current.empty:
            def get_value(df, account_name):
                """재무제표에서 특정 항목 값 추출"""
                row = df[df['account_nm'].str.contains(account_name, na=False)]
                if not row.empty:
                    val = row.iloc[0]['thstrm_amount']
                    return float(str(val).replace(',', '')) if val else None
                return None

            # 현재연도 수치
            revenue_cur = get_value(fs_current, '매출액')
            op_income_cur = get_value(fs_current, '영업이익')
            total_debt = get_value(fs_current, '부채총계')
            total_equity = get_value(fs_current, '자본총계')
            cash = get_value(fs_current, '현금및현금성자산')
            net_income = get_value(fs_current, '당기순이익')

            # 전년도 수치
            revenue_prev, op_income_prev = None, None
            if fs_prev is not None and not fs_prev.empty:
                revenue_prev = get_value(fs_prev, '매출액')
                op_income_prev = get_value(fs_prev, '영업이익')

            # 계산
            if revenue_cur and op_income_cur:
                result['영업이익률'] = f"{(op_income_cur / revenue_cur * 100):.1f}%"

            if revenue_cur and revenue_prev and revenue_prev != 0:
                growth = (revenue_cur - revenue_prev) / abs(revenue_prev) * 100
                result['매출성장률'] = f"{growth:.1f}%"

            if op_income_cur and op_income_prev and op_income_prev != 0:
                op_growth = (op_income_cur - op_income_prev) / abs(op_income_prev) * 100
                result['영업이익성장률'] = f"{op_growth:.1f}%"

            if total_debt and total_equity and total_equity != 0:
                result['부채비율'] = f"{(total_debt / total_equity * 100):.1f}%"

            if cash:
                result['현금보유량'] = f"{cash / 1e8:.0f}억원"

            if net_income and total_equity and total_equity != 0:
                result['ROE'] = f"{(net_income / total_equity * 100):.1f}%"

            # PEG = PER / 영업이익성장률
            if result['PER'] != '-' and result['영업이익성장률'] != '-':
                per_val = float(result['PER'])
                op_growth_val = float(result['영업이익성장률'].replace('%', ''))
                if op_growth_val > 0:
                    result['PEG'] = f"{per_val / op_growth_val:.2f}"

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


def get_ai_summary(stock_name, news_text, financial):
    """GPT로 상한가 이유 + 재무 종합 요약"""
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
- PER: {financial['PER']}
- PEG: {financial['PEG']}
- ROE: {financial['ROE']}
- 매출성장률: {financial['매출성장률']}
- 영업이익률: {financial['영업이익률']}
- 영업이익성장률: {financial['영업이익성장률']}
- 부채비율: {financial['부채비율']}
- 현금보유량: {financial['현금보유량']}

1) 상한가 이유를 1줄로 요약
2) 재무 상태를 1줄로 평가 (긍정/부정/보통)
짧고 핵심만 써줘.
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
    today = datetime.now().strftime("%Y년 %m월 %d일")
    upper_df = get_upper_limit_stocks()

    if upper_df.empty:
        send_telegram(f"📊 {today}\n오늘 상한가 종목이 없습니다.")
        return

    msg = f"📈 *{today} 상한가 종목*\n"
    msg += f"총 {len(upper_df)}개 종목\n"
    msg += "━━━━━━━━━━━━━━\n\n"

    for _, row in upper_df.iterrows():
        ticker = row.get('티커', '')
        name = row.get('종목명', ticker)
        rate = row['등락률']
        volume = row['거래량']

        print(f"{name} 처리 중...")

        # 재무 데이터
        financial = get_financial_data(ticker, name)
        time.sleep(1)

        # 뉴스 + AI 요약
        news = get_news(name)
        time.sleep(1)
        summary = get_ai_summary(name, news, financial)

        msg += f"*{name}* (+{rate:.1f}%)\n"
        msg += f"거래량: {volume:,}주\n"
        msg += f"시가총액: {financial['시가총액']} | PER: {financial['PER']} | PEG: {financial['PEG']}\n"
        msg += f"ROE: {financial['ROE']} | 부채비율: {financial['부채비율']} | 현금: {financial['현금보유량']}\n"
        msg += f"매출성장률: {financial['매출성장률']} | 영업이익률: {financial['영업이익률']} | 영업이익성장률: {financial['영업이익성장률']}\n"
        msg += f"💬 {summary}\n\n"

    # 4000자 초과시 나눠서 전송
    for i in range(0, len(msg), 4000):
        send_telegram(msg[i:i+4000])
        time.sleep(1)

    print("전송 완료!")


if __name__ == "__main__":
    main()
