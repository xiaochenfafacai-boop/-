import logging
import sqlite3
import json
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import re
import threading
from flask import Flask, request, jsonify
import os

# 配置日志
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==================== 核心配置 ====================
TOKEN = "8617895746:AAHAkmHi3ibyeTf3ACQ6s2IrGfQmYGg7z-w"
WEB_URL = "https://laodi-888gh.onrender.com"
PORT = int(os.environ.get('PORT', 8080))

# 创始超级管理员账户ID（接收买家审核通知和开通按钮）
FOUNDER_USERS = [8179896441]

# 销售收款与三档阶梯价格配置
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"
PRICE_1_MONTH = 80   # 1个月 80 USDT
PRICE_2_MONTH = 130  # 2个月 130 USDT
PRICE_3_MONTH = 220  # 3个月 220 USDT

TIMEZONES = {
    'china': 'Asia/Shanghai',
    'myanmar': 'Asia/Yangon',
    'thailand': 'Asia/Bangkok',
}

flask_app = Flask(__name__)

# ========== 数据库初始化 ==========
def init_db():
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY, operators TEXT DEFAULT '[]', exchange_rate REAL DEFAULT 7.2,
                  fee_rate REAL DEFAULT 0, is_active INTEGER DEFAULT 0, language TEXT DEFAULT 'chinese',
                  timezone TEXT DEFAULT 'Asia/Shanghai', show_usdt INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bills
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, user_id INTEGER, username TEXT,
                  remark TEXT, amount REAL, usdt_amount REAL, exchange_rate REAL, bill_type TEXT,
                  timestamp TEXT, date_str TEXT, is_settled INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS vip_users
                 (user_id INTEGER PRIMARY KEY, username TEXT, expire_time TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS dynamic_masters
                 (user_id INTEGER PRIMARY KEY, username TEXT, added_by INTEGER)''')
    conn.commit()
    conn.close()

# ========== 核心时间函数 ==========
def get_current_time(timezone_str):
    try:
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    except:
        tz = pytz.timezone('Asia/Shanghai')
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")

# ========== 权限判定引擎 (含最多3名主人限制) ==========
def get_all_masters():
    masters = list(FOUNDER_USERS)
    try:
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT user_id FROM dynamic_masters")
        rows = c.fetchall()
        conn.close()
        for row in rows:
            if row[0] not in masters: masters.append(row[0])
    except: pass
    return masters

def is_master(user_id):
    return user_id in get_all_masters()

def get_dynamic_masters_count():
    """获取当前已绑定的新主人数量（不含创始超级管理员）"""
    try:
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM dynamic_masters")
        count = c.fetchone()[0]
        conn.close()
        return count
    except:
        return 0

def is_vip_user(user_id):
    if is_master(user_id): return True
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        try:
            expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            return datetime.now() < expire
        except: return False
    return False

def can_use(group_id, user_id):
    if is_master(user_id) or is_vip_user(user_id): return True
    ops = json.loads(get_setting(group_id, 'operators') or '[]')
    return user_id in ops

def get_setting(group_id, key):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
    row = c.fetchone()
    conn.close()
    if not row: return None
    cols = ['group_id', 'operators', 'exchange_rate', 'fee_rate', 'is_active', 'language', 'timezone', 'show_usdt']
    return dict(zip(cols, row)).get(key)

def update_setting(group_id, key, value):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
    if c.fetchone():
        c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
    else:
        c.execute("INSERT INTO settings (group_id, operators, exchange_rate, fee_rate, is_active, language, timezone, show_usdt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (group_id, '[]', 7.2, 0, 0, 'chinese', 'Asia/Shanghai', 1))
        c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
    conn.commit()
    conn.close()

# ========== 记账数据交互核心 ==========
def add_bill(group_id, user_id, username, remark, amount, bill_type, exchange_rate=None):
    if exchange_rate is None:
        exchange_rate = get_setting(group_id, 'exchange_rate') or 7.2
    if bill_type == 'income':
        usdt_amount = amount / exchange_rate
    else:
        usdt_amount = amount
    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, full_time = get_current_time(tz_str)
    date_str = now.strftime("%Y-%m-%d")
    
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute('''INSERT INTO bills 
                 (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, timestamp, date_str, is_settled)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)''',
              (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, full_time, date_str))
    conn.commit()
    conn.close()
    return usdt_amount

def get_class_bills_by_date(group_id, target_date):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT remark, username, amount, usdt_amount, exchange_rate, timestamp FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' ORDER BY id DESC", (group_id, target_date))
    income = c.fetchall()
    c.execute("SELECT remark, username, usdt_amount, exchange_rate, timestamp FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense' ORDER BY id DESC", (group_id, target_date))
    expense = c.fetchall()
    c.execute("SELECT SUM(amount), SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income'", (group_id, target_date))
    total_income = c.fetchone()
    c.execute("SELECT SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense'", (group_id, target_date))
    total_expense = c.fetchone()
    conn.close()
    return income, expense, total_income, total_expense

def settle_today_bills(group_id, target_date):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("UPDATE bills SET is_settled = 1 WHERE group_id = ? AND date_str = ?", (group_id, target_date))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated

def delete_today_bills(group_id):
    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_date = now.strftime("%Y-%m-%d")
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ?", (group_id, today_date))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def delete_last_bill(group_id):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT id FROM bills WHERE group_id = ? ORDER BY id DESC LIMIT 1", (group_id,))
    last = c.fetchone()
    if last:
        c.execute("DELETE FROM bills WHERE id = ?", (last[0],))
        deleted = 1
    else: deleted = 0
    conn.commit()
    conn.close()
    return deleted

def delete_all_bills(group_id):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ?", (group_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def delete_user_bills(group_id, name):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ? AND (LOWER(username) = ? OR LOWER(remark) = ?)", (group_id, name.lower(), name.lower()))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

# ========== Web 后台渲染与 API 端点 ==========
@flask_app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>课时历史账单系统</title><style>*{margin:0;padding:0;box-sizing:border-box;}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;background:#f0f2f5;padding:20px;}.container{max-width:1400px;margin:0 auto;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.1);overflow:hidden;}.header{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:24px 30px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:15px;}.header-text{flex:1;}.header h1{font-size:28px;margin-bottom:8px;}.date-picker-box{background:rgba(255,255,255,0.2);padding:10px 15px;border-radius:8px;color:white;}.date-picker-box label{font-size:14px;margin-right:8px;font-weight:bold;}.date-picker-box input{border:none;padding:6px 10px;border-radius:4px;font-size:14px;outline:none;}.content{padding:24px 30px;}.section{margin-bottom:32px;}.section-title{font-size:18px;font-weight:600;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #667eea;}table{width:100%;border-collapse:collapse;font-size:14px;}th,td{padding:12px 10px;text-align:left;border-bottom:1px solid #eef2f6;}th{background:#f8f9fc;font-weight:600;}.stats-box{background:linear-gradient(135deg,#f8f9fc 0%,#f0f2f5 100%);border-radius:12px;padding:24px;margin-top:20px;}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;}.stat-card{background:white;padding:16px;border-radius:12px;text-align:center;}.stat-label{font-size:12px;color:#888;margin-bottom:8px;}.stat-value{font-size:24px;font-weight:700;color:#333;}.stat-item{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eef2f6;}.stat-name{font-weight:500;color:#333;}.stat-number{color:#667eea;font-weight:600;}.loading{text-align:center;padding:50px;color:#888;}</style></head>
    <body>
        <div class="container">
            <div class="header">
                <div class="header-text">
                    <h1>📋 实时课堂账单历史明细</h1>
                    <p id="dateInfo">默认同步实时账单</p>
                </div>
                <div class="date-picker-box">
                    <label>📅 选择账单日期:</label>
                    <input type="date" id="targetDate" onchange="onDateChange()">
                </div>
            </div>
            <div class="content" id="content"><div class="loading">正在同步实时账单...</div></div>
        </div>
        <script>
            let GROUP_ID = null; let currentSelectedDate = "";
            const today = new Date(); const yyyy = today.getFullYear(); let mm = today.getMonth() + 1; let dd = today.getDate();
            if (mm < 10) mm = '0' + mm; if (dd < 10) dd = '0' + dd;
            currentSelectedDate = `${yyyy}-${mm}-${dd}`; document.getElementById('targetDate').value = currentSelectedDate;

            function getGroupID() { 
                const urlParams = new URLSearchParams(window.location.search); GROUP_ID = urlParams.get('group_id'); 
                if (!GROUP_ID) { document.getElementById('content').innerHTML = '<div class="loading">❌ 请通过机器人的 "查看完整账单" 按钮访问</div>'; return false; } 
                return true; 
            }
            function onDateChange() { currentSelectedDate = document.getElementById('targetDate').value; loadData(); }

            async function loadData() { 
                if (!GROUP_ID) return;
                try { 
                    const response = await fetch(`/api/bill?group_id=${GROUP_ID}&date=${currentSelectedDate}`); 
                    const data = await response.json(); 
                    if (data.error || (!data.income_bills.length && !data.expense_bills.length)) { 
                        document.getElementById('content').innerHTML = `<div class="loading">📅 ${currentSelectedDate} 暂无账单数据记录</div>`; return; 
                    }
                    let suffix = data.show_usdt ? ' USDT' : ''; let html = '';
                    if (data.income_bills && data.income_bills.length > 0) { 
                        html += `<div class="section"><div class="section-title">📥 入款记录 (${data.income_bills.length} 笔)</div><table><thead><tr><th>备注</th><th>时间</th><th>金额(元)</th><th>汇率</th><th>等值数量</th><th>操作人</th></tr></thead><tbody>`; 
                        for (const bill of data.income_bills) { html += `<tr><td><b>${bill.remark}</b></td><td>${bill.time}</td><td>${bill.amount}</td><td>${bill.exchange_rate}</td><td>${bill.usdt}${suffix}</td><td>${bill.username}</td></tr>`; } 
                        html += `</tbody></table></div>`; 
                    }
                    if (data.expense_bills && data.expense_bills.length > 0) { 
                        html += `<div class="section"><div class="section-title">📤 下发记录 (${data.expense_bills.length} 笔)</div><table><thead><tr><th>备注</th><th>时间</th><th>下发数量</th><th>操作人</th></tr></thead><tbody>`; 
                        for (const bill of data.expense_bills) { html += `<tr><td><b>${bill.remark}</b></td><td>${bill.time}</td><td>${bill.usdt}${suffix}</td><td>${bill.username}</td></tr>`; } 
                        html += `</tbody></table></div>`; 
                    }
                    if (data.remark_stats && data.remark_stats.length > 0) { 
                        html += `<div class="section"><div class="section-title">📊 备注分类统计</div>`; 
                        for (const stat of data.remark_stats) { html += `<div class="stat-item"><span class="stat-name">📝 ${stat.remark}</span><span class="stat-number">${stat.count}笔 | ${stat.amount}元 | ${stat.usdt}${suffix}</span></div>`; } 
                        html += `</div>`; 
                    }
                    html += `<div class="stats-box"><div class="stats-grid"><div class="stat-card"><div class="stat-label">💰 费率</div><div class="stat-value">${data.fee_rate}%</div></div><div class="stat-card"><div class="stat-label">💱 汇率</div><div class="stat-value">${data.exchange_rate}</div></div><div class="stat-card"><div class="stat-label">📥 总入款(元)</div><div class="stat-value">${data.total_rmb}</div></div><div class="stat-card"><div class="stat-label">💵 总入款数量</div><div class="stat-value">${data.total_usdt}${suffix}</div></div><div class="stat-card"><div class="stat-label">📤 已下发</div><div class="stat-value">${data.expense_usdt}${suffix}</div></div><div class="stat-card"><div class="stat-label">📊 未下发</div><div class="stat-value">${data.remaining_usdt}${suffix}</div></div></div></div>`;
                    document.getElementById('content').innerHTML = html;
                } catch (err) { document.getElementById('content').innerHTML = '<div class="loading">❌ 数据解析错误或网络异常</div>'; }
            }
            if (getGroupID()) { loadData(); setInterval(() => { const t = new Date(); let m = t.getMonth() + 1; let d = t.getDate(); if (m < 10) m = '0' + m; if (d < 10) d = '0' + d; if (currentSelectedDate === `${t.getFullYear()}-${m}-${d}`) { loadData(); } }, 4000); }
        </script>
    </body>
    </html>
    '''

