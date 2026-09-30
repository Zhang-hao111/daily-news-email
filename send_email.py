import json
import logging
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from pathlib import Path

import markdown
import requests
import yaml
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parent / '.env')  # 不依赖工作目录


def _env(key, default=''):
    """读取环境变量，空字符串视为未设置（GitHub Actions 会把未配置的 Secret 传成空串）"""
    v = os.getenv(key)
    return v if v not in (None, '') else default


# ========== 配置 ==========
VAULT_PATH = _env('OBSIDIAN_VAULT_PATH')
DEEPSEEK_KEY = _env('DEEPSEEK_API_KEY')
DEEPSEEK_URL = _env('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
DEEPSEEK_MODEL = _env('DEEPSEEK_MODEL', 'deepseek-chat')
REASONING_EFFORT = _env('DEEPSEEK_REASONING_EFFORT', 'none')  # 推理模型默认关闭思考：机械抽取任务无需思维链，且思考会吃满 max_tokens 导致 JSON 截断

# ========== 管道与源配置（config.yaml：加源/加管道改配置不改代码） ==========
CONFIG_PATH = Path(__file__).resolve().parent / 'config.yaml'
try:
    CONFIG = yaml.safe_load(CONFIG_PATH.read_text(encoding='utf-8')) or {}
except FileNotFoundError:
    sys.exit(f'缺少配置文件：{CONFIG_PATH}')
except yaml.YAMLError as e:
    sys.exit(f'config.yaml 格式错误：{e}')

PIPELINE = CONFIG.get('pipelines') or {}
for _kind, _conf in PIPELINE.items():
    _missing = {'label', 'feeds', 'max_total', 'select_rules', 'category', 'key_data'} - set(_conf or {})
    if _missing:
        sys.exit(f'config.yaml 管道 {_kind} 缺少字段：{", ".join(sorted(_missing))}')
if not PIPELINE:
    sys.exit('config.yaml 未定义任何 pipelines')
CATEGORY_ORDER = CONFIG.get('categories_order') or []
SMTP_HOST = _env('SMTP_HOST')
SMTP_PORT = int(_env('SMTP_PORT', '465'))
SMTP_USER = _env('SMTP_USER')
SMTP_AUTH = _env('SMTP_AUTH_CODE')
EMAIL_TO = _env('EMAIL_TO')

# 借鉴 TrendRadar 的可配置项
NEWS_MAX_AGE_HOURS = float(_env('NEWS_MAX_AGE_HOURS', str(CONFIG['defaults']['news_max_age_hours'])))   # 超过 N 小时的文章视为旧闻
FOCUS_KEYWORDS = [k.strip() for k in _env('FOCUS_KEYWORDS').split(',') if k.strip()]
EXCLUDE_KEYWORDS = [k.strip() for k in _env(
    'EXCLUDE_KEYWORDS', '优惠券,限时优惠,免费领取,抽奖,福利,扫码,带货,种草').split(',') if k.strip()]
PUSH_WEBHOOK_URL = _env('PUSH_WEBHOOK_URL')   # 群机器人 webhook，留空不推送
PUSH_WEBHOOK_TYPE = _env('PUSH_WEBHOOK_TYPE', 'feishu')  # feishu / dingtalk / wecom

try:
    import trafilatura
except ImportError:
    trafilatura = None

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

FETCH_TIMEOUT = 15          # RSS 请求超时（秒）
AI_MAX_WORKERS = 6          # AI 分析并发数，过高可能触发 API 限流
FETCH_MAX_WORKERS = 10      # RSS 抓取并发数
SEEN_MAX_DAYS = 7           # 已推送链接的保留天数，期间不重复推送
WEEKLY_MAX_TOTAL = 15       # 周报最终保留的条数
SOURCE_FAIL_DISABLE_DAYS = int(_env('SOURCE_FAIL_DISABLE_DAYS', '7'))    # 连续 N 天无数据 → 停用
SOURCE_PROBE_INTERVAL_DAYS = int(_env('SOURCE_PROBE_INTERVAL_DAYS', '14'))  # 停用后每隔 N 天复检一次

CATEGORY_ORDER = CONFIG.get('categories_order') or []

DIGEST_KEYWORDS = ['晚报', '早报', '日报', '周报', '速报', '快讯', '氪星']

# ========== 日志（控制台 + logs/年月.log，定时任务排障靠它） ==========
LOG_DIR = Path(__file__).resolve().parent / 'logs'
STATE_DIR = Path(__file__).resolve().parent / 'state'
SEEN_FILE = STATE_DIR / 'seen_urls.json'
SOURCE_HEALTH_FILE = STATE_DIR / 'source_health.json'


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_DIR / f'{datetime.now():%Y%m}.log', encoding='utf-8'),
        ],
    )


setup_logging()

_ds_client = None


def get_ds_client():
    """懒加载 DeepSeek 客户端；未配置 API key 时返回 None"""
    global _ds_client
    if _ds_client is None and DEEPSEEK_KEY:
        _ds_client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_URL)
    return _ds_client


# ========== 通用工具 ==========
def strip_html(text):
    return re.sub(r'<[^>]+>', '', text).strip()


def parse_json(text):
    """去掉可能的 markdown 代码块围栏后解析 JSON"""
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)


def extract_content(resp):
    """取出回答文本；空响应时带上 finish_reason 和用量，便于定位（如思考吃满 max_tokens）"""
    choice = resp.choices[0]
    content = choice.message.content or ''
    if not content.strip():
        raise RuntimeError(f'空响应 finish={choice.finish_reason} usage={resp.usage}')
    return content


