import os
import json
import requests
from datetime import datetime

# 从环境变量读取 Cloudflare D1 凭证
CF_ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
CF_DATABASE_ID = os.environ["CF_DATABASE_ID"]
CF_API_TOKEN = os.environ["CF_API_TOKEN"]

D1_QUERY_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/d1/database/{CF_DATABASE_ID}/query"


def execute_d1_sql(sql: str, params: list = None):
    """通过 Cloudflare REST API 执行 D1 SQL"""
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


def fetch_weibisai_events() -> list[dict]:
    """
    抓取微比赛 (weibisai.com) 羽毛球公开赛事列表
    注：微比赛小程序/H5 列表接口通常返回 JSON，可根据实际抓包参数微调 url 与 payload
    """
    events = []
    # 微比赛常见赛事列表接口（项目类型 sport_type=1 通常代表羽毛球）
    api_url = "https://drsports.m.weibisai.com/api/match/list"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": "https://drsports.m.weibisai.com/",
        "Accept": "application/json, text/plain, */*"
    }

    # 拉取前 3 页最新报名中/即将开始的赛事
    for page in range(1, 4):
        try:
            params = {
                "page": page,
                "limit": 20,
                "sport_id": 1,  # 羽毛球分类ID
                "status": 1     # 1: 报名中 / 即将开始
            }
            resp = requests.get(api_url, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                print(f"[警告] 微比赛第 {page} 页请求状态码异常: {resp.status_code}")
                break

            res_json = resp.json()
            # 兼容常见嵌套结构 data.list 或 data.rows 或 data
            raw_list = (
                res_json.get("data", {}).get("list")
                or res_json.get("data", {}).get("rows")
                or (res_json.get("data") if isinstance(res_json.get("data"), list) else [])
            )
            if not raw_list:
                break

            for item in raw_list:
                match_id = str(item.get("id") or item.get("match_id") or "")
                if not match_id:
                    continue

                # 状态映射
                raw_status = str(item.get("status_text") or item.get("status") or "")
                if "满" in raw_status or raw_status == "3":
                    status = "full"
                elif "即将" in raw_status or raw_status == "0":
                    status = "upcoming"
                else:
                    status = "open"

                # 组别提取
                groups = item.get("groups") or item.get("items") or ["公开组"]
                if isinstance(groups, list) and groups and isinstance(groups[0], dict):
                    groups = [g.get("name", "") for g in groups if g.get("name")]

                events.append({
                    "source_platform": "微比赛",
                    "source_id": f"wbs_{match_id}",
                    "title": item.get("title") or item.get("name") or "未命名羽毛球赛",
                    "province": item.get("province") or "",
                    "city": item.get("city") or "未知城市",
                    "district": item.get("district") or item.get("area") or "",
                    "venue": item.get("address") or item.get("venue") or "详见报名页",
                    "match_date": str(item.get("start_time") or item.get("match_date") or "")[:10],
                    "signup_deadline": str(item.get("end_time") or item.get("signup_end") or "")[:10],
                    "categories": json.dumps(groups, ensure_ascii=False),
                    "skill_level": item.get("level_desc") or "不限",
                    "entry_fee": str(item.get("fee_desc") or item.get("price") or "见详情"),
                    "signup_url": f"https://drsports.m.weibisai.com/match/detail?id={match_id}",
                    "status": status
                })
        except Exception as e:
            print(f"[错误] 抓取微比赛第 {page} 页失败: {e}")
            break

    return events


def upsert_events_to_d1(events: list[dict]):
    """将标准化后的赛事批量 UPSERT 写入 Cloudflare D1"""
    if not events:
        print("本次未获取到有效赛事数据，跳过写入。")
        return

    upsert_sql = """
    INSERT INTO tournaments (
        source_platform, source_id, title, province, city, district,
        venue, match_date, signup_deadline, categories, skill_level,
        entry_fee, signup_url, status, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(source_id) DO UPDATE SET
        title = excluded.title,
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
        if not ev["match_date"]:
            continue
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

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 同步完成，共更新/插入 {success_count} 条赛事。")


if __name__ == "__main__":
    print("开始抓取各地羽毛球赛事...")
    all_events = []
    all_events.extend(fetch_weibisai_events())
    upsert_events_to_d1(all_events)