@flask_app.route('/api/bill')
def api_bill():
    try:
        group_id = request.args.get('group_id', type=int, default=0)
        tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        today_str = now.strftime("%Y-%m-%d")
        target_date = request.args.get('date', default=today_str)
        
        income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)
        rate = get_setting(group_id, 'exchange_rate') or 7.2
        fee_rate = get_setting(group_id, 'fee_rate') or 0
        show_usdt = get_setting(group_id, 'show_usdt') or 1
        
        total_rmb = total_income[0] if (total_income and total_income[0]) else 0
        total_usdt = total_income[1] if (total_income and total_income[1]) else 0
        expense_usdt = total_expense[0] if (total_expense and total_expense[0]) else 0
        
        income_bills = []
        expense_bills = []
        for row in income:
            remark, username, amount, usdt, ex_rate, ts = row
            time_str = ts[5:16] if (ts and len(ts) > 11) else (ts or '-')
            income_bills.append({'remark': remark or '-', 'username': username or '未知', 'amount': f"{amount or 0:.0f}", 'usdt': f"{usdt or 0:.2f}", 'exchange_rate': f"{ex_rate or rate:.2f}", 'time': time_str})
        for row in expense:
            remark, username, usdt, ex_rate, ts = row
            time_str = ts[5:16] if (ts and len(ts) > 11) else (ts or '-')
            expense_bills.append({'remark': remark or '-', 'username': username or '未知', 'usdt': f"{usdt or 0:.2f}", 'time': time_str})

        remark_stats = []
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT remark, COUNT(*), SUM(amount), SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' GROUP BY remark ORDER BY SUM(usdt_amount) DESC", (group_id, target_date))
        for row in c.fetchall():
            remark_stats.append({'remark': row[0] if row[0] else '无备注', 'count': row[1] or 0, 'amount': f"{row[2] or 0:.0f}", 'usdt': f"{row[3] or 0:.2f}"})
        conn.close()
        
        return jsonify({
            'exchange_rate': f"{rate:.2f}", 'fee_rate': f"{fee_rate:.0f}", 'total_rmb': f"{total_rmb:.0f}", 
            'total_usdt': f"{total_usdt:.2f}", 'expense_usdt': f"{expense_usdt:.2f}", 
            'remaining_usdt': f"{total_usdt - expense_usdt:.2f}", 'show_usdt': int(show_usdt), 
            'income_bills': income_bills, 'expense_bills': expense_bills, 'remark_stats': remark_stats
        })
    except Exception as e:
        return jsonify({'error': True, 'msg': str(e)}), 500