def parse_feed_date(text):
    """解析 RSS（RFC 822）或 Atom（ISO 8601）日期，失败返回 None"""
    if not text:
        return None
    text = text.strip()
    try:
        return parsedate_to_datetime(text)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except Exception:
        return None


def call_with_retry(fn, retries=3, description=''):
    """带指数退避的重试，全部失败时抛出最后一次异常"""
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                raise
            wait = 2 * attempt
            logging.warning(f'  {description} 第{attempt}次失败: {e}，{wait}s后重试')
            time.sleep(wait)


# ========== 第1步：抓取 RSS ==========
def fetch_rss(url, timeout=FETCH_TIMEOUT):
    """从 RSS 源抓取新闻，支持 RSS 2.0 和 Atom，附带发布时间"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        # 用 bytes 让 ElementTree 按 XML 声明的编码解析，避免 resp.text 二次解码出错
        root = ET.fromstring(resp.content)

        dc_date = '{http://purl.org/dc/elements/1.1/}date'
        items = []
        for item in root.iter('item'):
            title = (item.findtext('title') or '').strip()
            link = (item.findtext('link') or '').strip()
            desc = strip_html(item.findtext('description') or '')
            pub = item.findtext('pubDate') or item.findtext(dc_date)
            if title:
                items.append({'title': title, 'url': link, 'content': desc[:300],
                              'published_dt': parse_feed_date(pub)})
        if not items:
            atom = '{http://www.w3.org/2005/Atom}'
            for entry in root.iter(f'{atom}entry'):
                title = (entry.findtext(f'{atom}title') or '').strip()
                link_el = entry.find(f'{atom}link')
                link = link_el.get('href', '') if link_el is not None else ''
                desc = strip_html(entry.findtext(f'{atom}summary') or '')
                pub = entry.findtext(f'{atom}published') or entry.findtext(f'{atom}updated')
                if title:
                    items.append({'title': title, 'url': link, 'content': desc[:300],
                                  'published_dt': parse_feed_date(pub)})
        return items[:15]  # 每个源最多15条
    except Exception as e:
        logging.warning(f'  抓取失败 {url}: {e}')
        return []


def fetch_all_news(feeds):
    """并发抓取指定 RSS 源，按源顺序汇总，返回 (新闻列表, 各源条数)"""
    by_name = {}
    with ThreadPoolExecutor(max_workers=min(FETCH_MAX_WORKERS, len(feeds))) as pool:
        futures = {pool.submit(fetch_rss, url): name for name, url in feeds.items()}
        for fut in as_completed(futures):
            by_name[futures[fut]] = fut.result()

    all_news = []
    counts = {}
    for name in feeds:  # 保持源顺序，方便对照日志
        items = by_name.get(name, [])
        counts[name] = len(items)
        logging.info(f'  {name}: {len(items)} 条')
        for item in items:
            item['source'] = name
        all_news.extend(items)
    return all_news, counts


def _days_since(date_str):
    try:
        return (datetime.now() - datetime.strptime(date_str, '%Y-%m-%d')).days
    except (TypeError, ValueError):
        return 0


def update_source_health(counts, today_str=None):
    """记录各源当日抓取结果：有数据则清零恢复，无数据则累加连续失败天数"""
    today_str = today_str or datetime.now().strftime('%Y-%m-%d')
    try:
        health = json.loads(SOURCE_HEALTH_FILE.read_text(encoding='utf-8')) \
            if SOURCE_HEALTH_FILE.exists() else {}
    except Exception:
        health = {}
    for name, count in counts.items():
        if count > 0:
            health.pop(name, None)  # 恢复正常，移出观察名单
        else:
            entry = health.get(name) or {'fail_streak': 0, 'last_attempt': today_str}
            entry['fail_streak'] = int(entry.get('fail_streak', 0)) + 1
            entry['last_attempt'] = today_str
            health[name] = entry
    try:
        STATE_DIR.mkdir(exist_ok=True)
        SOURCE_HEALTH_FILE.write_text(json.dumps(health, ensure_ascii=False), encoding='utf-8')
    except Exception as e:
        logging.warning(f'记录源健康失败: {e}')


def get_disabled_sources():
    """返回已停用的源 {name: entry}（连续 SOURCE_FAIL_DISABLE_DAYS 天无数据）"""
    try:
        health = json.loads(SOURCE_HEALTH_FILE.read_text(encoding='utf-8')) \
            if SOURCE_HEALTH_FILE.exists() else {}
    except Exception:
        return {}
    return {name: e for name, e in health.items()
            if int(e.get('fail_streak', 0)) >= SOURCE_FAIL_DISABLE_DAYS}


def dedupe_articles(news_list):
    """按标题和 URL 去掉完全重复的条目（不同源对同一事件的报道仍靠 AI 去重）"""
    seen_titles, seen_urls = set(), set()
    out = []
    for n in news_list:
        title_key = re.sub(r'\s+', '', n['title'].lower())
        url_key = (n.get('url') or '').split('?')[0].lower()
        if (title_key and title_key in seen_titles) or (url_key and url_key in seen_urls):
            continue
        seen_titles.add(title_key)
        seen_urls.add(url_key)
        out.append(n)
    return out


def filter_stale(news_list, max_age_hours=NEWS_MAX_AGE_HOURS):
    """新鲜度过滤：超过 N 小时的旧文不收录（借鉴 TrendRadar；无日期的源默认保留）"""
    now = datetime.now()
    kept = []
    for n in news_list:
        dt = n.get('published_dt')
        if dt is not None:
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            if (now - dt).total_seconds() > max_age_hours * 3600:
                continue
        kept.append(n)
    dropped = len(news_list) - len(kept)
    if dropped:
        logging.info(f'  新鲜度过滤（{max_age_hours}小时内）丢弃 {dropped} 条旧闻')
    return kept


def filter_excluded(news_list):
    """过滤综合新闻（晚报/早报等）和排除关键词（软文/广告类）命中的标题"""
    excluded = DIGEST_KEYWORDS + EXCLUDE_KEYWORDS
    return [n for n in news_list if not any(kw in n['title'] for kw in excluded)]


def focus_hits(n):
    """标题命中的关注关键词数量"""
    text = n['title'].lower()
    return sum(1 for kw in FOCUS_KEYWORDS if kw.lower() in text)


def filter_seen(news_list):
    """过滤近 N 天内已推送过的文章（按 URL 记录，避免跨天重复推送）"""
    seen = load_seen_urls()
    kept = [n for n in news_list if not n.get('url') or n['url'] not in seen]
    dropped = len(news_list) - len(kept)
    if dropped:
        logging.info(f'  过滤近{SEEN_MAX_DAYS}天已推送的重复文章 {dropped} 条')
    return kept


def load_seen_urls():
    try:
        return json.loads(SEEN_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def remember_seen(urls):
    """记录本次已推送的文章 URL，并清理过期记录"""
    if not urls:
        return
    state = load_seen_urls()
    today = datetime.now().strftime('%Y-%m-%d')
    for url in urls:
        if url:
            state[url] = today
    cutoff = (datetime.now() - timedelta(days=SEEN_MAX_DAYS)).strftime('%Y-%m-%d')
    state = {u: d for u, d in state.items() if d >= cutoff}
    try:
        STATE_DIR.mkdir(exist_ok=True)
        SEEN_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        logging.info(f'已记录 {len(urls)} 条推送链接（防重复）')
    except Exception as e:
        logging.warning(f'记录已推送链接失败: {e}')


def balanced_sample(news_list, max_total=40):
    """从每个源均匀采样，作为 AI 筛选失败时的 fallback"""
    filtered = filter_excluded(news_list)
    if not filtered:
        return []

    by_source = {}
    for item in filtered:
        by_source.setdefault(item['source'], []).append(item)

    per_source = max(5, max_total // len(by_source))
    sampled = []
    for items in by_source.values():
        sampled.extend(items[:per_source])
    return sampled[:max_total]


# ========== 第2步：AI 筛选与分析 ==========
def ai_select(news_list, kind, max_total):
    """用 AI 从新闻中筛选最有价值的文章，保证多样性；失败时降级为均匀采样"""
    client = get_ds_client()
    if client is None:
        logging.warning('  未配置 DEEPSEEK_API_KEY，跳过 AI 筛选，使用均匀采样')
        return balanced_sample(news_list, max_total)

    filtered = filter_excluded(news_list)
    if not filtered:
        return []

    list_text = '\n'.join(
        f"[{i}] 【{n['source']}】{n['title']} — {(n.get('content') or '')[:80]}"
        for i, n in enumerate(filtered)
    )
    conf = PIPELINE[kind]
    focus_hint = f'\n与这些关注关键词相关的文章优先入选：{"、".join(FOCUS_KEYWORDS)}\n' if FOCUS_KEYWORDS else ''
    prompt = f"""以下是 {len(filtered)} 条{conf['label']}新闻，请从中挑选最有新闻价值的 {max_total} 条。

