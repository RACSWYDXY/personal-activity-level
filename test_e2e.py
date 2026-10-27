#!/usr/bin/env python3
"""End-to-end regression check using only Python's standard library."""
import http.cookiejar
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parent
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0))
    PORT=sock.getsockname()[1]
URL=f'http://127.0.0.1:{PORT}'

def get(opener,path='/'):
    with opener.open(URL+path,timeout=5) as r:
        return r.read().decode()

def post(opener,path,data):
    content=urllib.parse.urlencode(data).encode()
    with opener.open(urllib.request.Request(URL+path,data=content),timeout=5) as r:
        return r.read().decode()

def csrf(page):
    return re.search(r'name="csrf" value="([^\"]+)"',page).group(1)

with tempfile.TemporaryDirectory(prefix='qf-test-') as data:
    env=dict(os.environ, APP_SETUP_KEY='test-secret-very-long-98765432',PORT=str(PORT),DATA_DIR=data,TZ='Asia/Shanghai')
    proc=subprocess.Popen([sys.executable,'-u',str(ROOT/'app.py')],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    try:
        jar=http.cookiejar.CookieJar()
        browser=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        for _ in range(100):
            try:
                landing=get(browser)
                break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError('server never became ready')
        assert '创建你的私人账户' in landing
        landing=post(browser,'/setup',dict(username='owner',password='testing@Password2026',setup_key=env['APP_SETUP_KEY']))
        assert '每日任务' in landing and '<title>PAL · Personal Activity Level</title>' in landing
        assert len(jar)>0
        token=csrf(landing)
        task=dict(csrf=token,title='每日阅读',description='试着读半小时',cadence='daily',category='学习',target='2',coins='16',energy='10',custom_days='7',anchor_date='2026-10-01')
        page=post(browser,'/tasks/create',task)
        assert '每日阅读' in page
        task_id=re.search(r'/tasks/(\d+)/progress',page).group(1)
        page=post(browser,f'/tasks/{task_id}/progress',dict(csrf=csrf(page),delta='1'))
        assert '1 / 2' in page
        page=post(browser,f'/tasks/{task_id}/progress',dict(csrf=csrf(page),delta='1'))
        assert '2 / 2' in page
        page=post(browser,f'/tasks/{task_id}/claim',dict(csrf=csrf(page)))
        assert '金币' in page and '16' in page
        # all three stages become claimable because only one quest worth ten energy
        for pct in (20,50,100):
            page=post(browser,f'/chests/daily/{pct}/claim',dict(csrf=csrf(page)))
            assert '宝箱奖励领取成功' in page
        import sqlite3
        with sqlite3.connect(Path(data)/'questflow.db') as con:
            assert con.execute('select count(*) from task_claims').fetchone()[0]==1
            assert con.execute('select count(*) from chest_claims').fetchone()[0]==3
            assert con.execute('select sum(delta) from ledger').fetchone()[0]==16+5+15+30
        shop=get(browser,'/?view=shop')
        shop=post(browser,'/shop/create',dict(csrf=csrf(shop),title='自由休息',description='放松一小时',price='30'))
        item_id=re.search(r'/shop/(\d+)/redeem',shop).group(1)
        shop=post(browser,f'/shop/{item_id}/redeem',dict(csrf=csrf(shop)))
        assert '已兑换：自由休息' in shop
        assert '36' in shop
        ach=get(browser,'/?view=achievements');assert '任务初体验' in ach and '已解锁' in ach
        history=get(browser,'/?view=history');assert '个人排行榜' in history
        settings=get(browser,'/?view=settings')
        settings=post(browser,'/settings/chest/daily',dict(csrf=csrf(settings),p20='6',p50='12',p100='20'))
        assert '宝箱奖励档位已保存' in settings
        settings=post(browser,'/settings/password',dict(csrf=csrf(settings),old_password='testing@Password2026',new_password='NewPassword_Test2026'))
        assert '管理员密码修改成功' in settings
        backup=Path(data)/'backup-copy.db'
        result=subprocess.check_output([sys.executable,str(ROOT/'backup.py'),str(backup)],env=env,text=True)
        assert 'Backup OK' in result and backup.exists()
        page=post(browser,'/logout',dict(csrf=csrf(settings)))
        assert '登录 PAL' in page
        page=post(browser,'/login',dict(password='NewPassword_Test2026'))
        assert '每日阅读' in page
        # archived tasks must not erase financial history
        page=post(browser,f'/tasks/{task_id}/archive',dict(csrf=csrf(page)))
        assert '这里还没有任务' in page
        with sqlite3.connect(Path(data)/'questflow.db') as con:
            assert con.execute('select count(*) from ledger').fetchone()[0]==5
        print('PASS: setup, auth, tasks, periods, chests, shop, achievements, leaderboard, settings, backups and archive')
    except Exception as exc:
        print('FAIL:',repr(exc))
        raise
    finally:
        proc.terminate()
        try:proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:proc.kill()