# ========== 语言包与面板渲染生成器 ==========
def get_help_text(lang):
    if lang == 'myanmar':
        return """
🤖 *စာရင်းကိုင်ဘော့ အကူအညီ* (Help)
📌 *စာရင်းသွင်းရန် ပုံစံများ：*
`+1000` - Ngwe Win 1000 Kyat
`-1000` - Ngwe Win -1000 Kyat
`MatChet+2000` - 带备注入款
`MatChet-2000` - 带备注减款
`Thut50` / `下发50` - 50 USDT Thut Ranyan
`MatChetThut50` - 带备注下发
`+0` - YaNay SaYinChoke KyiRanyan

📌 *စီမံခန့်ခွဲရေး ကွတ်ကီးများ：*
`အတန်းစ` / `上课` - SaYinKoing Sinit PhwintChin
`အတန်းဆင်း` / `下课` - SaYinPate Pyee ShinLinChin
`ငွေလဲနှုန်း 7.2` / `设置汇率 7.2` - ThatMatRanyan
`အော်ပရေတာခန့်ရန်` / `设置操作人` - KhantRanyan 
`အော်ပရေတာစာရင်း` / `查看操作员列表` - KyiRanyan
`ဘာသာစကား` / `改语言` - PyaungRanyan (中文/မြန်မာ)
`အချိန်သတ်မှတ်` / `设置时间` - AChainZone PyaungRanyan
"""
    else:
        return """
🤖 *记账机器人使用指南*
📌 *记账格式：*
`+1000` - 入款1000元
`-1000` - 入款-1000元 (扣减款)
`备注+2000` - 带备注入款
`备注-2000` - 带备注减款
`下发50` / `ထုတ်50` - 下发50 USDT
`备注下发50` - 带备注下发50 USDT
`+0` - 查看今日汇总

📌 *管理命令：*
`上课` - 开启记账模式
`下课` - 关闭记账模式并归档
`设置汇率 7.2` - 设置当前常规汇率
`设置操作人 @用户名` - 授权群成员协助记账（可直接@或回复消息）
`查看操作员列表` - 查看本群操作人
`改语言` - 切换群内 system 语言（中文/缅甸语）
`设置时间 china/myanmar` - 调整本群结算时区

📌 *删除命令：*
`删今天` - 清空今日账单 | `删最后` - 撤销最后一笔
`全部清单` - 清空历史 | `清单+备注` - 删除指定备注账单
"""