挑选原则：
{conf['select_rules']}{focus_hint}
新闻列表：
{list_text}

只输出一个 JSON 数组，包含选中文章的编号，例如 [0, 3, 5, 7]，不要输出其他内容。"""

    try:
        resp = call_with_retry(
            lambda: client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{'role': 'user', 'content': prompt}],
            extra_body={'reasoning_effort': REASONING_EFFORT},
                temperature=0.3,
                max_tokens=8000,
            ),
            description='AI 筛选',
        )
        indices = parse_json(extract_content(resp))
        selected = [filtered[int(i)] for i in indices
                    if isinstance(i, (int, float)) and 0 <= int(i) < len(filtered)]
        logging.info(f'  AI 筛选了 {len(selected)} 条{conf["label"]}新闻')
        return selected[:max_total]
    except Exception as e:
        logging.warning(f'  AI 筛选失败，降级使用均匀采样: {e}')
        return balanced_sample(news_list, max_total)


def fallback_result(n):
    """AI 分析失败或不可用时的兜底结果，直接用 RSS 摘要"""
    return {
        'title': n['title'],
        'source': n['source'],
        'url': n.get('url', ''),
        'category': '其他',
        'summary': (n.get('content') or n['title'])[:200],
        'key_data': '无',
        'comment': '无',
    }


def fetch_full_text(news_list, max_chars=3000):
    """并发抓取选中文章的正文（trafilatura 抽取，反爬站点经 r.jina.ai 兜底），失败保留 RSS 摘要"""
    if trafilatura is None:
        logging.warning('  未安装 trafilatura，跳过正文抓取，使用 RSS 摘要')
        return

    def _extract(html):
        text = trafilatura.extract(html, include_comments=False,
                                   include_tables=False) or ''
        return strip_html(text).strip()

    def _fetch(n):
        url = n.get('url')
        if not url:
            return
        # 先直连抽取；结果太短说明是反爬壳页面，改走 r.jina.ai 免费阅读代理
        for attempt, target in enumerate((url, f'https://r.jina.ai/{url}')):
            try:
                resp = requests.get(target, headers=HEADERS, timeout=20 if attempt else 10)
                resp.raise_for_status()
                if attempt == 0:
                    text = _extract(resp.text)
                else:
                    raw = resp.text
                    if 'Markdown Content:' in raw:  # 去掉 jina 返回的元信息头
                        raw = raw.split('Markdown Content:', 1)[1]
                    text = strip_html(raw).strip()
                # 抽取结果太短说明反爬壳或正文识别失败，不可信
                if len(text) > 200:
                    n['full_text'] = text[:max_chars]
                    if attempt:
                        jina_hits.append(n['title'])
                    return
            except Exception:
                continue

    jina_hits = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_fetch, news_list))
    got = sum(1 for n in news_list if n.get('full_text'))
    extra = f'（其中 {len(jina_hits)} 条经 jina 兜底）' if jina_hits else ''
    logging.info(f'  正文抓取成功 {got}/{len(news_list)} 条{extra}')


def _parse_score(value):
    """AI 返回的重要性评分转 1-10 整数，异常给 0（排序时垫底但不影响展示）"""
    try:
        return max(1, min(10, int(float(value))))
    except (TypeError, ValueError):
        return 0


def analyze_article(client, n, kind):
    """分析单条新闻，输出分类/摘要/关键数据/点评/重要性评分"""
    conf = PIPELINE[kind]
    content = n.get('full_text') or (n.get('content') or '')[:300]
    content_note = '（以下为文章正文节选）' if n.get('full_text') else '（以下为 RSS 摘要，信息有限请勿过度发挥）'
    prompt = f"""分析这条{conf['label']}新闻，输出JSON：
