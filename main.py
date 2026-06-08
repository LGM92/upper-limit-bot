import os
import time
from datetime import datetime, timedelta
import pandas as pd
import feedparser
from openai import OpenAI
import requests
from bs4 import BeautifulSoup
import OpenDartReader

# 환경변수
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
OPENAI_API_KEY = os.environ['OPENAI_API_KEY']
DART_API_KEY = os.environ['DART_API_KEY']

client = OpenAI(api_key=OPENAI_API_KEY)
dart = OpenDartReader(DART_API_KEY)

TEST_DATE = None  # 테스트시 "20260605" 입력, 운영시 None

# 블로그성 뉴스 필터
BAD_KEYWORDS = ["투자분석", "주달", "톺아보기", "민낯", "수급포착", "주가분석", "주가전망"]

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
                if len(cols) < 8:
                    continue

                name = cols[3].get_text(strip=True)
                rate_text = cols[6].get_text(strip=True)

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

                seen_names.add(name)
                results.append({
                    'Code': ticker,
                    'Name': name,
                    'FLUC_RT': rate,
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


def get_news(stock_name):
    """구글 뉴스 RSS 수집 + 블로그성 기사 필터"""
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

        return news_list if news_list else []

    except Exception as e:
        print(f"뉴스 오류 ({stock_name}): {e}")
        return []


def get_dart_disclosure(ticker, stock_name):
    """DART 최근 7일 공시 - 중요 공시만 필터"""
    try:
        today = get_today()
        today_dt = datetime.strptime(today, "%Y%m%d")
        week_ago = (today_dt - timedelta(days=7)).strftime("%Y%m%d")

        today_fmt = f"{today[:4]}-{today[4:6]}-{today[6:]}"
        week_fmt = f"{week_ago[:4]}-{week_ago[4:6]}-{week_ago[6:]}"

        disclosures = dart.list(ticker, start=week_fmt, end=today_fmt)

        if not isinstance(disclosures, pd.DataFrame) or disclosures.empty:
            return []

        all_titles = disclosures['report_nm'].tolist()

        # 중요 공시 우선
        important = [t for t in all_titles if any(k in t for k in IMPORTANT_DISCLOSURES)]
        return important[:3] if important else all_titles[:2]

    except Exception as e:
        print(f"공시 오류 ({stock_name}): {e}")
        return []


def get_ai_summary(stock_name, news_list, disclosure_list):
    """GPT 요약 - 뉴스/공시 근거 기반"""
    try:
        news_text = "\n".join(news_list) if news_list else "뉴스 없음"
        disclosure_text = "\n".join(disclosure_list) if disclosure_list else "공시 없음"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=250,
            messages=[{
                "role": "user",
                "content": f"""
종목명: {stock_name}

[당일 공시]
{disclosure_text}

[관련 뉴스]
{news_text}

규칙:
1. 반드시 위 공시/뉴스 내용에만 근거해서 작성
2. 공시/뉴스에 없는 내용은 절대 추가하지 말 것
3. 명확한 근거가 없으면 "원인 불명" 작성
4. 추측, 일반론("시장 기대감", "투자심리") 금지
5. 핵심만 2~3줄로 간결하게

아래 형식으로만 답변:

[상한가 원인]
(공시/뉴스 근거 2~3줄 또는 "원인 불명")

[원인 분류]
AI / 반도체 / 바이오 / 정책수혜 / 실적개선 / M&A / 수급 / 기타 중 하나만
"""
            }]
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print(f"GPT 오류 ({stock_name}): {e}")
        return "[상한가 원인]\n원인 불명\n\n[원인 분류]\n기타"


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

    msg = f"📈 *{today_display} 상한가 종목*\n총 {len(upper_df)}개\n\n"

    for _, row in upper_df.iterrows():
        ticker = row.get('Code', '')
        name = row.get('Name', ticker)
        rate = row.get('FLUC_RT', 0)

        print(f"\n{'='*30}\n{name} 처리 중...")

        # 뉴스 수집
        news_list = get_news(name)
        print(f"[뉴스] {news_list}")
        time.sleep(1)

        # 공시 수집
        disclosure_list = get_dart_disclosure(ticker, name)
        print(f"[공시] {disclosure_list}")
        time.sleep(1)

        # GPT 요약
        summary = get_ai_summary(name, news_list, disclosure_list)

        # 메시지 조합
        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"*{name}* (+{rate:.1f}%)\n\n"
        msg += f"{summary}\n\n"

        # 관련 뉴스 제목 나열
        if news_list:
            msg += f"📰 *관련 뉴스*\n"
            for news in news_list[:3]:
                msg += f"• {news}\n"
            msg += "\n"

    # 4000자 초과시 나눠서 전송
    for i in range(0, len(msg), 4000):
        send_telegram(msg[i:i+4000])
        time.sleep(1)

    print("전송 완료!")


if __name__ == "__main__":
    main()