def get_bill_content(income, expense, total_rmb, total_usdt, expense_usdt, rate, today_date, lang):
    unit = "U"
    if lang == 'myanmar':
        income_title, expense_title, rate_text, total_text, exp_text, rem_text = "📥 ငွေဝင်", "📤 ထုတ်ငွေ", "💰 လဲနှုန်း", "📊 စုစုပေါင်း", "📊 ထုတ်ပြီး", "📊 ကျန်ငွေ"
    else:
        income_title, expense_title, rate_text, total_text, exp_text, rem_text = "📥 入款", "📤 下发", "💰 汇率", "📊 总入款", "📊 已下发", "📊 未下发"
        
    message = f"📊 账单汇总 ({today_date})\n\n"
    if income:
        message += f"{income_title}:\n"
        for bill in income[:5]:
            remark, username, amount, usdt, ex_rate, ts = bill
            time_short = ts[11:16] if (ts and len(ts) > 16) else ''
            rem_str = f"【{remark}】" if remark else ""
            message += f"  {time_short} {rem_str}{amount or 0:.0f}/{ex_rate or rate:.1f}={usdt or 0:.1f}{unit}\n"
        message += "\n"
    if expense:
        message += f"{expense_title}:\n"
        for bill in expense[:5]:
            remark, username, usdt, ex_rate, ts = bill
            time_short = ts[11:16] if (ts and len(ts) > 16) else ''
            rem_str = f"【{remark}】" if remark else ""
            message += f"  {time_short} {rem_str}{usdt or 0:.1f}{unit}\n"
        message += "\n"
        
    message += f"{rate_text}: {rate:.2f}\n"
    message += f"{total_text}: {total_rmb:.0f} | {total_usdt:.1f}{unit}\n"
    message += f"{exp_text}: {expense_usdt:.1f}{unit}\n"
    message += f"{rem_text}: {total_usdt - expense_usdt:.1f}{unit}"
    return message