标题：{n['title']}
来源：{n['source']}
{content_note}
{content}

输出格式（只输出JSON）：
{{
  "category": "{conf['category']}",
  "summary": "2-3句话摘要",
  "key_data": "{conf['key_data']}",
  "comment": "简短点评",
  "score": "1-10的整数，新闻重要性评分（10=全球重大事件，7-8=行业大事，5-6=常规动态，4以下=琐碎）"
}}"""

    def call():
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{'role': 'user', 'content': prompt}],
            extra_body={'reasoning_effort': REASONING_EFFORT},
            temperature=0.3,
            max_tokens=4000,  # deepseek-flash 的思考也计入 max_tokens，预算须覆盖思维链+回答
        )
        return parse_json(extract_content(resp))

    try:
        result = call_with_retry(call, retries=2, description='AI 分析')
        if not isinstance(result, dict):
            raise ValueError(f'AI 返回的不是 JSON 对象: {type(result).__name__}')
    except Exception as e:
        logging.warning(f'  分析失败: {n["title"][:30]}... - {e}')
        return fallback_result(n)

    return {
        'title': n['title'],
        'source': n['source'],
        'url': n.get('url', ''),
        'category': result.get('category') or '其他',
        'summary': result.get('summary') or (n.get('content') or '')[:200],
        'key_data': result.get('key_data') or '无',
        'comment': result.get('comment') or '无',
        'score': _parse_score(result.get('score')),
    }


def analyze_all(news_list, kind):
    """并发分析新闻，结果顺序与输入一致"""
    if not news_list:
        return []
    client = get_ds_client()
    if client is None:
        logging.warning('  未配置 DEEPSEEK_API_KEY，直接使用 RSS 摘要')
        return [fallback_result(n) for n in news_list]

    results = [None] * len(news_list)
    with ThreadPoolExecutor(max_workers=min(AI_MAX_WORKERS, len(news_list))) as pool:
        futures = {pool.submit(analyze_article, client, n, kind): i
                   for i, n in enumerate(news_list)}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    logging.info(f'  {PIPELINE[kind]["label"]}新闻分析完成：{len(results)} 条')
    return results


def ai_overview(analyzed_news, label='今日'):
    """借鉴 TrendRadar 的 AI 分析简报：为一批新闻生成一段整体综述"""
    client = get_ds_client()
    if client is None or not analyzed_news:
        return ''
    lines = '\n'.join(f"- [{n.get('category', '其他')}] {n['title']}"
                      for n in analyzed_news[:60])
    prompt = f"""以下是{label}的新闻标题清单，请写一段3-5句话的{label}综述，概括{label}最值得关注的整体趋势和重点事件，不要逐条罗列新闻，面向想快速了解{label}要点的读者。

{lines}

