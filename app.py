#!/usr/bin/env python3
"""PAL (Personal Activity Level): a dependency-free single-user quest and reward tracker."""
import calendar
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('DATA_DIR', str(ROOT / 'data')))
DB_PATH = DATA_DIR / 'questflow.db'
PORT = int(os.environ.get('PORT', '8765'))
SETUP_KEY = os.environ.get('APP_SETUP_KEY', '')
TZ_NAME = os.environ.get('TZ', 'Asia/Shanghai')
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    TZ = dt.timezone.utc
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', '0') == '1'
SESSION_AGE = 60 * 60 * 24 * 21
CADENCES = {'daily': '每日任务', 'weekly': '每周任务', 'monthly': '每月任务', 'custom': '自定义周期', 'once': '成长任务'}
VIEWS = {'daily', 'weekly', 'monthly', 'custom', 'once', 'shop', 'achievements', 'history', 'settings'}
CHEST_CADENCES = ('daily', 'weekly', 'monthly')
CHEST_LEVELS = (20, 50, 100)
DEFAULT_CHEST_REWARDS = {'daily': [5, 15, 30], 'weekly': [20, 50, 100], 'monthly': [100, 250, 500]}
DB_LOCK = threading.Lock()


def today():
    return dt.datetime.now(TZ).date()


def timestamp():
    return dt.datetime.now(TZ).isoformat(timespec='seconds')


def e(value):
    return html.escape(str('' if value is None else value), quote=True)


def number(value, default, low=0, high=100000):
    try:
        x = int(value)
        return x if low <= x <= high else default
    except (TypeError, ValueError):
        return default


def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    con.execute('PRAGMA busy_timeout = 15000')
    return con


@contextmanager
def opened_db():
    con = db()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