async def show_full_bill(update: Update, gid):
    tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_date = now.strftime("%Y-%m-%d")
    
    income, expense, total_income, total_expense = get_class_bills_by_date(gid, today_date)
    rate = get_setting(gid, 'exchange_rate') or 7.2
    lang = get_setting(gid, 'language') or 'chinese'
    total_rmb = total_income[0] or 0
    total_usdt = total_income[1] or 0
    expense_usdt = total_expense[0] or 0
    
    message = get_bill_content(income, expense, total_rmb, total_usdt, expense_usdt, rate, today_date, lang)
    keyboard = [
        [InlineKeyboardButton("📊 完整账单 (Web)", url=f"{WEB_URL}?group_id={gid}")],
        [InlineKeyboardButton("📖 帮助 (Help)", callback_data='show_help')]
    ]
    if update.message:
        await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        await update.callback_query.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

# ========== 私聊销售大厅控制键盘 ==========
def get_private_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("💰 充值续费套餐", callback_data="menu_renew"),
         InlineKeyboardButton("📅 检查到期时间", callback_data="menu_expire")],
        [InlineKeyboardButton("👑 添加新机器人主人", callback_data="menu_add_master"),
         InlineKeyboardButton("📖 机器人使用指南", callback_data="menu_help")],
        [InlineKeyboardButton("🌐 访问账单网页端", url=WEB_URL)]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_renew_text():
    return f"""
💰 <b>【智能记账系统 - 三档特惠续费套餐】</b>
---
📊 <b>当前最新特惠价格：</b>
🔴 <b>1 个月 (30天)：</b> <code>{PRICE_1_MONTH} TRX</code>
🟡 <b>2 个月 (60天)：</b> <code>{PRICE_2_MONTH} TRX</code>
🟢 <b>3 个月 (90天)：</b> <code>{PRICE_3_MONTH} TRX</code>

🌟 <b>特权包干：</b> 购买后，您名下在<b>【无数个群组】</b>拉入此机器人均可自动解锁，不受限制！

📌 <b>自主转账与截图核对流程：</b>
1️⃣ 请向下方 <b>TRX/波场</b> 官方收币地址转账对应套餐金额：
👉 <code>{TRON_ADDRESS}</code> <i>(点击可自动复制)</i>

2️⃣ 转账成功后，<b>请直接将您的【转账成功截图】发送到当前私聊对话框中！</b>
3️⃣ 机器人会自动将截图提交给创始主人审核，审核通过后秒开特权！
"""