只输出综述正文，不要其他内容。"""

    try:
        resp = call_with_retry(
            lambda: client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{'role': 'user', 'content': prompt}],
            extra_body={'reasoning_effort': REASONING_EFFORT},
                temperature=0.3,
                max_tokens=2000,
            ),
            description=f'{label}综述',
        )
        logging.info(f'  {label}综述生成完成')
        return extract_content(resp).strip()
    except Exception as e:
        logging.warning(f'  {label}综述生成失败，跳过: {e}')
        return ''


def run_pipeline(kind):
    """抓取 → 去重 → 新鲜度/重复/关键词过滤 → AI 筛选 → AI 分析"""
    conf = PIPELINE[kind]
    disabled = get_disabled_sources()
    feeds = dict(conf['feeds'])
    for name in list(feeds):
        # 已停用且未到复检时间的源跳过抓取
        if name in disabled and _days_since(disabled[name].get('last_attempt')) < SOURCE_PROBE_INTERVAL_DAYS:
            logging.info(f'  （源失效停用，跳过抓取：{name}）')
            del feeds[name]
    news, counts = fetch_all_news(feeds)
    update_source_health(counts)
    fetched = len(news)
    logging.info(f'  共获取 {fetched} 条{conf["label"]}新闻')
    if not news:
        return [], {'fetched': 0, 'selected': 0}

    news = dedupe_articles(news)
    news = filter_stale(news)
    news = filter_seen(news)
    news = filter_excluded(news)
    if FOCUS_KEYWORDS:
        news.sort(key=focus_hits, reverse=True)  # 命中关注关键词的排前面，引导 AI 优先选

    sampled = ai_select(news, kind, max_total=conf['max_total'])
    fetch_full_text(sampled)
    analyzed = analyze_all(sampled, kind)
    return analyzed, {'fetched': fetched, 'selected': len(analyzed)}


# ========== 周报：积累一周日报精选 → 周一汇总成周报 ==========
def week_label(dt):
    iso = dt.isocalendar()
    return f'{iso[0]}-W{iso[1]:02d}'


def week_file(dt):
    return STATE_DIR / f'week_{week_label(dt)}.json'


def record_week_items(items, now_dt):
    """把本次日报精选的文章追加到本周积累文件，供下周一的周报汇总"""
    if not items:
        return
    path = week_file(now_dt)
    try:
        records = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    except Exception:
        records = []
    seen_urls = {r.get('url') for r in records if r.get('url')}
    today = now_dt.strftime('%Y-%m-%d')
    for n in items:
        if n.get('url') and n['url'] in seen_urls:
            continue
        seen_urls.add(n.get('url'))
        records.append({
            'date': today,
            'title': n['title'],
            'source': n.get('source', ''),
            'url': n.get('url', ''),
            'category': n.get('category', '其他'),
            'summary': (n.get('summary') or '')[:200],
            'key_data': n.get('key_data', '无'),
            'comment': n.get('comment', '无'),
            'score': n.get('score', 0),
        })
    try:
        STATE_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
        logging.info(f'本周已积累 {len(records)} 条精选文章')
    except Exception as e:
        logging.warning(f'记录本周文章失败: {e}')


def balanced_weekly_sample(items, max_total):
    """周报 AI 筛选失败时的兜底：按分类轮询取样，保证每个分类都有代表"""
    by_cat = {}
    for n in items:
        by_cat.setdefault(n.get('category', '其他'), []).append(n)
    ordered_cats = [c for c in CATEGORY_ORDER if c in by_cat] + \
                   [c for c in by_cat if c not in CATEGORY_ORDER]
    out = []
    idx = 0
    while len(out) < max_total:
        added = False
        for cat in ordered_cats:
            its = by_cat[cat]
            if idx < len(its):
                out.append(its[idx])
                added = True
                if len(out) >= max_total:
                    break
        if not added:
            break
        idx += 1
    return out


def ai_select_weekly(items, max_total=WEEKLY_MAX_TOTAL):
    """AI 从一周积累的精选文章中挑出本周最重要的几条"""
    client = get_ds_client()
    if client is None:
        logging.warning('  未配置 DEEPSEEK_API_KEY，周报使用分类均匀取样')
        return balanced_weekly_sample(items, max_total)
    if not items:
        return []

    list_text = '\n'.join(
        f"[{i}] [{n.get('date', '')}] 【{n.get('source', '')}】{n['title']} — {(n.get('summary') or '')[:60]}"
        for i, n in enumerate(items)
    )
    prompt = f"""以下是过去一周每天日报精选出的 {len(items)} 条新闻，请从中挑出本周最重要的 {max_total} 条，用于生成周报。

挑选原则：
1. 优先重大事件和有持续影响的行业动态，而不是一周内的琐碎更新
2. 同一事件在一周内的多篇跟进报道只保留信息量最大的一条
3. 保持领域多样性：AI、芯片、消费电子、互联网、创投、国际科技、地缘政治、全球财经等都要有覆盖

新闻列表：
{list_text}

