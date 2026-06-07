import os
import time
from datetime import datetime
import pandas as pd
import FinanceDataReader as fdr
import dart_fss as dart_lib
from openai import OpenAI
import requests
from bs4 import BeautifulSoup

# 환경변수에서 키 불러오기
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
OPENAI_API_KEY = os.environ['OPENAI_API_KEY']
DART_API_KEY = os.environ['DART_API_KEY']

client = OpenAI(api_key=OPENAI_API_KEY)
dart_lib.set_api_key(DART_API_KEY)


def get_upper_limit_stocks():
    """상한가 종목 수집"""
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"[{today}] 상한가 종목 수집 시작...")

    try:
        kospi = fdr.StockListing('KOSPI')
        time.sleep(1)
        kosdaq = fdr.StockListing('KOSDAQ')
        all_stocks = pd.concat([kospi, kosdaq])

        # 오늘 시세 가져오기
        results = []
        for _, row in all_stocks.iterrows():
            code = row.get('Code', '')
            name = row.get('Name', '')
            if not code:
                continue
            try:
                df = fdr.DataReader(code, today, today)
                if df.empty:
                    continue
                change = df.iloc[-1].get('Change', 0)
                volume = df.iloc[-1].get('Volume', 0)
                close = df.iloc[-1].get('Close', 0)
                if change >= 0.295:
                    results.append({
                        'Code': code,
                        'Name': name,
                        'Change': change * 100,
                        'Volume': volume,
                        'Close': close
                    })
                time.sleep(0.1)
            except:
                continue

        upper_df = pd.DataFrame(results)
        print(f"상한가 종목 {len(upper_df)}개 발견")
        return upper_df

    except Exception as e:
        print(f"상한가 수집 오류: {e}")
        return pd.DataFrame()


def get_financial_data(ticker, stock_name):
    """재무 데이터 수집"""
    result = {
        '시가총액': '-', 'PER': '-', '매출성장률': '-',
        '영업이익률': '-', '영업이익성장률': '-',
        '부채비율': '-', '현금보유량': '-', 'ROE': '-', 'PEG': '-',
    }

    try:
        # 시가총액, PER - KRX 전체 종목 리스트에서
        krx = fdr.StockListing('KRX')
        row = krx[krx['Code'] == ticker]
        if not row.empty:
            if 'Marcap' in row.columns:
                cap = row.iloc[0]['Marcap']
                if pd.notna(cap):
                    result['시가총액'] = f"{int(cap) / 1e8:.0f}억원"
            if 'PER' in row.columns:
                per = row.iloc[0]['PER']
                if pd.notna(per) and float(per) > 0:
                    result['PER'] = f"{float(per):.1f}"
        time.sleep(0.5)
    except Exception as e:
        print(f"시가총액/PER 오류 ({stock_name}): {e}")

    try:
        # DART 재무데이터
        corp_list = dart_lib.get_corp_list()
        corp = corp_list.find_by_stock_code(ticker)
        if not corp:
            print(f"DART 종목 없음: {stock_name}")
            return result

        current_year = datetime.now().year - 1
        prev_year = datetime.now().year - 2

        fs_current = corp.load_fs(bgn_de=f'{current_year}0101')
        time.sleep(0.5)
        fs_prev = corp.load_fs(bgn_de=f'{prev_year}0101')

        def get_account(fs, label):
            try:
                val = fs[label].iloc[-1]
                return float(val) if pd.notna(val) else None
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
    today = datetime.now().strftime("%Y년 %m월 %d일")
    upper_df = get_upper_limit_stocks()

    if upper_df.empty:
        send_telegram(f"📊 {today}\n오늘 상한가 종목이 없습니다.")
        return

    msg = f"📈 *{today} 상한가 종목*\n총 {len(upper_df)}개\n━━━━━━━━━━━━━━\n\n"

    for _, row in upper_df.iterrows():
        ticker = row['Code']
        name = row['Name']
        rate = row['Change']
        volume = row['Volume']

        print(f"{name} 처리 중...")
        financial = get_financial_data(ticker, name)
        time.sleep(1)
        news = get_news(name)
        time.sleep(1)
        summary = get_ai_summary(name, news, financial)

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