# ========== 核心网关事件分流器 ==========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        welcome_text = (
            f"👋 您好，<b>{update.effective_user.first_name}</b>！欢迎使用智能记账多群分销版后台管理大厅。\n\n"
            f"💡 请使用下方的高级控制面板管理您的记账特权、绑定新主人或查看账单："
        )
        await update.message.reply_text(welcome_text, reply_markup=get_private_main_keyboard(), parse_mode="HTML")
    else:
        await update.message.reply_text("📊 记账机器人已在群组就绪！包月买家请输入 <code>上课</code> 开启记账。私聊我可进入充值大厅。", parse_mode="HTML")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    gid = update.effective_chat.id
    uid = query.from_user.id
    await query.answer()

    # 内联按钮权限验证 (群组内防乱点)
    if update.effective_chat.type != "private":
        if not can_use(gid, uid):
            await query.answer("❌ 您没有权限点击此机器人的操作按钮", show_alert=True)
            return

    # 1. 创始人专属：图片快捷审核流机制
    if query.data.startswith("img_approve_"):
        parts = query.data.split("_")
        target_uid = int(parts[2])
        months = int(parts[3])
        days_to_add = months * 30
        
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        
        # 检查是否已经是老VIP，是的话就在到期时间上累加
        c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (target_uid,))
        row = c.fetchone()
        
        if row:
            try:
                current_expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                if current_expire > datetime.now():
                    new_expire = current_expire + timedelta(days=days_to_add)
                else:
                    new_expire = datetime.now() + timedelta(days=days_to_add)
            except:
                new_expire = datetime.now() + timedelta(days=days_to_add)
        else:
            new_expire