只输出一个 JSON 数组，包含选中文章的编号，例如 [0, 3, 5, 7]，不要输出其他内容。"""

    try:
        resp = call_with_retry(
            lambda: client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{'role': 'user', 'content': prompt}],
            extra_body={'reasoning_effort': REASONING_EFFORT},
                temperature=0.3,
                max_tokens=8000,
            ),
            description='周报 AI 筛选',
        )
        indices = parse_json(extract_content(resp))
        selected = [items[int(i)] for i in indices
                    if isinstance(i, (int, float)) and 0 <= int(i) < len(items)]
        logging.info(f'  周报 AI 筛选了 {len(selected)} 条')
        return selected[:max_total]
    except Exception as e:
        logging.warning(f'  周报 AI 筛选失败，降级使用分类取样: {e}')
        return balanced_weekly_sample(items, max_total)


def generate_weekly_markdown(selected, label, date_from, date_to, overview='', stats=None):
    """生成周报 Markdown：综述 + 统计头 + 按分类分组的全量详情"""
    md = f'# 每周热点汇报 - {label}（{date_from:%m.%d} ~ {date_to:%m.%d}）\n\n'

    if overview:
        md += f'> 📌 **本周综述**：{overview}\n\n'

    if stats:
        md += (f"**📊 本周数据**：日报共精选 {stats.get('fetched', 0)} 条 → "
               f"周报精选 {stats.get('selected', 0)} 条\n\n---\n\n")

    by_category = {}
    for n in selected:
        by_category.setdefault(n.get('category', '其他'), []).append(n)

    for cat in CATEGORY_ORDER:
        if cat not in by_category:
            continue
        md += f'## {cat}\n\n'
        for n in by_category[cat]:
            md += f'### {n["title"]}\n\n'
            if n.get('date'):
                md += f'**日期**：{n["date"]}　**来源**：{n.get("source", "")}\n\n'
            md += f'**摘要**：{n.get("summary", "")}\n\n'
            if n.get('key_data') and n.get('key_data') != '无':
                md += f'**关键数据**：{n["key_data"]}\n\n'
            if n.get('comment') and n.get('comment') != '无':
                md += f'**点评**：{n["comment"]}\n\n'
            if n.get('url'):
                md += f'[原文链接]({n["url"]})\n\n'
            md += '---\n\n'

    return md


def generate_weekly_report(now_dt):
    """汇总上周积累的日报精选，生成并发送周报（周一自动触发，也可 --weekly 手动触发）"""
    prev = now_dt - timedelta(days=7)  # 回退7天必落在上一个 ISO 周
    label = week_label(prev)
    path = week_file(prev)
    try:
        items = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    except Exception:
        items = []
    if not items:
        logging.info(f'上周（{label}）没有积累的日报数据，跳过周报')
        return

    logging.info(f'周报：上周（{label}）共积累 {len(items)} 条精选文章')
    date_from = prev - timedelta(days=prev.weekday())  # 上周周一
    date_to = date_from + timedelta(days=6)            # 上周周日

    selected = ai_select_weekly(items, max_total=WEEKLY_MAX_TOTAL)
    selected.sort(key=lambda n: n.get('score') or 0, reverse=True)  # 本周头条置顶
    overview = ai_overview(selected, label='本周')
    md = generate_weekly_markdown(selected, label, date_from, date_to, overview,
                                  stats={'fetched': len(items), 'selected': len(selected)})

    save_to_obsidian(md, f'weekly-{label}')
    link_to_daily_note(now_dt, f'weekly-{label}', '每周热点汇报')
    html = wrap_html(md_to_html(md), f'{label}（{date_from:%m.%d}~{date_to:%m.%d}）')
    send_email(md, html, f'每周热点汇报 - {label}')
    push_webhook(selected, overview)
    logging.info(f'===== 周报 {label} 完成 =====')


def dedupe_events(analyzed):
    """跨分类事件去重：同一事件的多篇报道只保留 AI 认为信息量最大的一条，失败时原样返回"""
    client = get_ds_client()
    if client is None or len(analyzed) < 2:
        return analyzed

    lines = '\n'.join(
        f"[{i}] ({n.get('category', '')}) {n['title']} — {(n.get('summary') or '')[:60]}"
        for i, n in enumerate(analyzed)
    )
    prompt = f"""以下是今天的 {len(analyzed)} 条新闻。请找出描述同一事件的多条报道（不同来源/角度报道同一件事），每组只保留信息量最大的一条。

新闻列表：
{lines}

