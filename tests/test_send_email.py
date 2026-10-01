"""单元测试：只覆盖纯函数逻辑，不发网络请求、不调 AI、不写真实 state/。"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import send_email as s


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """隔离环境：state 指向临时目录，禁用 AI/邮件/Obsidian"""
    monkeypatch.setattr(s, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(s, 'SEEN_FILE', tmp_path / 'seen_urls.json')
    monkeypatch.setattr(s, 'SOURCE_HEALTH_FILE', tmp_path / 'source_health.json')
    monkeypatch.setattr(s, 'LLM_KEY', None)
    monkeypatch.setattr(s, '_llm_client', None)
    monkeypatch.setattr(s, 'VAULT_PATH', None)
    monkeypatch.setattr(s, 'SMTP_HOST', None)
    monkeypatch.setattr(s, 'PUSH_WEBHOOK_URL', '')
    return tmp_path


def item(title='T', url='u', category='前沿科技', score=5, source='A', **extra):
    base = {'title': title, 'source': source, 'url': url, 'category': category,
            'summary': '摘要', 'key_data': '无', 'comment': '无', 'score': score}
    base.update(extra)
    return base


# ---------- 解析类 ----------

def test_parse_feed_date():
    assert s.parse_feed_date('Mon, 29 Sep 2026 10:00:00 GMT').tzinfo is not None
    assert s.parse_feed_date('2026-09-29T10:00:00Z').tzinfo is not None
    assert s.parse_feed_date('2026-09-29T18:00:00+08:00') is not None
    assert s.parse_feed_date('') is None
    assert s.parse_feed_date('garbage') is None
    assert s.parse_feed_date(None) is None


def test_parse_json():
    assert s.parse_json('[0, 3]') == [0, 3]
    assert s.parse_json('```json\n[1]\n```') == [1]
    assert s.parse_json('{"a": 1}') == {'a': 1}
    with pytest.raises(json.JSONDecodeError):
        s.parse_json('not json')


def _fake_resp(content, finish='stop'):
    msg = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=msg)],
                           usage=SimpleNamespace(total_tokens=1))


def test_extract_content():
    assert s.extract_content(_fake_resp(' ok ')) == ' ok '
    with pytest.raises(RuntimeError, match='finish=stop'):
        s.extract_content(_fake_resp(''))


def test_parse_score():
    assert s._parse_score(8) == 8
    assert s._parse_score('9.6') == 9
    assert s._parse_score(99) == 10
    assert s._parse_score(-3) == 1
    assert s._parse_score(None) == 0
    assert s._parse_score('abc') == 0


# ---------- 过滤链 ----------

def test_dedupe_articles():
    news = [item('Same  Story', 'http://a/1'), item('same story', 'http://a/2'),
            item('Other', 'http://a/3'), item('NoUrl', '')]
    kept = s.dedupe_articles(news)
    assert [n['title'] for n in kept] == ['Same  Story', 'Other', 'NoUrl']


def test_filter_stale():
    now = datetime.now()
    news = [item('old', published_dt=now - timedelta(days=5)),
            item('new', published_dt=now - timedelta(hours=2)),
            item('nodate', published_dt=None)]
    kept = s.filter_stale(news)
    assert [n['title'] for n in kept] == ['new', 'nodate']


def test_filter_excluded():
    news = [item('限时优惠大促'), item('正常新闻'), item('氪星晚报')]
    assert [n['title'] for n in s.filter_excluded(news)] == ['正常新闻']


def test_focus_hits_and_sort(monkeypatch):
    monkeypatch.setattr(s, 'FOCUS_KEYWORDS', ['AI', '芯片'])
    monkey_items = [item('手机发布'), item('新款AI芯片量产'), item('AI应用上新')]
    assert [i['title'] for i in sorted(monkey_items, key=s.focus_hits, reverse=True)][0] == '新款AI芯片量产'
    assert s.focus_hits(item('无关新闻')) == 0


def test_balanced_sample_empty_and_small():
    assert s.balanced_sample([], 10) == []
    news = [item(f't{i}', source=f's{i % 2}') for i in range(12)]
    assert len(s.balanced_sample(news, 8)) == 8


def test_balanced_weekly_sample_round_robin(isolated):
    from collections import Counter
    pool = [item(f'{c}{j}', category=c) for c in ['前沿科技', '国际政治', '美股市场'] for j in range(4)]
    sel = s.balanced_weekly_sample(pool, 7)
    counts = Counter(x['category'] for x in sel)
    assert len(sel) == 7 and set(counts.values()) == {3, 2} and len(counts) == 3
    assert s.balanced_weekly_sample([], 15) == []


# ---------- 状态类（state/ 隔离到 tmp） ----------

def test_seen_roundtrip(isolated):
    s.remember_seen(['http://a/1', 'http://a/2'])
    news = [item('t1', 'http://a/1'), item('t3', 'http://a/3')]
    assert [n['url'] for n in s.filter_seen(news)] == ['http://a/3']


def test_seen_expiry(isolated):
    s.remember_seen(['http://old/x'])
    state = s.load_seen_urls()
    state['http://old/x'] = '2020-01-01'
    s.SEEN_FILE.write_text(json.dumps(state), encoding='utf-8')
    s.remember_seen(['http://new/y'])
    assert 'http://old/x' not in s.load_seen_urls()


def test_week_label():
    assert s.week_label(datetime(2026, 9, 30)) == '2026-W40'
    assert s.week_label(datetime(2026, 9, 23)) == '2026-W39'


def test_record_week_items_dedupe(isolated):
    now = datetime(2026, 9, 30)
    items = [item(f'T{i}', f'http://a/{i}') for i in range(1, 4)]
    s.record_week_items(items, now)
    s.record_week_items([item('dup', 'http://a/1')], now)
    records = json.loads((isolated / 'week_2026-W40.json').read_text(encoding='utf-8'))
    assert len(records) == 3 and all(r['date'] == '2026-09-30' for r in records)


def test_source_health(isolated):
    s.update_source_health({'OK源': 15, '坏源': 0}, '2026-09-23')
    s.update_source_health({'坏源': 0}, '2026-09-24')
    health = json.loads((isolated / 'source_health.json').read_text(encoding='utf-8'))
    assert 'OK源' not in health
    assert health['坏源'] == {'fail_streak': 2, 'last_attempt': '2026-09-24'}
    health['坏源']['fail_streak'] = 7
    (isolated / 'source_health.json').write_text(json.dumps(health), encoding='utf-8')
    assert '坏源' in s.get_disabled_sources()


def test_days_since():
    assert s._days_since(datetime.now().strftime('%Y-%m-%d')) == 0
    assert s._days_since('bad-date') == 0
    assert s._days_since((datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d')) in (14, 15)


# ---------- 报告生成 ----------

def test_generate_markdown_full(isolated):
    news = [item('低分', score=3), item('高分', score=9), item('无分', score=None),
            item('美股', category='美股市场', url='')]
    md = s.generate_markdown(news, '2026-09-30', overview='综述文字',
                             stats={'fetched': 100, 'selected': 4},
                             market_md='- 🟢 上证指数：3,829.52（-0.02%）',
                             disabled_sources=[('36氪（科技商业）', {'fail_streak': 9})])
    assert '> 📌 **今日综述**：综述文字' in md
    assert '抓取 100 条 → 精选 4 条' in md
    assert md.index('### 高分') < md.index('### 低分') < md.index('### 无分')
    assert '## 📈 市场快照' in md and '上证指数' in md
    assert '源失效（已自动停用' in md and '连续 9 天无数据' in md


def test_generate_markdown_minimal(isolated):
    md = s.generate_markdown([item('T', category='美股市场')], '2026-09-30')
    assert '市场快照' not in md and '源失效' not in md and '今日综述' not in md
    assert md.startswith('# 每日热点汇报 - 2026-09-30')


def test_generate_weekly_markdown(isolated):
    sel = [item('大事件', category='前沿科技', score=9, date='2026-09-24')]
    md = s.generate_weekly_markdown(sel, '2026-W39', datetime(2026, 9, 21), datetime(2026, 9, 27),
                                    overview='本周综述', stats={'fetched': 100, 'selected': 1})
    assert '# 每周热点汇报 - 2026-W39（09.21 ~ 09.27）' in md
    assert '> 📌 **本周综述**：本周综述' in md
    assert '**日期**：2026-09-24' in md


def test_build_push_text(isolated):
    news = [item('科技头条', category='前沿科技')]
    text = s.build_push_text(news, '综述')
    assert '【综述】综述' in text and '【前沿科技】' in text and '· 科技头条' in text


def test_weekly_fallback_without_ai(isolated):
    pool = [item(f'T{i}', f'http://a/{i}', category=c, date='2026-09-2x')
            for i, c in enumerate(['前沿科技', '国际政治', '美股市场', '互联网产业'])]
    sel = s.ai_select_weekly(pool, max_total=3)
    assert len(sel) == 3
    # 轮询顺序遵循 CATEGORY_ORDER：前沿科技 → 互联网产业 → 美股市场
    assert {x['category'] for x in sel} == {'前沿科技', '互联网产业', '美股市场'}
