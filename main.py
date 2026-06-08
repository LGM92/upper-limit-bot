import os
import re
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

TEST_DATE = None  # 테스트시 "20260608", 운영시 None

# 저품질 뉴스 필터
BAD_KEYWORDS = [
    "투자분석", "주달", "대박", "추천주", "내일장",
    "종목은?", "예감", "급등예상", "수익률", "오늘의 주식"
]

# 중요 공시 키워드
IMPORTANT_DISCLOSURES = [
    "투자판단", "공급계약", "수주", "신규시설", "유상증자", "무상증자",
    "전환사채", "교환사채", "영업양수", "영업양도", "주요사항보고서",
    "자기주식취득", "합병", "분할", "임상", "특허", "계약"
]

# 원인 분류 (복수 허용, M&A 우선)
CATEGORY_RULES = {
    "M&A": ["합병", "인수", "영업양수", "영업양도"],
    "수급": ["자기주식취득", "자사주"],
    "정책수혜": ["국토부", "정부", "정책", "사업 선정", "수주"],
    "로봇": ["로봇", "휴머노이드", "감속기", "보스턴다이나믹스", "아틀라스", "액추에이터"],
    "AI": ["AI", "인공지능", "LLM"],
    "반도체": ["반도체", "MLCC", "웨이퍼"],
    "바이오": ["비만", "임상", "신약", "치료제", "바이오", "제약"],
    "실적개선": ["실적", "흑자", "영업이익"]
}


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

                seen_names.add(name)
                results.append({'Code': ticker, 'Name': name, 'FLUC_RT': rate})
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
    """구글 뉴스 RSS 수집 - 기본 필터만"""
    try:
        query = f'"{stock_name}"'.replace(" ", "+")
        url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)

        news_list = []
        for entry in feed.entries[:20]:
            title = entry.title

            # 저품질 키워드 제거
            if any(k in title for k in BAD_KEYWORDS):
                continue

            news_list.append(title)

        return news_list[:10]

    except Exception as e:
        print(f"뉴스 오류 ({stock_name}): {e}")
        return []


def get_dart_disclosure(ticker, stock_name):
    """DART 최근 7일 중요 공시"""
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
        important = [t for t in all_titles if any(k in t for k in IMPORTANT_DISCLOSURES)]
        return important[:3] if important else []

    except Exception as e:
        print(f"공시 오류 ({stock_name}): {e}")
        return []


def gpt_filter_news(stock_name, news_list, disclosure_list):
    """GPT 1단계: 주가 원인 관련 기사만 선별"""
    try:
        if not news_list and not disclosure_list:
            return [], []

        # 번호 붙여서 전달
        numbered = []
        for i, n in enumerate(news_list, 1):
            numbered.append(f"{i}. {n}")

        news_text = "\n".join(numbered) if numbered else "없음"
        disclosure_text = "\n".join(disclosure_list) if disclosure_list else "없음"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": f"""종목명: {stock_name}

공시:
{disclosure_text}

뉴스 목록:
{news_text}

위 뉴스 중 "{stock_name}" 주가 상승 원인을 설명하는 기사의 번호만 골라라.
연예인, 인물 소식, 체결강도, 매수잔량, 매도잔량, 실적 발표(원인 아님) 등은 제외.
원인 기사가 없으면 "없음"만 출력.

출력 형식: 번호만 쉼표로 구분
예시: 2,4,7
또는: 없음"""
            }]
        )

        result = response.choices[0].message.content.strip()
        print(f"[GPT필터] {stock_name}: {result}")

        if result == "없음" or not result:
            return [], disclosure_list

        # 선택된 번호 파싱
        selected = []
        for num in result.replace(" ", "").split(","):
            try:
                idx = int(num) - 1
                if 0 <= idx < len(news_list):
                    selected.append(news_list[idx])
            except:
                continue

        return selected, disclosure_list

    except Exception as e:
        print(f"GPT필터 오류 ({stock_name}): {e}")
        return news_list[:3], disclosure_list


def gpt_summarize(stock_name, filtered_news, disclosure_list):
    """GPT 2단계: 원인 1줄 요약 + 분류"""
    try:
        if not filtered_news and not disclosure_list:
            return "원인 불명", "기타"

        if disclosure_list:
            source_text = "[공시]\n" + "\n".join(disclosure_list)
            if filtered_news:
                source_text += "\n\n[뉴스]\n" + "\n".join(filtered_news[:3])
        else:
            source_text = "[뉴스]\n" + "\n".join(filtered_news[:3])

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": f"""종목명: {stock_name}

{source_text}

규칙:
1. 위 내용에 있는 사실만 사용
2. 추론 금지
3. "시장 기대감", "투자심리" 같은 일반론 금지
4. 근거 없으면 "원인 불명"

아래 형식으로만 출력:
요약: (30자 이내 한 줄)
분류: AI/반도체/바이오/로봇/정책수혜/M&A/수급/실적개선/기타 중 해당하는 것 최대 2개를 "/" 로 구분"""
            }]
        )

        text = response.choices[0].message.content.strip()
        print(f"[GPT요약] {stock_name}: {text}")

        # 파싱
        summary = "원인 불명"
        category = "기타"

        for line in text.split("\n"):
            if line.startswith("요약:"):
                summary = line.replace("요약:", "").strip()
            elif line.startswith("분류:"):
                category = line.replace("분류:", "").strip()

        return summary, category

    except Exception as e:
        print(f"GPT요약 오류 ({stock_name}): {e}")
        return "원인 불명", "기타"


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

    msg = f"📈 *{today_display} 상한가 종목*\n총 {len(upper_df)}개\n\n"

    for _, row in upper_df.iterrows():
        ticker = row.get('Code', '')
        name = row.get('Name', ticker)
        rate = row.get('FLUC_RT', 0)

        print(f"\n{'='*30}\n{name} 처리 중...")

        # 뉴스 수집
        news_list = get_news(name)
        print(f"[뉴스수집] {len(news_list)}개")
        time.sleep(1)

        # 공시 수집
        disclosure_list = get_dart_disclosure(ticker, name)
        print(f"[공시] {disclosure_list}")
        time.sleep(0.5)

        # GPT 1단계: 원인 기사 필터링
        filtered_news, disclosure_list = gpt_filter_news(name, news_list, disclosure_list)
        print(f"[필터후] {filtered_news}")
        time.sleep(1)

        # GPT 2단계: 요약 + 분류
        summary, category = gpt_summarize(name, filtered_news, disclosure_list)
        time.sleep(1)

        # 메시지 조합
        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"*{name}* (+{rate:.1f}%)\n\n"
        msg += f"📌 *요약*\n{summary}\n\n"
        msg += f"🏷 *분류*: {category}\n\n"

        if disclosure_list:
            msg += f"📄 *주요 공시*\n"
            for d in disclosure_list:
                msg += f"• {d}\n"
            msg += "\n"

        if filtered_news:
            msg += f"📰 *관련 뉴스*\n"
            for n in filtered_news[:3]:
                msg += f"• {n}\n"
            msg += "\n"
        elif news_list:
            # 필터 후 원인 기사 없어도 뉴스는 표시
            msg += f"📰 *관련 뉴스*\n"
            for n in news_list[:3]:
                msg += f"• {n}\n"
            msg += "\n"

    for i in range(0, len(msg), 4000):
        send_telegram(msg[i:i+4000])
        time.sleep(1)

    print("전송 완료!")


if __name__ == "__main__":
    main()