只输出 JSON：{{"groups": [{{"keep": 0, "drop": [3, 7]}}]}}，keep 是保留条目的编号，drop 是同事件要丢弃的编号列表。没有重复事件则输出 {{"groups": []}}，不要输出其他内容。"""

    try:
        resp = call_with_retry(
            lambda: client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{'role': 'user', 'content': prompt}],
            extra_body={'reasoning_effort': REASONING_EFFORT},
                temperature=0.1,
                max_tokens=4000,
            ),
            description='事件去重',
        )
        data = parse_json(extract_content(resp))
        drop = set()
        for group in data.get('groups', []):
            keep = group.get('keep')
            if not (isinstance(keep, (int, float)) and 0 <= int(keep) < len(analyzed)):
                continue
            for d in group.get('drop', []):
                if isinstance(d, (int, float)) and 0 <= int(d) < len(analyzed) and int(d) != int(keep):
                    drop.add(int(d))
        if not drop:
            logging.info('  事件去重：无同事件报道')
            return analyzed
        out = [n for i, n in enumerate(analyzed) if i not in drop]
        logging.info(f'  事件去重：合并 {len(drop)} 条同事件报道，剩 {len(out)} 条')
        return out
    except Exception as e:
        logging.warning(f'  事件去重失败，跳过: {e}')
        return analyzed


# ========== 第3步：生成 Markdown ==========
def fetch_market_snapshot():
    """A股三大指数快照（东方财富公开接口，无需鉴权），失败返回空串"""
    try:
        url = ('https://push2.eastmoney.com/api/qt/ulist.np/get'
               '?secids=1.000001,0.399001,0.399006'  # 上证指数/深证成指/创业板指
               '&fields=f2,f3,f12,f14&fltt=2&invt=2')
        resp = call_with_retry(
            lambda: requests.get(url, timeout=10, headers=HEADERS),
            retries=2, description='行情接口',
        )
        rows = resp.json().get('data', {}).get('diff') or []
        lines = []
        for d in rows:
            name, close, pct = d.get('f14'), d.get('f2'), d.get('f3')
            if isinstance(close, (int, float)) and isinstance(pct, (int, float)):
                icon = '🔴' if pct > 0 else ('🟢' if pct < 0 else '⚪')
                lines.append(f'- {icon} {name}：{close:,.2f}（{pct:+.2f}%）')
        if not lines:
            logging.warning('  行情接口返回空数据，跳过市场快照')
            return ''
        return '\n'.join(lines)
    except Exception as e:
        logging.warning(f'  市场快照获取失败，跳过: {e}')
        return ''


def generate_markdown(analyzed_news, date_str, overview='', stats=None, market_md='',
                      disabled_sources=None):
    """生成 Markdown 内容：统计头 + AI 综述 + 按分类分组的正文"""
    md = f'# 每日热点汇报 - {date_str}\n\n'

    if overview:
        md += f'> 📌 **今日综述**：{overview}\n\n'

    if stats:
        counts = {}
        for n in analyzed_news:
            cat = n.get('category', '其他')
            counts[cat] = counts.get(cat, 0) + 1
        cat_summary = ' · '.join(f'{cat} {counts[cat]}' for cat in CATEGORY_ORDER if cat in counts)
        md += f"**📊 今日数据**：抓取 {stats.get('fetched', 0)} 条 → 精选 {stats.get('selected', 0)} 条"
        if cat_summary:
            md += f'（{cat_summary}）'
        md += '\n\n---\n\n'

    if market_md:
        md += '## 📈 市场快照\n\n' + market_md + '\n\n---\n\n'

    if disabled_sources:
        names = '、'.join(f'{name}（连续 {info.get("fail_streak", 0)} 天无数据）'
                          for name, info in disabled_sources)
        md += (f'> ⚠️ **源失效（已自动停用，每 {SOURCE_PROBE_INTERVAL_DAYS} 天自动复检）**'
               f'：{names}\n\n')

    # 按分类分组
    by_category = {}
    for item in analyzed_news:
        by_category.setdefault(item.get('category', '其他'), []).append(item)

    for cat in CATEGORY_ORDER:
        if cat not in by_category:
            continue
        items = sorted(by_category[cat], key=lambda x: x.get('score') or 0, reverse=True)
        md += f'## {cat}\n\n'

        # 每个分类前3条为重要新闻，其余为简略
        important = items[:3]
        regular = items[3:]

        for item in important:
            md += f'### {item["title"]}\n\n'
            md += f'**摘要**：{item["summary"]}\n\n'
            md += f'**关键数据**：{item["key_data"]}\n\n'
            md += f'**点评**：{item["comment"]}\n\n'
            if item.get('url'):
                md += f'[原文链接]({item["url"]})\n\n'
            md += '---\n\n'

        # 其他新闻
        if regular:
            md += '#### 其他动态\n\n'
            for item in regular:
                brief = item.get('summary', '')[:50]
                if item.get('url'):
                    md += f'- {brief} [{item["title"]}]({item["url"]})\n'
                else:
                    md += f'- {brief} {item["title"]}\n'
            md += '\n'

    return md


def save_to_obsidian(md_content, date_str):
    """保存到 Obsidian vault"""
    if not VAULT_PATH:
        logging.warning('未配置 OBSIDIAN_VAULT_PATH，跳过 Obsidian 保存')
        return None
    out_dir = Path(VAULT_PATH) / 'daily-briefing'
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / f'{date_str}.md'
    file_path.write_text(md_content, encoding='utf-8')
    logging.info(f'已保存到 {file_path}')
    return file_path


def link_to_daily_note(now_dt, target, link_text):
    """在 Obsidian 当天日志中添加指向 target 的 wikilink"""
    if not VAULT_PATH:
        return
    daily_dir = Path(VAULT_PATH) / '日志'
    daily_dir.mkdir(parents=True, exist_ok=True)

    dt = now_dt
    weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    weekday_en = weekdays[dt.weekday()]
    date_dot = dt.strftime('%Y.%m.%d')

    daily_file = daily_dir / f'{date_dot}.md'
    briefing_link = f'[[daily-briefing/{target}|{link_text}]]'

    if daily_file.exists():
        content = daily_file.read_text(encoding='utf-8')
        # 检查是否已有链接
        if briefing_link in content:
            logging.info('日志中已存在热点链接')
            return
        # 在标题行下方插入链接（按前缀匹配，兼容行尾有无空格）
        title_line = f'# 📅 {date_dot} {weekday_en}  [[日志]]'
        if title_line in content:
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if line.startswith(title_line):
                    lines.insert(i + 1, briefing_link)
                    break
            content = '\n'.join(lines)
        daily_file.write_text(content, encoding='utf-8')
        logging.info('已在日志中添加热点链接')
    else:
        # 用模板创建新的日志文件
        content = f'''---
date: {date_dot}
tags:
  - 日志
  - daily
---


# 📅 {date_dot} {weekday_en}  [[日志]]
{briefing_link}
## ✅ 今日完成
- [ ]

## 📝 记录


## 💡 收获 / 想法


## 🔜 后续计划
- [ ]
'''
        daily_file.write_text(content, encoding='utf-8')
        logging.info('已创建日志并添加热点链接')


# ========== 第4步：推送（邮件 + 群机器人 webhook） ==========
def md_to_html(md_content):
    """Markdown 转 HTML"""
    return markdown.markdown(md_content, extensions=['tables', 'fenced_code'])


def wrap_html(body_html, date_str):
    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }}
h1 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
h2 {{ color: #34495e; margin-top: 30px; }}
h3 {{ color: #7f8c8d; }}
a {{ color: #3498db; }}
strong {{ color: #2c3e50; }}
</style></head><body>
<h1>每日热点汇报 - {date_str}</h1>
{body_html}
</body></html>'''


def send_email(md_content, html_content, subject):
    """发送邮件（HTML 为主，Markdown 原文为纯文本兜底）"""
    if not all([SMTP_HOST, SMTP_USER, SMTP_AUTH, EMAIL_TO]):
        logging.warning('SMTP 配置不完整，跳过邮件发送')
        return
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = SMTP_USER
    msg['To'] = EMAIL_TO
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid()
    msg.attach(MIMEText(md_content, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    recipients = [r.strip() for r in EMAIL_TO.split(',') if r.strip()]

    def _deliver():
        # 465 走 SSL，587 走 STARTTLS；QQ 邮箱对海外 IP 可能在认证阶段断连
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        try:
            if SMTP_PORT != 465:
                server.starttls()
            server.login(SMTP_USER, SMTP_AUTH)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass

    call_with_retry(_deliver, retries=2, description='SMTP 发送')
    logging.info(f'邮件已发送：{subject}')


def build_push_text(analyzed_news, overview):
    """构建 webhook 推送的精简文本：综述 + 各分类要点标题"""
    lines = []
    if overview:
        lines.append(f'【综述】{overview}')
    by_category = {}
    for n in analyzed_news:
        by_category.setdefault(n.get('category', '其他'), []).append(n)
    for cat in CATEGORY_ORDER:
        items = by_category.get(cat)
        if not items:
            continue
        lines.append(f'\n【{cat}】')
        for n in items[:5]:
            lines.append(f'· {n["title"]}')
    return '\n'.join(lines)


def push_webhook(analyzed_news, overview):
    """推送到群机器人 webhook（飞书/钉钉/企业微信），未配置时静默跳过"""
    if not PUSH_WEBHOOK_URL:
        return
    text = build_push_text(analyzed_news, overview)
    max_chars = {'feishu': 5000, 'dingtalk': 4000, 'wecom': 600}.get(PUSH_WEBHOOK_TYPE, 4000)
    text = text[:max_chars]

    if PUSH_WEBHOOK_TYPE == 'feishu':
        payload = {'msg_type': 'text', 'content': {'text': text}}
    elif PUSH_WEBHOOK_TYPE == 'dingtalk':
        payload = {'msgtype': 'markdown', 'markdown': {'title': '每日热点汇报', 'text': text}}
    elif PUSH_WEBHOOK_TYPE == 'wecom':
        payload = {'msgtype': 'text', 'text': {'content': text}}
    else:
        logging.warning(f'未知的推送类型 {PUSH_WEBHOOK_TYPE}，跳过')
        return

    try:
        resp = call_with_retry(
            lambda: requests.post(PUSH_WEBHOOK_URL, json=payload, timeout=15),
            retries=2,
            description='webhook 推送',
        )
        resp.raise_for_status()
        logging.info(f'已推送到 {PUSH_WEBHOOK_TYPE}')
    except Exception as e:
        logging.warning(f'webhook 推送失败: {e}')


# ========== 主流程 ==========
def main(dry_run=False):
    today = datetime.now().strftime('%Y-%m-%d')
    logging.info(f'===== 每日热点汇报 {today} =====' + ('（dry-run）' if dry_run else ''))

    if not DEEPSEEK_KEY:
        logging.warning('未配置 DEEPSEEK_API_KEY，本次将不使用 AI 筛选/分析')

    # 1. 三条管道并行：抓取 → 过滤 → AI 筛选 → 分析
    logging.info(f'[1/3] 运行{len(PIPELINE)}条管道（{"、".join(c["label"] for c in PIPELINE.values())}）...')
    with ThreadPoolExecutor(max_workers=len(PIPELINE)) as pool:
        futures = {kind: pool.submit(run_pipeline, kind) for kind in PIPELINE}
    all_items = []
    fetched_total = selected_total = 0
    for kind, fut in futures.items():
        items, st = fut.result()
        all_items.extend(items)
        fetched_total += st['fetched']
        selected_total += st['selected']
    stats = {'fetched': fetched_total, 'selected': selected_total}

    # 2. 事件去重 + 综述 + 报告
    logging.info('[2/3] 生成报告...')
    all_items = dedupe_events(all_items)
    stats['selected'] = len(all_items)
    disabled_sources = sorted(get_disabled_sources().items(),
                              key=lambda kv: -kv[1].get('fail_streak', 0))
    market_md = fetch_market_snapshot()
    overview = ai_overview(all_items)
    md_content = generate_markdown(all_items, today, overview, stats, market_md,
                                   disabled_sources)

    if dry_run:
        # 干跑：完整跑通抓取/AI/报告，但不写 Obsidian、不发邮件、不记防重复状态
        out = LOG_DIR / f'dry_run_{today}.md'
        out.write_text(md_content, encoding='utf-8')
        logging.info(f'[dry-run] 报告已写入 {out}，跳过 Obsidian/邮件/webhook/状态记录')
        logging.info('===== 完成 =====')
        return

    save_to_obsidian(md_content, today)
    link_to_daily_note(datetime.now(), today, '每日热点汇报')

    # 3. 推送：邮件 + webhook（邮件失败会抛异常走退出码，此时不记录已推送，明天重试）
    logging.info('[3/3] 推送...')
    html = wrap_html(md_to_html(md_content), today)
    send_email(md_content, html, f'每日热点汇报 - {today}')
    push_webhook(all_items, overview)
    remember_seen([n.get('url') for n in all_items])

    # 积累本周数据，供下周一的周报汇总
    record_week_items(all_items, datetime.now())

    # 周一早上自动生成上周周报
    if datetime.now().weekday() == 0:
        logging.info('[周报] 今天是周一，自动生成上周周报...')
        try:
            generate_weekly_report(datetime.now())
        except Exception:
            logging.exception('周报生成失败（不影响已完成的日报）')

    logging.info('===== 完成 =====')


if __name__ == '__main__':
    try:
        if '--weekly' in sys.argv:
            generate_weekly_report(datetime.now())
        elif '--dry-run' in sys.argv:
            main(dry_run=True)
        else:
            main()
    except KeyboardInterrupt:
        logging.warning('已手动中断')
        sys.exit(130)
    except Exception:
        logging.exception('运行失败')
        sys.exit(1)
