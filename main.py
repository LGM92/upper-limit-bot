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

TEST_DATE = None  # 테스트시 "20260605", 운영시 None

# 저품질/무관 뉴스 필터
BAD_KEYWORDS = [
    "투자분석", "주달", "톺아보기", "민낯", "수급포착",
    "주가분석", "주가전망", "대박", "추천주", "내일장",
    "종목은?", "예감", "급등예상", "수익률", "오늘의 주식"
]

# 요약으로 쓰면 안 되는 시황성 제목
BAD_TITLE_PATTERNS = [
    "52주 신고가", "상승률 상위", "거래량 증가",
    "VI 발동", "+29", "+30", "% 상승"
]

# 우량 언론사
GOOD_MEDIA = [
    "연합뉴스", "한국경제", "매일경제", "이데일리",
    "머니투데이", "서울경제", "조선비즈", "전자신문",
    "딜사이트", "뉴스1", "헤럴드경제", "파이낸셜뉴스",
    "비즈니스워치", "더벨", "약업신문", "전자부품"
]

# 중요 공시 키워드
IMPORTANT_DISCLOSURES = [
    "투자판단", "공급계약", "수주", "신규시설", "유상증자", "무상증자",
    "전환사채", "교환사채", "영업양수", "영업양도", "주요사항보고서",
    "자기주식취득", "합병", "분할", "임상", "특허", "계약"
]

# 원인 분류 (M&A 우선)
CATEGORY_RULES = {
    "M&A": ["합병", "인수", "영업양수", "영업양도"],
    "수급": ["자기주식취득", "자사주"],
    "정책수혜": ["국토부", "정부", "정책", "사업 선정", "수주"],
    "로봇": ["로봇", "휴머노이드", "감속기", "자율주행"],
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
    """구글 뉴스 RSS - 종목명 포함 + 품질 필터"""
    try:
        query = f'"{stock_name}"'.replace(" ", "+")
        url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)

        filtered = []
        for entry in feed.entries[:30]:
            title = entry.title

            # source 추출
            source = ""
            try:
                source = entry.source.title
            except:
                pass

            # 1. 종목명 포함 여부 (공백 앞뒤로 정확히 매칭)
            # 예: "엔피" → "엔피디" 같은 유사 종목명 오염 방지
            import re
            if not re.search(r'(?<!\w)' + re.escape(stock_name) + r'(?!\w)', title):
                continue

            # 2. 저품질 키워드 제거
            if any(k in title for k in BAD_KEYWORDS):
                continue

            # 3. 시황성 제목 제거
            if any(p in title for p in BAD_TITLE_PATTERNS):
                continue

            filtered.append((title, source))

        # 우량 언론사 우선 정렬
        def media_score(item):
            title, source = item
            for media in GOOD_MEDIA:
                if media in source or media in title:
                    return 1
            return 0

        filtered = sorted(filtered, key=media_score, reverse=True)[:5]
        return [title for title, source in filtered]

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


def classify_reason(summary):
    """요약 텍스트만 보고 분류 - 뉴스 전체 아님"""
    for category, keywords in CATEGORY_RULES.items():
        if any(k in summary for k in keywords):
            return category
    return "기타"


def get_ai_summary(stock_name, news_list, disclosure_list):
    """GPT - 기사 제목 압축만, 추론 금지"""
    try:
        # 공시 있으면 공시 우선으로 GPT에 전달
        if disclosure_list:
            source_text = "[공시]\n" + "\n".join(disclosure_list)
            if news_list:
                source_text += "\n\n[뉴스]\n" + "\n".join(news_list[:3])
        elif news_list:
            source_text = "[뉴스]\n" + "\n".join(news_list[:5])
        else:
            return "관련 원인 기사 확인되지 않음"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": f"""종목명: {stock_name}

{source_text}

규칙:
1. 위 공시/뉴스 제목에 있는 내용만 사용
2. 추론 금지. 없는 내용 추가 금지
3. 아래 표현 절대 사용 금지:
   - "시장 기대감", "투자심리", "수급"
   - "매도잔량", "체결강도", "거래량"
   - "상한가", "급등", "강세"
4. 한 줄로만 출력 (30자 이내)
5. 기업 이벤트(계약/개발/선정/합병/임상)가 없으면 반드시 "원인 불명"만 출력

출력 예시:
웨이퍼 로봇 기업 인수 및 티아이에스 지분 취득
국토부 AI시티 혁신기술 사업 선정
4중 작용 비만치료제 전임상 결과 발표
원인 불명"""
            }]
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print(f"GPT 오류 ({stock_name}): {e}")
        return "원인 불명"


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

        news_list = get_news(name)
        print(f"[뉴스] {news_list}")
        time.sleep(1)

        disclosure_list = get_dart_disclosure(ticker, name)
        print(f"[공시] {disclosure_list}")
        time.sleep(0.5)

        summary = get_ai_summary(name, news_list, disclosure_list)
        category = classify_reason(summary)

        print(f"[요약] {summary} / [분류] {category}")

        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"*{name}* (+{rate:.1f}%)\n\n"
        msg += f"📌 *요약*\n{summary}\n\n"
        msg += f"🏷 *분류*: {category}\n\n"

        if disclosure_list:
            msg += f"📄 *주요 공시*\n"
            for d in disclosure_list:
                msg += f"• {d}\n"
            msg += "\n"

        if news_list:
            msg += f"📰 *관련 뉴스*\n"
            for n in news_list[:3]:
                msg += f"• {n}\n"
            msg += "\n"

        time.sleep(1)

    for i in range(0, len(msg), 4000):
        send_telegram(msg[i:i+4000])
        time.sleep(1)

    print("전송 완료!")


if __name__ == "__main__":
    main()