@contextmanager
def atomic():
    con = db()
    try:
        con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with opened_db() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=FULL')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS owner (
          id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL,
          salt TEXT NOT NULL, pwdhash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
          key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, first_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          cadence TEXT NOT NULL CHECK(cadence IN ('daily','weekly','monthly','custom','once')),
          target INTEGER NOT NULL CHECK(target BETWEEN 1 AND 10000),
          coins INTEGER NOT NULL CHECK(coins BETWEEN 0 AND 100000),
          energy INTEGER NOT NULL CHECK(energy BETWEEN 0 AND 100000),
          category TEXT NOT NULL DEFAULT '生活',
          custom_days INTEGER NOT NULL DEFAULT 7,
          anchor_date TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS progress (
          task_id INTEGER NOT NULL REFERENCES tasks(id),
          period_key TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(task_id,period_key)
        );
        CREATE TABLE IF NOT EXISTS task_claims (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NOT NULL REFERENCES tasks(id),
          period_key TEXT NOT NULL, coins INTEGER NOT NULL, energy INTEGER NOT NULL,
          claimed_at TEXT NOT NULL, UNIQUE(task_id,period_key)
        );
        CREATE TABLE IF NOT EXISTS chest_claims (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cadence TEXT NOT NULL, period_key TEXT NOT NULL,
          threshold INTEGER NOT NULL, coins INTEGER NOT NULL,
          claimed_at TEXT NOT NULL, UNIQUE(cadence,period_key,threshold)
        );
        CREATE TABLE IF NOT EXISTS shop (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          price INTEGER NOT NULL CHECK(price BETWEEN 1 AND 1000000),
          enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS redemptions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          item_id INTEGER NOT NULL REFERENCES shop(id),
          title TEXT NOT NULL, price INTEGER NOT NULL, redeemed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ledger (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          delta INTEGER NOT NULL, kind TEXT NOT NULL,
          detail TEXT NOT NULL, occurred_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        ''')
        for cadence, prizes in DEFAULT_CHEST_REWARDS.items():
            for pct, prize in zip(CHEST_LEVELS, prizes):
                con.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)', (f'chest:{cadence}:{pct}', str(prize)))


def make_password(pwd):
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac('sha256', pwd.encode(), salt, 320000)
    return salt.hex(), digest.hex()


def check_password(pwd, salt, digest):
    candidate = hashlib.pbkdf2_hmac('sha256', pwd.encode(), bytes.fromhex(salt), 320000)
    return hmac.compare_digest(candidate, bytes.fromhex(digest))


def period(task, day=None):
    day = day or today()
    cadence = task['cadence']
    if cadence == 'once':
        return f"once:{task['id']}", '长期目标，不自动重置'
    if cadence == 'daily':
        return f'daily:{day.isoformat()}', day.strftime('%Y-%m-%d')
    if cadence == 'weekly':
        start = day - dt.timedelta(days=day.weekday())
        end = start + dt.timedelta(days=6)
        return f'weekly:{start.isoformat()}', f'{start:%m/%d} — {end:%m/%d}'
    if cadence == 'monthly':
        return f'monthly:{day:%Y-%m}', f'{day:%Y 年 %m 月}'
    anchor = dt.date.fromisoformat(task['anchor_date'])
    days = int(task['custom_days'])
    index = max(0, (day - anchor).days // days)
    start = anchor + dt.timedelta(days=index * days)
    end = start + dt.timedelta(days=days - 1)
    return f'custom:{start.isoformat()}:{days}', f'{start:%m/%d} — {end:%m/%d}'


def add_ledger(con, delta, kind, detail):
    con.execute('INSERT INTO ledger(delta,kind,detail,occurred_at) VALUES (?,?,?,?)', (delta, kind, detail, timestamp()))


def balance(con):
    return con.execute('SELECT COALESCE(SUM(delta),0) AS b FROM ledger').fetchone()['b']


def current_task_rows(con, cadence):
    tasks = con.execute('SELECT * FROM tasks WHERE enabled=1 AND cadence=? ORDER BY id DESC', (cadence,)).fetchall()
    result = []
    for task in tasks:
        key, text = period(task)
        p = con.execute('SELECT count FROM progress WHERE task_id=? AND period_key=?', (task['id'], key)).fetchone()
        claim = con.execute('SELECT * FROM task_claims WHERE task_id=? AND period_key=?', (task['id'], key)).fetchone()
        progress = p['count'] if p else 0
        result.append({'task': dict(task), 'period_key': key, 'period_text': text,
                       'progress': progress, 'claimed': bool(claim),
                       'pct': min(100, round(progress * 100 / task['target']))})
    return result


def current_energy(rows):
    return (sum(r['task']['energy'] for r in rows if r['claimed']),
            sum(r['task']['energy'] for r in rows))


def period_key_for_tab(tab):
    fake = {'cadence': tab, 'id': 0}
    return period(fake)[0]


def period_scores(con, cadence='weekly'):
    entries = con.execute("SELECT energy,claimed_at FROM task_claims ORDER BY claimed_at DESC LIMIT 50000").fetchall()
    scores = defaultdict(int)
    for claim in entries:
        try:
            d = dt.date.fromisoformat(claim['claimed_at'][:10])
            key = (d - dt.timedelta(days=d.weekday())).isoformat() if cadence == 'weekly' else d.strftime('%Y-%m')
            scores[key] += claim['energy']
        except ValueError:
            continue
    d = today()
    current_key = (d - dt.timedelta(days=d.weekday())).isoformat() if cadence == 'weekly' else d.strftime('%Y-%m')
    scores.setdefault(current_key, 0)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:10]


def day_streak(con):
    day_list = con.execute("SELECT DISTINCT substr(claimed_at,1,10) d FROM task_claims ORDER BY d DESC").fetchall()
    days = {x['d'] for x in day_list}
    current = today()
    if current.isoformat() not in days:
        current -= dt.timedelta(days=1)
    streak = 0
    while current.isoformat() in days:
        streak += 1
        current -= dt.timedelta(days=1)
    return streak


def achievements(con):
    claims = con.execute('SELECT COUNT(*) n FROM task_claims').fetchone()['n']
    chests = con.execute('SELECT COUNT(*) n FROM chest_claims').fetchone()['n']
    purchased = con.execute('SELECT COUNT(*) n FROM redemptions').fetchone()['n']
    coins_earned = con.execute('SELECT COALESCE(SUM(delta),0) n FROM ledger WHERE delta>0').fetchone()['n']
    streak = day_streak(con)
    return [
        ('🌱', '任务初体验', '成功领取 1 次任务奖励', claims >= 1, f'{min(claims,1)}/1'),
        ('🏹', '挑战达人', '累计领取 10 次任务奖励', claims >= 10, f'{min(claims,10)}/10'),
        ('🏆', '百炼成钢', '累计领取 50 次任务奖励', claims >= 50, f'{min(claims,50)}/50'),
        ('🔥', '三日连胜', '连续 3 天领取任务奖励', streak >= 3, f'{min(streak,3)}/3'),
        ('⚡', '一周之星', '连续 7 天领取任务奖励', streak >= 7, f'{min(streak,7)}/7'),
        ('🎁', '宝箱猎人', '领取至少 1 个活跃度宝箱', chests >= 1, f'{min(chests,1)}/1'),
        ('🛒', '奖励收藏家', '至少兑换过 1 件奖励', purchased >= 1, f'{min(purchased,1)}/1'),
        ('💰', '金币达人', '累计赚取 1000 金币', coins_earned >= 1000, f'{min(coins_earned,1000)}/1000'),
    ]


# ---- Web UI: all dynamic text escaped by e() ----
def layout(body, view, coin_balance, owner, csrf, message='', authed=True):
    nav = [('daily','☀','每日'),('weekly','🗓','每周'),('monthly','🌙','每月'),
           ('custom','⏳','自定义'),('once','🚀','成长'),('shop','🎁','奖励商店'),
           ('achievements','🏆','成就'),('history','📊','排行榜'),('settings','⚙','设置')]
    links = ''.join(f'<a class="navlink {"selected" if view==key else ""}" href="/?view={key}"><span>{emoji}</span>{name}</a>' for key,emoji,name in nav)
    sidebar = (f'<aside class="sidebar"><a class="brand" href="/">✦ <span>PAL<small>Personal Activity Level</small></span></a>'
               f'<div class="sidebar-label">任务与奖励</div><nav>{links}</nav>'
               f'<div class="sidebar-bottom"><div class="wallet">🪙 当前金币<strong>{coin_balance:,}</strong></div>'
               f'<span class="muted">账户：{e(owner)}</span><form method="post" action="/logout">{csrf_input(csrf)}<button class="linkbtn" type="submit">退出登录</button></form></div></aside>') if authed else ''
    mobile = '<nav class="mobile-nav">' + ''.join(f'<a class="{"selected" if view==key else ""}" href="/?view={key}" title="{name}"><span>{emoji}</span><small>{name}</small></a>' for key,emoji,name in nav) + '</nav>' if authed else ''
    alert = f'<div class="notice">{e(message)}</div>' if message else ''
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#f6f7fc"><meta name="description" content="PAL · 私有化个人活跃度与奖励管理"><link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/static/icon.svg" type="image/svg+xml"><link rel="stylesheet" href="/static/style.css"><title>PAL · Personal Activity Level</title></head><body>
    <div class="app-layout">{sidebar}<main class="main">{alert}{body}</main></div>{mobile}
    <script src="/static/app.js" defer></script></body></html>'''


def csrf_input(token):
    return f'<input type="hidden" name="csrf" value="{e(token)}">'


def form_button(action, csrf, title, klass='btn', hidden='', disabled=False):
    return f'<form method="post" action="{e(action)}">{csrf_input(csrf)}{hidden}<button class="{klass}" type="submit" {"disabled" if disabled else ""}>{e(title)}</button></form>'


def select_field(name, current):
    return '<select name="cadence" id="cadence-select" required>' + ''.join(f'<option value="{key}" {"selected" if key==current else ""}>{e(label)}</option>' for key,label in CADENCES.items()) + '</select>'


def task_form(csrf, row=None, cadence='daily'):
    task = row or {'title':'','description':'','cadence':cadence,'category':'生活','target':1,'coins':10,'energy':10,'custom_days':7,'anchor_date':today().isoformat()}
    action = f'/tasks/{task["id"]}/edit' if row else '/tasks/create'
    head = '编辑任务' if row else '创建新任务'
    return f'''<form method="post" action="{action}" class="edit-form">{csrf_input(csrf)}
      <h3>{head}</h3><label>任务名称<input name="title" maxlength="80" required value="{e(task['title'])}" placeholder="例如：阅读 30 分钟"></label>
      <div class="form-grid"><label>任务类型{select_field('cadence',task['cadence'])}</label><label>分类<input name="category" maxlength="20" value="{e(task['category'])}" placeholder="生活"></label>
      <label>目标次数<input name="target" type="number" min="1" max="10000" required value="{task['target']}"></label>
      <label>任务金币<input name="coins" type="number" min="0" max="100000" required value="{task['coins']}"></label>
      <label>任务活跃度<input name="energy" type="number" min="0" max="100000" required value="{task['energy']}"></label>
      <label>自定义周期（天）<input name="custom_days" type="number" min="1" max="3650" value="{task['custom_days']}"></label>
      <label>自定义周期起始日<input name="anchor_date" type="date" value="{e(task['anchor_date'])}" required></label></div>
      <label>备注（可选）<textarea name="description" maxlength="500" rows="2" placeholder="写下执行标准，方便统计">{e(task['description'])}</textarea></label>
      <button type="submit" class="btn primary">{head}</button></form>'''


def task_card(row, csrf):
    t = row['task']; task_id=t['id']; p=row['progress']; target=t['target']
    if row['claimed']:
        action = '<span class="pill done">✓ 已领奖</span>'
    elif p >= target:
        action = form_button(f'/tasks/{task_id}/claim',csrf,'🎁 领取奖励','btn reward')
    else:
        action = '<span class="pill pending">进行中</span>'
    progress_buttons = '' if row['claimed'] else (
        '<div class="stepper">' + form_button(f'/tasks/{task_id}/progress',csrf,'−','step', '<input type="hidden" name="delta" value="-1">', p==0) +
        form_button(f'/tasks/{task_id}/progress',csrf,'+1','step step-plus','<input type="hidden" name="delta" value="1">',p>=target) + '</div>')
    return f'''<article class="quest-card"><div class="quest-top"><div><span class="category">{e(t['category'])}</span><h3>{e(t['title'])}</h3>
      <p class="description">{e(t['description']) if t['description'] else '达成目标即可领取任务奖励'}</p></div>{action}</div>
      <div class="quest-meta"><span>⚡ +{t['energy']} 活跃度</span><span>🪙 +{t['coins']} 金币</span><span>↻ {e(row['period_text'])}</span></div>
      <div class="progress-heading"><span>完成进度</span><strong>{p} / {target} <em>· {row['pct']}%</em></strong></div>
      <div class="bar"><div style="width:{row['pct']}%"></div></div>
      <div class="quest-actions">{progress_buttons}<details class="task-editor"><summary>编辑 / 归档</summary>{task_form(csrf,t)}
        {form_button(f'/tasks/{task_id}/archive',csrf,'归档任务（保留历史）','btn danger')}</details></div></article>'''


def chest_panel(con, cadence, rows, csrf):
    energy, total = current_energy(rows)
    pct = min(100, round(energy * 100 / total)) if total else 0
    cycle = period_key_for_tab(cadence)
    claims = {r['threshold'] for r in con.execute('SELECT threshold FROM chest_claims WHERE cadence=? AND period_key=?',(cadence,cycle))}
    cards = ''
    for threshold in CHEST_LEVELS:
        prize = int(con.execute('SELECT value FROM settings WHERE key=?',(f'chest:{cadence}:{threshold}',)).fetchone()['value'])
        claimed = threshold in claims
        available = total > 0 and pct >= threshold
        state = 'chest-open' if claimed else 'chest-ready' if available else 'chest-locked'
        button = ('<div class="chest-label">✓ 已领取</div>' if claimed else
                  form_button(f'/chests/{cadence}/{threshold}/claim', csrf, '领取宝箱', 'btn chest-btn') if available else
                  '<div class="chest-label">🔒 未解锁</div>')
        cards += f'<div class="chest {state}"><div class="chest-icon">{"🎁" if not claimed else "✅"}</div><strong>{threshold}%</strong><small>🪙 +{prize}</small>{button}</div>'
    return f'''<section class="panel chest-panel"><div class="panel-heading"><div><h2>🎁 活跃度宝箱</h2><p>任务完成后领取活跃度，到达进度节点解锁宝箱</p></div><div class="energy-badge">⚡ {energy} / {total}</div></div>
      <div class="bar large"><div style="width:{pct}%"></div></div><div class="chest-grid">{cards}</div></section>'''


def overview(con):
    active = con.execute('SELECT COUNT(*) n FROM tasks WHERE enabled=1').fetchone()['n']
    complete = con.execute('SELECT COUNT(*) n FROM task_claims').fetchone()['n']
    earned = con.execute('SELECT COALESCE(SUM(delta),0) n FROM ledger WHERE delta>0').fetchone()['n']
    streak = day_streak(con)
    return f'''<div class="stats"><div class="stat"><span>当前活跃任务</span><strong>{active}</strong><small>跨全部任务周期</small></div>
     <div class="stat"><span>累计领奖</span><strong>{complete}</strong><small>已领取的任务奖励</small></div>
     <div class="stat"><span>累计赚取金币</span><strong>{earned}</strong><small>不含支出</small></div>
     <div class="stat"><span>连续活跃</span><strong>{streak} <i>天</i></strong><small>以领取任务奖励计</small></div></div>'''


def page_tasks(con, cadence, csrf):
    rows=current_task_rows(con,cadence)
    progress = round(sum(r['pct'] for r in rows)/len(rows)) if rows else 0
    task_html = ''.join(task_card(row, csrf) for row in rows) if rows else '''<div class="empty">🌱<h3>这里还没有任务</h3><p>创建第一条任务，给每天的行动一个可以看见的奖励。</p></div>'''
    return f'''<div class="page-head"><div><div class="eyebrow">PERSONAL ACTIVITY LEVEL</div><h1>{CADENCES[cadence]}</h1>
      <p>将计划拆成可计数的行动，完成后获得金币与活跃度。</p></div><div class="ring" style="--percent:{progress}"><span>{progress}%<small>任务进度</small></span></div></div>
      {overview(con)}{chest_panel(con,cadence,rows,csrf) if cadence in CHEST_CADENCES else ''}
      <div class="section-head"><h2>📋 任务清单 <span class="count">{len(rows)}</span></h2><button class="btn primary" data-toggle="new-task">＋ 新增任务</button></div>
      <div id="new-task" class="panel create-panel" hidden>{task_form(csrf,cadence=cadence)}</div>
      <div class="tasks">{task_html}</div>'''


def page_shop(con, csrf):
    items=con.execute('SELECT * FROM shop WHERE enabled=1 ORDER BY id DESC').fetchall()
    cards=[]
    b=balance(con)
    for item in items:
        can=b>=item['price']
        button=form_button(f'/shop/{item["id"]}/redeem',csrf,'兑换奖励' if can else '金币不足','btn reward' if can else 'btn disabled', disabled=not can)
        editor=f'''<details class="task-editor"><summary>编辑 / 下架</summary><form class="edit-form" method="post" action="/shop/{item['id']}/edit">{csrf_input(csrf)}
         <label>名称<input name="title" maxlength="80" required value="{e(item['title'])}"></label><label>说明<input name="description" maxlength="200" value="{e(item['description'])}"></label>
         <label>所需金币<input type="number" name="price" min="1" max="1000000" value="{item['price']}"></label><button class="btn primary">保存</button></form>
         {form_button(f'/shop/{item["id"]}/archive',csrf,'下架奖励','btn danger')}</details>'''
        cards.append(f'''<article class="shop-card"><div class="shop-icon">🎟️</div><h3>{e(item['title'])}</h3><p>{e(item['description'] or '奖励自己完成计划的努力')}</p>
        <div class="shop-bottom"><strong>🪙 {item['price']}</strong>{button}</div>{editor}</article>''')
    form=f'''<form class="edit-form" method="post" action="/shop/create">{csrf_input(csrf)}<h3>新增一个你真正想要的奖励</h3>
      <label>奖励名称<input name="title" maxlength="80" required placeholder="例如：一部电影的时间"></label>
      <label>奖励说明<input name="description" maxlength="200" placeholder="例如：周末完整看一部喜欢的电影"></label>
      <label>金币价格<input name="price" type="number" min="1" max="1000000" value="100" required></label><button class="btn primary">保存奖励</button></form>'''
    history=con.execute('SELECT * FROM redemptions ORDER BY id DESC LIMIT 8').fetchall()
    recent=''.join(f'<li>🎟️ {e(x["title"])} <span>−{x["price"]} 🪙 · {e(x["redeemed_at"][:10])}</span></li>' for x in history) or '<li class="muted">暂时还没有兑换记录</li>'
    return f'''<div class="page-head"><div><div class="eyebrow">REWARD SHOP</div><h1>奖励商店</h1><p>把你真正在意的奖励设为目标，让每一次任务完成都更有价值。</p></div>
       <div class="coin-hero">🪙<strong>{b}</strong><small>当前金币</small></div></div>
       <div class="section-head"><h2>🎁 可兑换奖励</h2><button class="btn primary" data-toggle="new-reward">＋ 新增奖励</button></div>
       <div id="new-reward" class="panel create-panel" hidden>{form}</div>
       <div class="shop-grid">{''.join(cards) if cards else '<div class="empty">还没有奖励，先添加一件喜欢的事物吧。</div>'}</div>
       <section class="panel"><h2>🧾 最近兑换记录</h2><ul class="history-list">{recent}</ul></section>'''


def page_achievements(con):
    ach=achievements(con); unlocked=sum(a[3] for a in ach)
    cards=''.join(f'''<article class="achievement {"unlocked" if a[3] else "locked"}"><div class="medal">{a[0]}</div><h3>{e(a[1])}</h3><p>{e(a[2])}</p><span>{'✓ 已解锁' if a[3] else a[4]}</span></article>''' for a in ach)
    return f'''<div class="page-head"><div><div class="eyebrow">ACHIEVEMENTS</div><h1>成就殿堂</h1><p>将坚持与成长变成可见的里程碑。成就会按实际记录自动更新。</p></div><div class="coin-hero">🏆<strong>{unlocked}/{len(ach)}</strong><small>已解锁</small></div></div><div class="ach-grid">{cards}</div>'''


def page_history(con):
    def ranking(cadence):
        rows = period_scores(con,cadence)
        best = max((n for _,n in rows),default=1) or 1
        return ''.join(f'''<div class="leader-row"><span class="rank">#{i+1}</span><span class="leader-date">{e(day)}</span><div class="leader-track"><div style="width:{round(score*100/best)}%"></div></div><strong>⚡ {score}</strong></div>''' for i,(day,score) in enumerate(rows))
    weeks=ranking('weekly')
    months=ranking('monthly')
    log=con.execute('SELECT * FROM ledger ORDER BY id DESC LIMIT 30').fetchall()
    events=''.join(f'<li><span>{e(r["detail"])}</span><strong class="{"positive" if r["delta"]>=0 else "negative"}">{"+" if r["delta"]>=0 else ""}{r["delta"]}</strong><small>{e(r["occurred_at"][:16].replace("T"," "))}</small></li>' for r in log) or '<li>暂无交易记录</li>'
    return f'''<div class="page-head"><div><div class="eyebrow">PERSONAL LEADERBOARD</div><h1>个人排行榜</h1><p>将自己的每一周进行排名。单人版不提供虚构的多人排名。</p></div></div>
     <section class="panel"><h2>🏅 历史周活跃度排名</h2><p class="muted">以任务奖励的领取日期统计每周活跃度；本周也参与排名。</p>{weeks}</section>
     <section class="panel"><h2>🌙 历史月活跃度排名</h2><p class="muted">与过去的自己比较；本月也参与排名。</p>{months}</section>
     <section class="panel"><h2>🧾 最近金币流水</h2><ul class="history-list ledger">{events}</ul></section>'''


def page_settings(con, csrf):
    sets={r['key']:r['value'] for r in con.execute('SELECT key,value FROM settings')}
    cards=''
    for cadence in CHEST_CADENCES:
        inputs=''.join(f'<label>{p}% 奖励金币<input type="number" name="p{p}" min="0" max="100000" required value="{e(sets.get(f"chest:{cadence}:{p}","0"))}"></label>' for p in CHEST_LEVELS)
        cards+=f'''<form class="panel edit-form settings-card" method="post" action="/settings/chest/{cadence}">{csrf_input(csrf)}<h3>{CADENCES[cadence]} · 宝箱设置</h3><div class="form-grid">{inputs}</div><button class="btn primary">保存档位</button></form>'''
    change=f'''<form method="post" action="/settings/password" class="panel edit-form"><h3>🔐 修改管理员密码</h3>{csrf_input(csrf)}
    <label>原密码<input name="old_password" type="password" required autocomplete="current-password"></label>
    <label>新密码（至少 10 位）<input name="new_password" type="password" minlength="10" required autocomplete="new-password"></label>
    <button class="btn primary">修改密码</button></form>'''
    return f'''<div class="page-head"><div><div class="eyebrow">PREFERENCES</div><h1>系统设置</h1><p>调整每日、每周、每月的活跃度宝箱奖励，保留自己的节奏。</p></div></div>
    <div class="settings-grid">{cards}{change}</div><section class="panel"><h2>💾 数据与访问</h2>
    <p>数据存储为 SQLite 数据库，保存在 Docker 的持久化数据卷。可使用附带的 backup.py 做在线一致性备份。</p>
    <p>系统时区：<strong>{e(TZ_NAME)}</strong>。如需远程访问，请使用 Tailscale 或 HTTPS 反向代理；勿将明文 HTTP 端口直接暴露到公网。</p>
    <p>归档的任务和下架的奖励不会删除历史记录。</p></section>'''


def gate(title, csrf='', mode='login', err=''):
    fields = (f'<label>用户名<input name="username" maxlength="40" value="owner" required autocomplete="username"></label>' if mode=='setup' else '')
    token = f'<label>首次安装密钥<input name="setup_key" type="password" required placeholder="对应 APP_SETUP_KEY"></label>' if mode=='setup' else ''
    return f'''<div class="auth-wrap"><div class="auth-card"><div class="auth-logo">✦</div><div class="eyebrow">PAL / PERSONAL ACTIVITY LEVEL</div><h1>{e(title)}</h1>
    <p>{'首次部署：建立自己的私人任务空间' if mode=='setup' else '欢迎回来，继续积累你的成长积分。'}</p>
    <form method="post" action="/{mode}" class="edit-form">{csrf_input(csrf)}{fields}{token}
      <label>密码<input type="password" name="password" required {'minlength="10" autocomplete="new-password"' if mode=='setup' else 'autocomplete="current-password"'}></label>
      <button class="btn primary full">{'初始化账户' if mode=='setup' else '登录 PAL'}</button></form></div></div>'''


class Web(BaseHTTPRequestHandler):
    server_version = 'PAL/0.2'
    sys_version = ''

    def log_message(self, fmt, *args):
        print('%s [%s] %s' % (self.client_address[0], self.log_date_time_string(),fmt % args),flush=True)

    def send_html(self, document, status=200, headers=None):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if headers:
            for k,v in headers.items(): self.send_header(k,v)
        data=document.encode('utf-8')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, url, cookie=None):
        self.send_response(303)
        self.send_header('Location',url)
        self.send_header('Cache-Control','no-store')
        if cookie: self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length','0')
        self.end_headers()

    def error_page(self, status, msg):
        self.send_html(layout('<div class="panel"><h2>操作未完成</h2><p>'+e(msg)+'</p><p><a href="/">返回首页</a></p></div>','daily',0,'','',authed=False),status)

    def parse_post(self):
        if number(self.headers.get('Content-Length'),-1,0,20000)<0:
            return None
        length=int(self.headers.get('Content-Length'))
        if 'application/x-www-form-urlencoded' not in self.headers.get('Content-Type',''):
            return None
        raw=self.rfile.read(length).decode('utf-8','replace')
        return {key:val[0] if val else '' for key,val in parse_qs(raw,keep_blank_values=True,max_num_fields=30).items()}

    def get_session(self, con):
        cookie=SimpleCookie()
        try: cookie.load(self.headers.get('Cookie',''))
        except Exception: return None
        if 'qfsession' not in cookie: return None
        value=cookie['qfsession'].value
        if not re.fullmatch(r'[a-f0-9]{64}',value): return None
        digest=hashlib.sha256(value.encode()).hexdigest()
        return con.execute('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?',(digest,int(time.time()))).fetchone()

    def session_cookie(self, token, max_age=SESSION_AGE):
        s=f'qfsession={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}'
        if COOKIE_SECURE: s+='; Secure'
        return s

    def has_owner(self, con):
        return con.execute('SELECT id FROM owner WHERE id=1').fetchone() is not None

    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/health':
            return self.send_html('ok')
        if path.startswith('/static/') and path.split('/')[-1] in {'style.css','app.js','icon.svg'}:
            name=path.split('/')[-1]
            f=ROOT/'static'/name
            ctype={'style.css':'text/css','app.js':'application/javascript','icon.svg':'image/svg+xml'}[name]
            return self.static(f,ctype)
        if path=='/manifest.webmanifest':
            return self.static(ROOT/'manifest.webmanifest','application/manifest+json')
        if path not in {'/','/login','/setup'}:
            return self.error_page(404,'页面不存在')
        with opened_db() as con:
            exists=self.has_owner(con)
            sess=self.get_session(con)
            if not exists:
                return self.send_html(layout(gate('创建你的私人账户',mode='setup'),'daily',0,'','',authed=False))
            if not sess:
                return self.send_html(layout(gate('登录'),'daily',0,'','',authed=False))
            owner=con.execute('SELECT username FROM owner WHERE id=1').fetchone()['username']
            qs=parse_qs(urlparse(self.path).query)
            view=qs.get('view',['daily'])[0]
            if view not in VIEWS: view='daily'
            csrf=sess['csrf']
            pages={'shop':lambda:page_shop(con,csrf),'achievements':lambda:page_achievements(con),
                   'history':lambda:page_history(con),'settings':lambda:page_settings(con,csrf)}
            body = pages[view]() if view in pages else page_tasks(con,view,csrf)
            msg=qs.get('msg',[''])[0][:140]
            return self.send_html(layout(body,view,balance(con),owner,csrf,msg))

    def static(self, file, ctype):
        try: data=file.read_bytes()
        except OSError: return self.error_page(404,'静态文件不存在')
        self.send_response(200)
        self.send_header('Content-Type',ctype+'; charset=utf-8')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','public, max-age=3600')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers();self.wfile.write(data)

    def do_POST(self):
        path=urlparse(self.path).path
        try: form=self.parse_post()
        except Exception: return self.error_page(400,'表单格式不正确')
        if form is None: return self.error_page(400,'仅允许大小不超过 20KB 的表单')
        try:
            if path=='/setup': return self.setup_account(form)
            if path=='/login': return self.login(form)
            with opened_db() as con:
                sess=self.get_session(con)
                if not sess: return self.error_page(401,'请先登录')
                if not hmac.compare_digest(form.get('csrf',''),sess['csrf']):
                    return self.error_page(403,'页面已过期或 CSRF 校验失败，请刷新后重试')
                if path=='/logout':
                    with atomic() as tx: tx.execute('DELETE FROM sessions WHERE token_hash=?',(sess['token_hash'],))
                    return self.redirect('/',self.session_cookie('',0))
            message,view=self.handle_action(path,form)
            return self.redirect('/?view='+view+'&msg='+__import__('urllib.parse',fromlist=['quote']).quote(message))
        except ValueError as exc:
            return self.error_page(400,str(exc))
        except sqlite3.IntegrityError:
            return self.error_page(409,'该奖励已经领取，或数据存在冲突；请返回刷新')
        except Exception as exc:
            self.log_message('ERROR: %r',exc)
            return self.error_page(500,'服务器处理失败，请查看 Docker 日志')

    def create_session(self, con):
        token=secrets.token_hex(32)
        con.execute('INSERT INTO sessions(token_hash,csrf,expires_at) VALUES (?,?,?)',
                    (hashlib.sha256(token.encode()).hexdigest(),secrets.token_hex(24),int(time.time())+SESSION_AGE))
        return token

    def setup_account(self,form):
        if not SETUP_KEY: return self.error_page(503,'未配置 APP_SETUP_KEY，拒绝初始化账户')
        if not hmac.compare_digest(form.get('setup_key',''),SETUP_KEY): return self.error_page(403,'首次安装密钥不正确')
        user=form.get('username','').strip()
        pwd=form.get('password','')
        if not re.fullmatch(r'[\w\u4e00-\u9fff-]{2,40}',user): return self.error_page(400,'用户名必须为 2–40 位中英文、数字或横线')
        if len(pwd)<10 or len(pwd)>200: return self.error_page(400,'密码长度应为 10–200 位')
        salt,digest=make_password(pwd)
        with atomic() as con:
            if self.has_owner(con): return self.error_page(409,'账户已初始化')
            con.execute('INSERT INTO owner(id,username,salt,pwdhash,created_at) VALUES (1,?,?,?,?)',
                        (user,salt,digest,timestamp()))
            token=self.create_session(con)
        return self.redirect('/',self.session_cookie(token))

    def login(self,form):
        pwd=form.get('password','')
        ip=self.client_address[0]
        now=int(time.time())
        with atomic() as con:
            fail=con.execute('SELECT * FROM login_attempts WHERE key=?',(ip,)).fetchone()
            if fail and fail['attempts']>=8 and now-fail['first_at']<900:
                return self.error_page(429,'尝试次数过多，请 15 分钟后再试')
            owner=con.execute('SELECT * FROM owner WHERE id=1').fetchone()
            if not owner or not check_password(pwd,owner['salt'],owner['pwdhash']):
                attempts=fail['attempts']+1 if fail and now-fail['first_at']<900 else 1
                first=fail['first_at'] if fail and now-fail['first_at']<900 else now
                con.execute('INSERT INTO login_attempts(key,attempts,first_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET attempts=excluded.attempts,first_at=excluded.first_at',(ip,attempts,first))
                return self.error_page(401,'密码不正确')
            con.execute('DELETE FROM login_attempts WHERE key=?',(ip,))
            token=self.create_session(con)
        return self.redirect('/',self.session_cookie(token))

    def handle_action(self,path,f):
        if path=='/tasks/create':
            t=self.validate_task(f)
            with atomic() as con:
                con.execute('''INSERT INTO tasks(title,description,cadence,target,coins,energy,category,custom_days,anchor_date,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?)''',(*t,timestamp()))
            return '新任务已创建',t[2]
        m=re.fullmatch(r'/tasks/(\d+)/(edit|archive|progress|claim)',path)
        if m:
            tid=int(m[1]);action=m[2]
            with atomic() as con:
                task=con.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
                if not task or not task['enabled']: raise ValueError('任务不存在或已归档')
                view=task['cadence']
                if action=='edit':
                    t=self.validate_task(f)
                    con.execute('''UPDATE tasks SET title=?,description=?,cadence=?,target=?,coins=?,energy=?,category=?,custom_days=?,anchor_date=? WHERE id=?''',(*t,tid))
                    return '任务设置已保存',t[2]
                if action=='archive':
                    con.execute('UPDATE tasks SET enabled=0 WHERE id=?',(tid,))
                    return '任务已归档，历史领取记录保留',view
                key,_=period(task)
                if task['cadence']=='custom' and today()<dt.date.fromisoformat(task['anchor_date']): raise ValueError('任务还未到开始日期')
                was_claimed=con.execute('SELECT id FROM task_claims WHERE task_id=? AND period_key=?',(tid,key)).fetchone()
                if action=='progress':
                    if was_claimed: raise ValueError('任务奖励已经领取，本周期进度不可再变更')
                    delta=number(f.get('delta'),0,-1,1)
                    if delta not in (-1,1): raise ValueError('进度调整值无效')
                    row=con.execute('SELECT count FROM progress WHERE task_id=? AND period_key=?',(tid,key)).fetchone()
                    count=(row['count'] if row else 0)+delta
                    if count<0 or count>task['target']: raise ValueError('任务进度已达上下限')
                    con.execute('''INSERT INTO progress(task_id,period_key,count) VALUES(?,?,?)
                         ON CONFLICT(task_id,period_key) DO UPDATE SET count=excluded.count''',(tid,key,count))
                    return '任务已完成，可以领取奖励！' if count>=task['target'] else '进度已更新',view
                if was_claimed: raise ValueError('本周期任务奖励已领取')
                row=con.execute('SELECT count FROM progress WHERE task_id=? AND period_key=?',(tid,key)).fetchone()
                if not row or row['count']<task['target']: raise ValueError('还未达到任务目标')
                con.execute('INSERT INTO task_claims(task_id,period_key,coins,energy,claimed_at) VALUES (?,?,?,?,?)',
                            (tid,key,task['coins'],task['energy'],timestamp()))
                add_ledger(con,task['coins'],'task',f'任务奖励：{task["title"]}')
            return f'🎉 任务奖励已领取：+{task["coins"]} 金币，+{task["energy"]} 活跃度',view
        m=re.fullmatch(r'/chests/(daily|weekly|monthly)/(20|50|100)/claim',path)
        if m:
            cadence=m[1];pct=int(m[2]);cycle=period_key_for_tab(cadence)
            with atomic() as con:
                rows=current_task_rows(con,cadence)
                obtained,max_energy=current_energy(rows)
                if not max_energy or obtained*100<pct*max_energy: raise ValueError('活跃度尚未达到领取门槛')
                reward=int(con.execute('SELECT value FROM settings WHERE key=?',(f'chest:{cadence}:{pct}',)).fetchone()['value'])
                con.execute('INSERT INTO chest_claims(cadence,period_key,threshold,coins,claimed_at) VALUES (?,?,?,?,?)',
                            (cadence,cycle,pct,reward,timestamp()))
                add_ledger(con,reward,'chest',f'{CADENCES[cadence]} {pct}% 活跃度宝箱')
            return f'🎁 宝箱奖励领取成功！+{reward} 金币',cadence
        if path=='/shop/create':
            title,desc,price=self.validate_reward(f)
            with atomic() as con:
                con.execute('INSERT INTO shop(title,description,price,created_at) VALUES (?,?,?,?)',(title,desc,price,timestamp()))
            return '新奖励已加入商店','shop'
        m=re.fullmatch(r'/shop/(\d+)/(edit|archive|redeem)',path)
        if m:
            sid=int(m[1]);action=m[2]
            with atomic() as con:
                item=con.execute('SELECT * FROM shop WHERE id=?',(sid,)).fetchone()
                if not item or not item['enabled']:raise ValueError('奖励不存在或已下架')
                if action=='edit':
                    title,desc,price=self.validate_reward(f)
                    con.execute('UPDATE shop SET title=?,description=?,price=? WHERE id=?',(title,desc,price,sid))
                    return '奖励设置已保存','shop'
                if action=='archive':
                    con.execute('UPDATE shop SET enabled=0 WHERE id=?',(sid,))
                    return '奖励已下架','shop'
                if balance(con)<item['price']:raise ValueError('金币余额不足')
                con.execute('INSERT INTO redemptions(item_id,title,price,redeemed_at) VALUES (?,?,?,?)',
                            (sid,item['title'],item['price'],timestamp()))
                add_ledger(con,-item['price'],'redeem',f'兑换奖励：{item["title"]}')
            return f'🎉 已兑换：{item["title"]}，记得兑现给自己的奖励！','shop'
        m=re.fullmatch(r'/settings/chest/(daily|weekly|monthly)',path)
        if m:
            cadence=m[1]
            amounts=[]
            for pct in CHEST_LEVELS:
                raw=f.get('p'+str(pct),'')
                n=number(raw,-1,0,100000)
                if n<0:raise ValueError('宝箱金币应为 0–100000 的整数')
                amounts.append(n)
            with atomic() as con:
                for pct,coins in zip(CHEST_LEVELS,amounts):
                    con.execute('UPDATE settings SET value=? WHERE key=?',(str(coins),f'chest:{cadence}:{pct}'))
            return '宝箱奖励档位已保存','settings'
        if path=='/settings/password':
            with atomic() as con:
                owner=con.execute('SELECT * FROM owner WHERE id=1').fetchone()
                if not check_password(f.get('old_password',''),owner['salt'],owner['pwdhash']):raise ValueError('原密码不正确')
                pwd=f.get('new_password','')
                if not 10<=len(pwd)<=200:raise ValueError('新密码应为 10–200 位')
                salt,digest=make_password(pwd)
                con.execute('UPDATE owner SET salt=?,pwdhash=? WHERE id=1',(salt,digest))
            return '管理员密码修改成功','settings'
        raise ValueError('未知操作')

    @staticmethod
    def validate_task(f):
        title=f.get('title','').strip()
        if not title or len(title)>80:raise ValueError('任务名称需要 1–80 个字符')
        desc=f.get('description','').strip()
        if len(desc)>500:raise ValueError('描述最多 500 个字符')
        cadence=f.get('cadence')
        if cadence not in CADENCES:raise ValueError('任务周期无效')
        target=number(f.get('target'),-1,1,10000)
        coins=number(f.get('coins'),-1,0,100000)
        energy=number(f.get('energy'),-1,0,100000)
        days=number(f.get('custom_days'),-1,1,3650)
        if min(target,coins,energy,days)<0:raise ValueError('任务数值无效')
        category=f.get('category','').strip() or '生活'
        if len(category)>20:raise ValueError('分类最多 20 字')
        try:anchor=dt.date.fromisoformat(f.get('anchor_date',''))
        except ValueError:raise ValueError('日期无效')
        if not 2000<=anchor.year<=2100: raise ValueError('年份应在 2000–2100 年')
        return (title,desc,cadence,target,coins,energy,category,days,anchor.isoformat())

    @staticmethod
    def validate_reward(f):
        title=f.get('title','').strip()
        description=f.get('description','').strip()
        price=number(f.get('price'),-1,1,1000000)
        if not title or len(title)>80:raise ValueError('奖励名称需要 1–80 字')
        if len(description)>200:raise ValueError('奖励说明最多 200 字')
        if price<0:raise ValueError('奖励价格无效')
        return title,description,price


def main():
    if not SETUP_KEY or len(SETUP_KEY)<12 or SETUP_KEY in {'replace-me-with-a-strong-setup-key', 'CHANGE-THIS-TO-A-RANDOM-LONG-SECRET'}:
        raise SystemExit('请先配置长度至少 12 位的 APP_SETUP_KEY（首次安装密钥）。')
    init_db()
    server=ThreadingHTTPServer(('0.0.0.0',PORT),Web)
    server.daemon_threads=True
    print(f'PAL running on 0.0.0.0:{PORT}, data={DB_PATH}, timezone={TZ_NAME}',flush=True)
    try:server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
