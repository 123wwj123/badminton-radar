import os
import json
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

CF_ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
CF_DATABASE_ID = os.environ["CF_DATABASE_ID"]
CF_API_TOKEN = os.environ["CF_API_TOKEN"]

D1_QUERY_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/d1/database/{CF_DATABASE_ID}/query"

BASE_URL = "https://drsports.m.weibisai.com"
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/www/?_v=1013",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest"
}


def execute_d1_sql(sql: str, params: list = None):
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {"sql": sql}
    if params:
        payload["params"] = params

    resp = requests.post(D1_QUERY_URL, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"D1 SQL 执行失败: {data}")
    return data


def probe_weibisai_single_match(match_id: int) -> dict | None:
    """
    通过微比赛公开赛事详情接口探测单个赛事信息（无需登录 Token）
    """
    candidate_endpoints = [
        f"{BASE_URL}/api/{match_id}/match/index",
        f"{BASE_URL}/api/{match_id}/match/get_match_info",
    ]
    for url in candidate_endpoints:
        try:
            resp = requests.get(url, headers=COMMON_HEADERS, timeout=8)
            if resp.status_code != 200:
                continue
            text = resp.text.strip()
            if not text.startswith("{"):
                continue
            data = json.loads(text)
            m_info = data.get("matchInfo") or data.get("data", {}).get("matchInfo") or data.get("data")
            if not isinstance(m_info, dict):
                continue

            title = str(m_info.get("matchName") or m_info.get("title") or m_info.get("name") or "").strip()
            if not title:
                continue

            # 过滤非羽毛球赛事（若接口明确返回项目类型或标题不含羽毛球关键词则跳过）
            sport_name = str(m_info.get("sportName") or m_info.get("itemName") or "")
            if "羽" not in title and "羽" not in sport_name:
                return None

            # 提取状态
            state_text = str(m_info.get("state_text") or m_info.get("statusText") or "")
            if "结束" in state_text or "关闭" in state_text:
                return None  # 已结束赛事跳过
            elif "满" in state_text or "截止" in state_text:
                status = "full"
            elif "即将" in state_text or "未开始" in state_text:
                status = "upcoming"
            else:
                status = "open"

            # 提取项目组别列表
            raw_events = data.get("eventList") or data.get("events") or m_info.get("eventList") or []
            categories = []
            if isinstance(raw_events, list):
                for ev in raw_events:
                    if isinstance(ev, dict) and ev.get("eventName"):
                        categories.append(ev["eventName"])
            if not categories:
                categories = ["公开组"]

            city = str(m_info.get("cityName") or m_info.get("city") or "").strip()
            if not city:
                # 尝试从标题中提取常见城市名（如“合肥市羽毛球联赛”）
                for suffix in ["市", "州", "区"]:
                    idx = title.find(suffix)
                    if 1 <= idx <= 4:
                        city = title[:idx + 1]
                        break
            if not city:
                city = "全国/其他"

            match_date = str(m_info.get("matchStartTime") or m_info.get("startTime") or m_info.get("beginDate") or "")[:10]
            if not match_date:
                match_date = datetime.now().strftime("%Y-%m-%d")

            signup_deadline = str(m_info.get("enrollEndTime") or m_info.get("endTime") or "")[:10]

            return {
                "source_platform": "微比赛",
                "source_id": f"wbs_{match_id}",
                "title": title,
                "province": str(m_info.get("provinceName") or m_info.get("province") or ""),
                "city": city,
                "district": str(m_info.get("areaName") or m_info.get("district") or ""),
                "venue": str(m_info.get("address") or m_info.get("stadium") or m_info.get("place") or "详见微比赛详情页"),
                "match_date": match_date,
                "signup_deadline": signup_deadline,
                "categories": json.dumps(categories, ensure_ascii=False),
                "skill_level": str(m_info.get("levelDesc") or "见规程"),
                "entry_fee": str(m_info.get("feeDesc") or m_info.get("enrollFee") or "见详情"),
                "signup_url": f"{BASE_URL}/www/?_v=1013#/match/{match_id}",
                "status": status
            }
        except Exception:
            continue
    return None


def fetch_weibisai_events() -> list[dict]:
    """
    基于已知最新赛事 ID (18954) 周边区间扫描最新上架的羽毛球赛事
    """
    events = []
    # 扫描 18900 ~ 19150 区间内的公开赛事（覆盖近期及未来上架的赛事）
    target_ids = list(range(18900, 19150))
    print(f"正在并发扫描微比赛赛事区间 [{target_ids[0]} - {target_ids[-1]}]...")

    with ThreadPoolExecutor(max_workers=12) as executor:
        future_to_id = {executor.submit(probe_weibisai_single_match, mid): mid for mid in target_ids}
        for future in as_completed(future_to_id):
            res = future.result()
            if res:
                print(f"  [命中] {res['city']} | {res['title']} ({res['match_date']})")
                events.append(res)

    return events


def upsert_events_to_d1(events: list[dict]):
    if not events:
        print("本次未获取到新的有效羽毛球赛事数据。")
        return

    upsert_sql = """
    INSERT INTO tournaments (
        source_platform, source_id, title, province, city, district,
        venue, match_date, signup_deadline, categories, skill_level,
        entry_fee, signup_url, status, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(source_id) DO UPDATE SET
        title = excluded.title,
        city = excluded.city,
        venue = excluded.venue,
        match_date = excluded.match_date,
        signup_deadline = excluded.signup_deadline,
        categories = excluded.categories,
        entry_fee = excluded.entry_fee,
        status = excluded.status,
        updated_at = CURRENT_TIMESTAMP;
    """

    success_count = 0
    for ev in events:
        params = [
            ev["source_platform"], ev["source_id"], ev["title"],
            ev["province"], ev["city"], ev["district"],
            ev["venue"], ev["match_date"], ev["signup_deadline"],
            ev["categories"], ev["skill_level"], ev["entry_fee"],
            ev["signup_url"], ev["status"]
        ]
        try:
            execute_d1_sql(upsert_sql, params)
            success_count += 1
        except Exception as e:
            print(f"写入赛事 [{ev['title']}] 失败: {e}")

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 同步完成，共更新/插入 {success_count} 条真实赛事。")


if __name__ == "__main__":
    print("开始抓取各地羽毛球赛事...")
    all_events = fetch_weibisai_events()
    upsert_events_to_d1(all_events)
