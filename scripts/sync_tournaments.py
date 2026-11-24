import os
import json
import requests
from datetime import datetime

CF_ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
CF_DATABASE_ID = os.environ["CF_DATABASE_ID"]
CF_API_TOKEN = os.environ["CF_API_TOKEN"]

D1_QUERY_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/d1/database/{CF_DATABASE_ID}/query"

# 微比赛地方承办机构渠道列表（后续可随时追加其他城市的 *.m.weibisai.com 子域名）
WEIBISAI_CHANNELS = [
    {"name": "微比赛·德仁体育", "base_url": "https://drsports.m.weibisai.com"},
]


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


def normalize_date(raw_date_range: str) -> tuple[str, str]:
    """
    将 '2026.11.07 - 11.08' 或 '2026.10.14 - 10.30' 格式化为标准 YYYY-MM-DD
    """
    if not raw_date_range:
        return datetime.now().strftime("%Y-%m-%d"), ""
    parts = [p.strip() for p in raw_date_range.split("-")]
    start_str = parts[0].replace(".", "-")
    end_str = ""
    if len(parts) > 1:
        end_part = parts[1].replace(".", "-")
        if len(end_part) <= 5 and len(start_str) >= 4:
            end_str = f"{start_str[:4]}-{end_part}"
        else:
            end_str = end_part
    return start_str, end_str


def fetch_weibisai_channel(channel: dict) -> list[dict]:
    """
    调用真实 POST /api/channel/search_match 接口抓取羽毛球赛事
    """
    base_url = channel["base_url"]
    source_name = channel["name"]
    api_url = f"{base_url}/api/channel/search_match"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": f"{base_url}/www/?_v=1013",
        "Origin": base_url,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/plain, */*"
    }

    events = []
    page = 1
    while page <= 10:
        # 严格对齐抓包 Form data 参数
        payload = {
            "page": page,
            "type": 0,
            "state": -1,
            "year": -1
        }
        try:
            resp = requests.post(api_url, headers=headers, data=payload, timeout=15)
            if resp.status_code != 200:
                print(f"[{source_name}] 第 {page} 页返回状态码 {resp.status_code}")
                break

            res_json = resp.json()
            match_list = res_json.get("matchList") or []
            print(f"[{source_name}] 第 {page} 页获取到 {len(match_list)} 条记录 (总数: {res_json.get('count', 0)})")

            if not match_list:
                break

            for m in match_list:
                title = str(m.get("name") or "").strip()
                # 严格过滤羽毛球赛事（排除如登山比赛等非羽毛球项目）
                if m.get("sports") != 100 or "登山" in title:
                    continue

                match_id = m.get("id")
                state = m.get("state")
                state_text = str(m.get("state_text") or "")

                if state == 4 or "结束" in state_text:
                    status = "full"
                elif m.get("enrollFull") or "满" in state_text or "截止" in state_text:
                    status = "full"
                elif state == 1 or "即将" in state_text or "未开始" in state_text:
                    status = "upcoming"
                else:
                    status = "open"

                play_start, _ = normalize_date(m.get("playDates", ""))
                _, enroll_end = normalize_date(m.get("enrollDates", ""))

                mode_text = m.get("sports_mode_text") or "公开组"
                categories = [mode_text]
                if m.get("isPoint"):
                    categories.append("积分赛")

                events.append({
                    "source_platform": source_name,
                    "source_id": f"wbs_{match_id}",
                    "title": title,
                    "province": m.get("province") or "",
                    "city": m.get("city") or "未知城市",
                    "district": "",
                    "venue": m.get("address") or "详见微比赛详情页",
                    "match_date": play_start,
                    "signup_deadline": enroll_end or "见详情",
                    "categories": json.dumps(categories, ensure_ascii=False),
                    "skill_level": f"已报名 {m.get('enroll_count', 0)} 人/队",
                    "entry_fee": "见报名页",
                    "signup_url": f"{base_url}/www/?_v=1013#/match/{match_id}",
                    "status": status
                })

            if res_json.get("noMore", True):
                break
            page += 1
        except Exception as e:
            print(f"[{source_name}] 第 {page} 页抓取异常: {e}")
            break

    return events


def upsert_events_to_d1(events: list[dict]):
    if not events:
        print("本次未获取到有效羽毛球赛事数据。")
        return

    upsert_sql = """
    INSERT INTO tournaments (
        source_platform, source_id, title, province, city, district,
        venue, match_date, signup_deadline, categories, skill_level,
        entry_fee, signup_url, status, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(source_id) DO UPDATE SET
        title = excluded.title,
        province = excluded.province,
        city = excluded.city,
        venue = excluded.venue,
        match_date = excluded.match_date,
        signup_deadline = excluded.signup_deadline,
        categories = excluded.categories,
        skill_level = excluded.skill_level,
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

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 同步完成，共写入/更新 {success_count} 条真实赛事！")


if __name__ == "__main__":
    print("开始通过 search_match 接口同步各地羽毛球赛事...")
    all_events = []
    for ch in WEIBISAI_CHANNELS:
        all_events.extend(fetch_weibisai_channel(ch))
    upsert_events_to_d1(all_events)
