import os
import time
from datetime import datetime, timedelta
import pandas as pd
import feedparser
import requests
from bs4 import BeautifulSoup
import OpenDartReader

# 환경변수
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
DART_API_KEY = os.environ['DART_API_KEY']

dart = OpenDartReader(DART_API_KEY)

TEST_DATE = None  # 테스트시 "20260605", 운영시 None

# 저품질 뉴스 필터
BAD_KEYWORDS = [
    "투자분석", "주달", "톺아보기", "민낯", "수급포착",
    "주가분석", "주가전망", "주가 왜", "무슨 회사"
]

# 요약으로 쓰면 안 되는 제목 패턴
BAD_TITLE_PATTERNS = [
    "상한가", "52주 신고가", "상승률 상위",
    "거래량 증가", "VI 발동", "급등세", "주가 왜",
    "주가 상한가", "상한가 마감", "상한가 직행",
    "+29", "+30", "% 상승", "% 올라"
]

# 원인 우선 키워드 (특징주 기사 선택용)
PRIORITY_KEYWORDS = [
    "특징주", "수주", "계약", "개발", "선정",
    "인수", "합병", "임상", "신약", "공장", "증설",
    "공급", "수혜", "출시", "승인"
]

# 원인 분류 (M&A 우선으로 순서 변경)
CATEGORY_RULES = {
    "M&A": ["합병", "인수", "영업양수", "영업양도"],
    "수급": ["자기주식취득", "자사주"],
    "정책수혜": ["국토부", "정부", "정책", "사업 선정", "수주"],
    "로봇": ["로봇", "휴머노이드", "감속기", "자율주행"],
    "AI": ["AI", "인공지능", "LLM", "ChatGPT"],
    "반도체": ["반도체", "MLCC", "삼성전자", "삼성전기", "SK하이닉스"],
    "바이오": ["비만", "임상", "신약", "치료제", "의약", "바이오", "제약"],
    "실적개선": ["실적", "흑자", "영업이익"]
}

# 우량 언론사 (점수 +10)
GOOD_MEDIA = [
    "연합뉴스", "한국경제", "매일경제", "이데일리",
    "머니투데이", "서울경제", "조선비즈", "전자신문", "딜사이트"
]

# 중요 공시 키워드
IMPORTANT_DISCLOSURES = [
    "투자판단", "공급계약", "수주", "신규시설", "유상증자", "무상증자",
    "전환사채", "교환사채", "영업양수", "영업양도", "주요사항보고서",
    "자기주식취득", "합병", "분할", "임상", "특허", "계약"
]

# 원인 분류 키워드 매핑 (M&A, 수급 정교화)
CATEGORY_RULES = {
    "AI": ["AI", "인공지능", "LLM", "ChatGPT"],
    "반도체": ["반도체", "MLCC", "삼성전자", "삼성전기", "SK하이닉스"],
    "바이오": ["비만", "임상", "신약", "치료제", "의약", "바이오", "제약"],
    "로봇": ["로봇", "휴머노이드", "감속기", "자율주행"],
    "정책수혜": ["국토부", "정부", "정책", "사업 선정", "수주"],
    "M&A": ["합병", "인수", "영업양수", "영업양도"],
    "수급": ["자기주식취득", "자사주"],
    "실적개선": ["실적", "흑자", "매출", "영업이익"]
}

# 원인으로 인정할 뉴스 키워드
REASON_KEYWORDS = [
    "특징주", "수주", "계약", "선정", "개발", "합병", "인수",
    "임상", "신약", "치료제", "공장", "증설", "공급", "상한가",
    "급등", "감속기", "휴머노이드", "AI", "반도체", "바이오"
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


def news_score(title, source=""):
    """뉴스 품질 점수 - source 우선 활용"""
    score = 0
    check_text = source + " " + title
    for media in GOOD_MEDIA:
        if media in check_text:
            score += 10
    for bad in BAD_KEYWORDS:
        if bad in title:
            score -= 10
    # 원인 키워드 있으면 가산점
    for keyword in REASON_KEYWORDS:
        if keyword in title:
            score += 5
            break
    return score


def get_news(stock_name):
    """구글 뉴스 RSS 수집 + 품질 필터"""
    try:
        # 종목명만 검색 (상한가 추가시 시황기사만 나옴)
        query = f'"{stock_name}"'.replace(" ", "+")
        url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)

        filtered = []
        for entry in feed.entries[:30]:
            title = entry.title
            source = ""
            try:
                source = entry.source.title
            except:
                pass

            # 1. 종목명 포함 여부 확인
            if stock_name not in title:
                continue

            # 2. 저품질 키워드 제거
            if any(k in title for k in BAD_KEYWORDS):
                continue

            # 3. 상한가/시황 기사 제거
            if any(p in title for p in BAD_TITLE_PATTERNS):
                continue

            filtered.append((title, source))

        # 품질 점수 기준 정렬 후 상위 3개
        filtered = sorted(filtered, key=lambda x: news_score(x[0], x[1]), reverse=True)[:3]
        return [title for title, source in filtered]

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
        important = [t for t in all_titles if any(k in t for k in IMPORTANT_DISCLOSURES)]
        return important[:3] if important else []

    except Exception as e:
        print(f"공시 오류 ({stock_name}): {e}")
        return []


def classify_reason(news_list, disclosure_list):
    """키워드 기반 원인 분류 - AI 추론 없음"""
    combined = " ".join(news_list + disclosure_list)
    for category, keywords in CATEGORY_RULES.items():
        for keyword in keywords:
            if keyword in combined:
                return category
    return "기타"


def has_reason_news(news_list):
    """원인으로 인정할 수 있는 뉴스인지 확인"""
    for news in news_list:
        if any(k in news for k in REASON_KEYWORDS):
            return True
    return False


def generate_summary(news_list, disclosure_list):
    """공시 최우선 → PRIORITY 기사 → REASON 기사 → 원인 불명"""

    # 1. 공시 최우선
    if disclosure_list:
        return disclosure_list[0]

    # 2. PRIORITY_KEYWORDS 포함 기사
    for news in news_list:
        if any(k in news for k in PRIORITY_KEYWORDS):
            return news

    # 3. REASON_KEYWORDS 포함 기사
    for news in news_list:
        if any(k in news for k in REASON_KEYWORDS):
            return news

    # 4. 원인 불명
    return "관련 원인 기사 확인되지 않음"


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

        news_list = get_news(name)
        time.sleep(1)

        disclosure_list = get_dart_disclosure(ticker, name)
        time.sleep(0.5)

        summary = generate_summary(news_list, disclosure_list)
        category = classify_reason(news_list, disclosure_list)

        # 메시지 조합
        msg += f"━━━━━━━━━━━━━━\n"
        msg += f"*{name}* (+{rate:.1f}%)\n\n"
        msg += f"📌 *요약*\n{summary}\n\n"
        msg += f"🏷 *분류*\n{category}\n\n"

        if disclosure_list:
            msg += f"📄 *주요 공시*\n"
            for d in disclosure_list:
                msg += f"• {d}\n"
            msg += "\n"

        if news_list:
            msg += f"📰 *관련 뉴스*\n"
            for n in news_list:
                msg += f"• {n}\n"
            msg += "\n"

    # 4000자 초과시 나눠서 전송
    for i in range(0, len(msg), 4000):
        send_telegram(msg[i:i+4000])
        time.sleep(1)

    print("전송 완료!")


if __name__ == "__main__":
    main()
