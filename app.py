#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import uuid
import shutil
import tempfile
import subprocess
import requests
from seleniumbase import SB

# 当前正在处理的账号（由 set_current_account 切换；login / send_tg_message 读取）
EMAIL        = ""
PASSWORD     = ""

TG_CHAT_ID   = os.environ.get("TG_CHAT_ID") or ""        # tg通知 chat id(可选)
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""      # tg通知bot token(可选)

BASE_URL = "https://dashboard.katabump.com"  # 网站链接


# ===== 多账号支持 =====

def set_current_account(email, password):
    """切换当前处理的账号（login / send_tg_message 仍读这两个全局）"""
    global EMAIL, PASSWORD
    EMAIL, PASSWORD = email, password


def _mask_email(email):
    """日志/通知用的邮箱脱敏：保留用户名前2位和后2位"""
    if "@" not in email:
        return email[:2] + "****"
    name, domain = email.split("@", 1)
    return f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else f"{name}@{domain}"


def _as_text(value):
    """把 JSON 里的任意标量归一化成 str，避免数字/布尔导致 .strip()/.replace() 崩溃"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _load_accounts():
    """返回 [(email, password), ...]

    优先读取 KATABUMP_ACCOUNTS（JSON 数组）；
    完全未配置该项时，回落到旧的单账号 KATABUMP_EMAIL + KATABUMP_PASSWORD。
    注意：KATABUMP_ACCOUNTS 一旦配置但格式非法，直接返回空列表（不回落），
    避免「报了错却只续一个账号」的静默降级。
    """
    raw = (os.environ.get("KATABUMP_ACCOUNTS") or "").strip()
    accounts = []

    if raw:
        try:
            data = json.loads(raw)
        except Exception as ex:
            print(f"❌ KATABUMP_ACCOUNTS JSON 解析失败: {ex}")
            return []

        if not isinstance(data, list):
            print('❌ KATABUMP_ACCOUNTS 必须是 JSON 数组，例如 [{"email":"a@x.com","password":"p"}]')
            return []

        for i, item in enumerate(data, 1):
            if not isinstance(item, dict):
                print(f"⚠️ 第 {i} 项不是对象，已跳过")
                continue
            e = _as_text(item.get("email")).strip()
            p = _as_text(item.get("password"))
            if e and p:
                accounts.append((e, p))
            else:
                print(f"⚠️ 第 {i} 项邮箱或密码为空，已跳过: {e or '(空邮箱)'}")

        return accounts

    # 未配置 KATABUMP_ACCOUNTS，走旧单账号配置
    e = (os.environ.get("KATABUMP_EMAIL") or "").strip()
    p = os.environ.get("KATABUMP_PASSWORD") or ""
    if e and p:
        accounts.append((e, p))

    return accounts


# ===== 浏览器 profile 隔离与清理 =====

def _clear_profile(profile_dir):
    """删除指定 profile 目录（浏览器已退出后才能删干净）"""
    if not profile_dir or not os.path.isdir(profile_dir):
        return True
    for attempt in range(5):
        try:
            shutil.rmtree(profile_dir, ignore_errors=False)
            return True
        except FileNotFoundError:
            return True
        except Exception as e:
            print(f"⚠️ 清理 profile 第 {attempt + 1} 次失败: {e}")
            time.sleep(0.5)
    # 最后兜底：忽略错误强删
    shutil.rmtree(profile_dir, ignore_errors=True)
    return not os.path.isdir(profile_dir)


def _safe_error(e, *secrets):
    """把异常文本转成可安全打印的字符串：剔除密码等敏感片段"""
    msg = f"{type(e).__name__}: {e}"
    for s in secrets:
        if s and len(s) >= 3:
            msg = msg.replace(s, "***")
    return msg[:300]


def _reset_browser_state(profile_dir):
    """账号之间的隔离动作：删除本账号的 profile 目录

    浏览器进程已由 with SB(...) 的 __exit__ 关闭，这里只需清掉 profile，
    下个账号会拿到全新目录，cookie / 缓存 / 登录态自然为空。
    """
    print("🧹 清理浏览器缓存...")
    ok = _clear_profile(profile_dir)
    if ok:
        print("✅ profile 目录已删除，下个账号使用全新环境")
    else:
        print("⚠️ profile 目录未能完全删除，继续执行")
    return ok


#  Telegram 推送模块
def send_tg_message(status_icon, status_text, time_left=""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 推送。")
        return

    # 获取北京时间 (UTC+8)
    local_time = time.gmtime(time.time() + 8 * 3600)
    current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", local_time)

    # 邮箱脱敏：保留用户名前2位和后2位，中间用****代替
    masked_email = _mask_email(EMAIL)

    text = (
        f"🇫🇷 katabump 续期通知\n\n"
        f"{status_icon} {status_text}\n"
        f"👤 续期账户: {masked_email}\n"
        f"⏱️ 续期时间: {current_time_str}"
    )
    # 第三个参数承载页面原文/附加说明，之前被丢弃，导致告警只有标题没有细节
    if time_left:
        text += f"\n📄 详情: {time_left}"

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text
    }
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("📩 Telegram 通知发送成功！")
        else:
            # 不回显 r.text / 异常原文：其中可能包含带 bot token 的请求 URL
            print(f"⚠️ Telegram 通知发送失败: HTTP {r.status_code}")
    except Exception as e:
        # 只打印异常类型，避免 bot token 随 URL 进入 Actions 日志
        print(f"⚠️ Telegram 通知发送异常: {type(e).__name__}")

#  页面注入脚本
_EXPAND_JS = """
(function() {
    var ts = document.querySelector('input[name="cf-turnstile-response"]');
    if (!ts) return 'no-turnstile';
    var el = ts;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var s = window.getComputedStyle(el);
        if (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.overflowY === 'hidden')
            el.style.overflow = 'visible';
        el.style.minWidth = 'max-content';
    }
    document.querySelectorAll('iframe').forEach(function(f){
        if (f.src && f.src.includes('challenges.cloudflare.com')) {
            f.style.width = '300px'; f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible'; f.style.opacity = '1';
        }
    });
    return 'done';
})()
"""

_EXISTS_JS = """
(function(){
    return document.querySelector('input[name="cf-turnstile-response"]') !== null;
})()
"""

_SOLVED_JS = """
(function(){
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(i && i.value && i.value.length > 20);
})()
"""

_WININFO_JS = """
(function(){
    return {
        sx: window.screenX || 0,
        sy: window.screenY || 0,
        oh: window.outerHeight,
        ih: window.innerHeight
    };
})()
"""

# ===== 自动续期相关 =====

# 在模态框内查找 iframe 并展开，返回点击坐标
_ALTCHA_EXPAND_JS = """
(function() {
    var modal = document.querySelector('div.modal.show') || document;
    var iframes = modal.querySelectorAll('iframe');
    for (var i = 0; i < iframes.length; i++) {
        var r = iframes[i].getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
            iframes[i].style.width  = '300px';
            iframes[i].style.height = '150px';
            iframes[i].style.minWidth  = '300px';
            iframes[i].style.minHeight = '150px';
            iframes[i].style.visibility = 'visible';
            iframes[i].style.opacity = '1';
            var el = iframes[i];
            for (var j = 0; j < 10; j++) {
                el = el.parentElement;
                if (!el) break;
                el.style.overflow = 'visible';
            }
            var r2 = iframes[i].getBoundingClientRect();
            return { cx: Math.round(r2.x + 30), cy: Math.round(r2.y + r2.height / 2) };
        }
    }
    return null;
})()
"""

# 检测 ALTCHA 是否已验证通过
_ALTCHA_SOLVED_JS = """
(function(){
    var modal = document.querySelector('div.modal.show') || document;
    // hidden input 有值
    var inputs = modal.querySelectorAll('input[type="hidden"]');
    for (var i = 0; i < inputs.length; i++) {
        var n = (inputs[i].name || '').toLowerCase();
        if ((n.includes('altcha') || n.includes('captcha')) &&
            inputs[i].value && inputs[i].value.length > 20) return true;
    }
    // checkbox 变为 disabled
    var cbs = modal.querySelectorAll('input[type="checkbox"]');
    for (var j = 0; j < cbs.length; j++) {
        if (cbs[j].disabled) return true;
    }
    // widget data-state 属性
    var w = modal.querySelector('[data-state="verified"],.altcha--verified,.altcha-verified');
    if (w) return true;
    return false;
})()
"""

#  底层输入工具
def js_fill_input(sb, selector: str, text: str):
    safe_text = text.replace('\\', '\\\\').replace('"', '\\"')
    sb.execute_script(f"""
    (function(){{
        var el = document.querySelector('{selector}');
        if (!el) return;
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
        if (nativeInputValueSetter) {{
            nativeInputValueSetter.call(el, "{safe_text}");
        }} else {{
            el.value = "{safe_text}";
        }}
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }})()
    """)

def _activate_window():
    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls], capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]], timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"], timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# def _xdotool_click(x: int, y: int):
#     _activate_window()
#     try:
#         subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=3, stderr=subprocess.DEVNULL)
#         time.sleep(0.15)
#         subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
#     except Exception:
#         os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")

#  人机验证处理（使用 SeleniumBase 内置 uc_gui_click_captcha）
def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)

    # 检查是否已静默通过
    if sb.execute_script(_SOLVED_JS):
        print("✅ 已静默通过")
        return True

    # 尝试展开 Turnstile（防止被父容器 overflow:hidden 裁剪）
    for _ in range(3):
        try: sb.execute_script(_EXPAND_JS)
        except Exception: pass
        time.sleep(0.5)

    # 使用 SeleniumBase 内置 uc_gui_click_captcha 处理 Turnstile
    # 该方法自动完成：检测验证码类型 → 定位 iframe → 计算坐标 → PyAutoGUI 平滑点击
    for attempt in range(6):
        if sb.execute_script(_SOLVED_JS):
            print(f"✅ Turnstile 通过（第 {attempt} 次尝试）")
            return True

        print(f"🖱️ 第 {attempt + 1} 次调用 uc_gui_click_captcha...")
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"⚠️ uc_gui_click_captcha 调用异常: {e}")

        # 等待验证结果（最多 8 秒）
        for _ in range(16):
            time.sleep(0.5)
            if sb.execute_script(_SOLVED_JS):
                print(f"✅ Turnstile 通过（第 {attempt + 1} 次尝试）")
                return True

        print(f"⚠️ 第 {attempt + 1} 次未通过，重试...")

    print("  ❌ Turnstile 6 次均失败")
    return False

#  账户登录
def login(sb) -> bool:
    print(f"🌐 打开登录页面: {BASE_URL}/auth/login")
    sb.uc_open_with_reconnect(BASE_URL + "/auth/login", reconnect_time=8)
    time.sleep(8)

    # 先等待 Cloudflare 验证通过（最多等 30 秒）
    print("⏳ 等待 Cloudflare 验证通过...")
    cf_passed = False
    for i in range(30):
        page_src = sb.get_page_source() or ""
        if 'input[name="email"]' in page_src.lower() or 'name="email"' in page_src.lower():
            cf_passed = True
            print(f"✅ Cloudflare 验证已通过（{i+1}s）")
            break
        time.sleep(1)
    if not cf_passed:
        print("⚠️ Cloudflare 验证可能未通过，继续尝试...")

    try:
        sb.wait_for_element('input[type="email"]', timeout=15)
    except Exception:
        # 尝试大写选择器作为后备
        try:
            sb.wait_for_element('input[type="Email"]', timeout=5)
        except Exception:
            print("❌ 页面未加载出登录表单")
            cur_url = sb.get_current_url()
            page_title = sb.get_title() or ""
            print(f"  当前 URL: {cur_url}")
            print(f"  当前标题: {page_title}")
            return False

    print("🍪 关闭可能的 Cookie 弹窗...")
    try:
        for btn in sb.find_elements("button"):
            if "Accept" in (btn.text or ""):
                btn.click()
                time.sleep(0.5)
                break
    except Exception:
        pass

    print(f"📧 填写邮箱...")
    js_fill_input(sb, 'input[type="email"]', EMAIL)
    time.sleep(1)
    
    print("🔑 填写密码...")
    js_fill_input(sb, 'input[type="password"]', PASSWORD)
    time.sleep(3)

    # 等待 Turnstile 验证框出现（最多 10 秒）
    print("⏳ 等待 Turnstile 验证框出现...")
    ts_found = False
    for i in range(10):
        if sb.execute_script(_EXISTS_JS):
            ts_found = True
            print(f"✅ 检测到 Turnstile（{i+1}s）")
            break
        time.sleep(1)

    if ts_found:
        if not handle_turnstile(sb):
            print("❌ 登录界面的 Turnstile 验证失败")
            return False
    else:
        print("ℹ️ 未检测到 Turnstile")

    print("🖱️ 敲击回车提交表单...")
    sb.press_keys('input[name="password"]', '\n')

    print("⏳ 等待登录跳转...")
    for _ in range(12):
        time.sleep(1)
        cur_url = sb.get_current_url().split('?')[0].lower()
        page_title = sb.get_title() or ""
        if cur_url.startswith(f"{BASE_URL}/dashboard") or "dashboard | katabump" in page_title.lower():
            break

    cur_url = sb.get_current_url().split('?')[0].lower()
    page_title = sb.get_title() or ""
    if cur_url.startswith(f"{BASE_URL}/dashboard") or "dashboard | katabump" in page_title.lower():
        print(f"✅ 登录成功！(URL: {sb.get_current_url()}, Title: {page_title})")
        return True
        
    print(f"❌ 登录失败，页面未跳转到账户页。(URL: {sb.get_current_url()}, Title: {page_title})")
    return False

# ===== 自动续期流程 =====

def _read_alert(sb):
    """读取页面第一个 Bootstrap alert 的文本，找不到返回空串"""
    try:
        el = sb.find_element("div.alert", timeout=4)
        return (el.text or "").strip()
    except Exception:
        return ""


def _goto_server_detail(sb) -> bool:
    """在 Dashboard 首页查找并点击 See 进入服务器详情页"""
    print("\n🖥️  正在进入服务器续期页...")
    time.sleep(5)

    # 检查页面顶部是否已有"还无法续期"全局提示
    alert_text = _read_alert(sb)
    if alert_text and "can't renew" in alert_text.lower():
        print(f"ℹ️  页面顶部提示: {alert_text}")
        send_tg_message("ℹ️", "⚠️ 未到续期时间", alert_text)
        return False

    # 多种选择器尝试查找 See 链接
    selectors = [
        'a[href*="/servers/edit?id="]',
        'td a[href*="/servers/edit"]',
        'table a[href*="/servers/edit"]',
        'table td a',
    ]

    see_link = None
    for sel in selectors:
        try:
            see_link = sb.find_element(sel, timeout=8)
            print(f"✅ 通过选择器找到链接: {sel}")
            break
        except Exception:
            continue

    # 选择器全部失败，尝试通过文本内容查找
    if see_link is None:
        print("⚠️ 选择器未命中，尝试文本匹配...")
        try:
            for a in sb.find_elements("a"):
                if (a.text or "").strip().lower() == "see":
                    see_link = a
                    print("✅ 通过文本 'See' 找到链接")
                    break
        except Exception:
            pass

    if see_link is None:
        # 打印调试信息帮助排查
        cur_url = sb.get_current_url()
        title = sb.get_title() or ""
        print(f"❌ 未找到 'See' 链接")
        print(f"当前 URL: {cur_url}")
        print(f"页面标题: {title}")
        try:
            links = sb.find_elements("a")
            print(f"     页面共 {len(links)} 个链接:")
            for a in links[:20]:
                href = a.get_attribute("href") or ""
                txt  = (a.text or "").strip()[:30]
                if href:
                    print(f"       - [{txt}] -> {href}")
        except Exception:
            pass
        return False

    print("🖱️  点击 'See' 进入服务器详情页...")
    see_link.click()
    time.sleep(5)
    print(f"📄 当前页面: {sb.get_current_url()}")
    return True


def _open_renew_modal(sb) -> bool:
    """滚动到 Renew 按钮并点击，打开模态框"""
    print("\n🔄 查找 Renew 按钮...")
    try:
        renew_btn = sb.find_element('button[data-bs-target="#renew-modal"]', timeout=10)
    except Exception:
        try:
            renew_btn = sb.find_element('button.btn.btn-outline-primary', timeout=5)
        except Exception:
            print("  ❌ 未找到 Renew 按钮")
            return False

    sb.execute_script("""
        (function(){
            var btn = document.querySelector('button[data-bs-target="#renew-modal"]')
                     || document.querySelector('button.btn.btn-outline-primary');
            if (btn) btn.scrollIntoView({behavior:'smooth',block:'center'});
        })()
    """)
    time.sleep(0.8)
    renew_btn.click()
    print("🖱️ 已点击 Renew 按钮，等待确认框...")
    time.sleep(3)

    try:
        sb.find_element('div.modal.show', timeout=5)
        print("✅ Renew 模态框已弹出")
        return True
    except Exception:
        print("⚠️ 模态框未弹出")
        return False


# def _solve_altcha(sb) -> bool:
#     """处理 ALTCHA 人机验证"""
#     print("\n🔐 处理 ALTCHA 人机验证...")
#     time.sleep(2)
#
#     # 先检查是否已自动通过
#     if sb.execute_script(_ALTCHA_SOLVED_JS):
#         print("✅ ALTCHA 已自动通过")
#         return True
#
#     # 展开模态框内 iframe 并获取坐标
#     coords = None
#     try:
#         coords = sb.execute_script(_ALTCHA_EXPAND_JS)
#     except Exception:
#         pass
#
#     if coords:
#         print(f"  📍 找到模态框内 iframe 坐标: ({coords['cx']}, {coords['cy']})")
#
#     # 最多尝试 3 轮
#     for attempt in range(3):
#         if sb.execute_script(_ALTCHA_SOLVED_JS):
#             print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮）")
#             return True
#
#         # 策略 1: xdotool 物理点击 iframe 坐标
#         if coords:
#             try:
#                 wi = sb.execute_script(_WININFO_JS)
#             except Exception:
#                 wi = {"sx": 0, "sy": 0, "oh": 800, "ih": 768}
#             bar = wi["oh"] - wi["ih"]
#             ax  = coords["cx"] + wi["sx"]
#             ay  = coords["cy"] + wi["sy"] + bar
#             print(f"🖱️  ALTCHA点击复选框  ({ax}, {ay})")
#             _xdotool_click(ax, ay)
#
#         # 策略 2: SeleniumBase 原生点击模态框内 iframe 元素
#         try:
#             iframes = sb.find_elements('div.modal.show iframe')
#             for iframe in iframes:
#                 try:
#                     iframe.click()
#                     print("🖱️  SeleniumBase 点击模态框 iframe")
#                 except Exception:
#                     pass
#         except Exception:
#             pass
#
#         # 策略 3: JS 遍历模态框内所有可点击元素
#         sb.execute_script("""
#             (function(){
#                 var modal = document.querySelector('div.modal.show');
#                 if (!modal) return;
#                 // 点击 iframe
#                 var iframes = modal.querySelectorAll('iframe');
#                 for (var i = 0; i < iframes.length; i++) {
#                     iframes[i].click();
#                     iframes[i].dispatchEvent(new MouseEvent('click', {bubbles:true}));
#                 }
#                 // 点击含 checkbox 的 label
#                 var labels = modal.querySelectorAll('label');
#                 for (var j = 0; j < labels.length; j++) {
#                     var txt = (labels[j].textContent || '').toLowerCase();
#                     if (txt.includes('robot') || txt.includes('captcha') || txt.includes('verify'))
#                         labels[j].click();
#                 }
#                 // 点击 checkbox
#                 var cbs = modal.querySelectorAll('input[type="checkbox"]');
#                 for (var k = 0; k < cbs.length; k++) {
#                     if (!cbs[k].disabled) {
#                         cbs[k].click();
#                         cbs[k].dispatchEvent(new MouseEvent('click', {bubbles:true}));
#                     }
#                 }
#             })()
#         """)
#
#         # 等待验证结果
#         for _ in range(6):
#             time.sleep(1)
#             if sb.execute_script(_ALTCHA_SOLVED_JS):
#                 print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮）")
#                 return True
#
#         print(f"  ⚠️ 第 {attempt + 1} 轮未通过，重试...")
#         # 重新获取坐标（iframe 可能已重新渲染）
#         try:
#             new_coords = sb.execute_script(_ALTCHA_EXPAND_JS)
#             if new_coords:
#                 coords = new_coords
#         except Exception:
#             pass
#
#     print("  ❌ ALTCHA 3 轮均失败")
#     return False


def _submit_renew(sb):
    """点击模态框内的 Renew 提交按钮"""
    print("🖱️  点击模态框中的 Renew 按钮...")
    try:
        submit = sb.find_element('div.modal-footer button.btn.btn-primary', timeout=10)
        submit.click()
    except Exception:
        sb.execute_script("""
            (function(){
                var m = document.querySelector('button.btn.btn-primary');
                if (!m) return;
                var bs = m.querySelectorAll('button');
                for (var i = 0; i < bs.length; i++)
                    if (/renew/i.test(bs[i].textContent)) bs[i].click();
            })()
        """)
    time.sleep(8)


def _check_renew_result(sb):
    """读取页面 alert 提示，判断续期结果并推送 TG 通知"""
    print("\n📋 检查续期结果...")
    alert_text = _read_alert(sb)
    if not alert_text:
        time.sleep(3)
        alert_text = _read_alert(sb)

    if alert_text:
        print(f"📩 页面提示: {alert_text}")
        # 统一处理直引号与排版引号（’ U+2019 / ‘ U+2018）
        low = alert_text.lower().replace("\u2019", "'").replace("\u2018", "'")

        # 否定语义必须优先判定，否则 "not renewed" 会被下面的 "renewed" 误判成功
        neg_keys = (
            "can't renew", "cannot renew", "could not renew", "couldn't renew",
            "not renewed", "unable to renew", "failed", "error", "too early",
            "not yet", "not allowed",
        )
        pos_keys = ("renewed", "success", "extended")

        if any(k in low for k in neg_keys):
            # 未到续期时间 vs 其他失败，分别给出不同提示
            if any(k in low for k in ("can't renew", "cannot renew", "too early",
                                      "not yet", "not allowed")):
                send_tg_message("⏳", "未到续期时间", alert_text)
            else:
                send_tg_message("❌", "续期失败", alert_text)
        elif any(k in low for k in pos_keys):
            send_tg_message("✅", "续期成功", alert_text)
        else:
            send_tg_message("ℹ️", "续期操作已执行", alert_text)
    else:
        print("ℹ️ 未检测到明确的提示框，可能续期操作未生效")
        send_tg_message("ℹ️", "未检测到明确提示", "请手动确认续期结果")


def renew_server(sb):
    """登录成功后调用：自动进入详情页 -> Renew -> ALTCHA -> 提交

    返回 True 表示「提交动作已执行」，False 表示流程中断（未找到详情页/Renew 按钮）。
    注意：具体续期成败仍以 _check_renew_result 的页面提示为准。
    """
    print("\n" + "#" * 25)
    print("  开始自动续期流程")
    print("#" * 25)

    if not _goto_server_detail(sb):
        print("❌ 未能进入服务器详情页，续期流程中断")
        send_tg_message("❌", "未能进入服务器详情页")
        return False

    if not _open_renew_modal(sb):
        print("❌ 未能打开 Renew 弹窗（可能未到续期时间）")
        send_tg_message("❌", "未能打开 Renew 弹窗（可能未到续期时间）")
        return False

    # altcha_ok = _solve_altcha(sb)
    # if not altcha_ok:
    #     print("⚠️ ALTCHA 验证未通过，仍尝试提交 Renew...")

    _submit_renew(sb)
    _check_renew_result(sb)
    return True


#  脚本执行入口 (可选代理)
def main():
    print("#" * 25)
    print("   katabump 自动登录续期")
    print("#" * 25)

    accounts = _load_accounts()
    if not accounts:
        print("❌ 未配置账号：请设置 KATABUMP_ACCOUNTS（JSON 数组），"
              "或 KATABUMP_EMAIL + KATABUMP_PASSWORD")
        send_tg_message("❌", "未配置账号")
        sys.exit(1)
    print(f"👥 待处理账号数: {len(accounts)}")

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_str = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1081"
    sb_kwargs = {"uc": True, "headless": False}

    if IS_PROXY:
        print(f"🔗 挂载代理: {proxy_str}")
        sb_kwargs["proxy"] = proxy_str
    else:
        print("🌐 未使用代理，直连访问")

    results = []
    for idx, (email, password) in enumerate(accounts, 1):
        set_current_account(email, password)
        print("\n" + "=" * 25)
        print(f"  [{idx}/{len(accounts)}] {_mask_email(email)}")
        print("=" * 25)

        # 每个账号一个独立的临时 profile 目录，退出后删除
        profile_dir = os.path.join(
            tempfile.gettempdir(), f"katabump_profile_{uuid.uuid4().hex[:12]}"
        )

        ok = False
        try:
            print("🚀 启动浏览器...")
            # user_data_dir 指向独立目录：seleniumbase 不会复用它，也不会自动删
            # 退出 with 块时浏览器自动关闭，下个账号拿到全新 profile
            with SB(user_data_dir=profile_dir, **sb_kwargs) as sb:
                try:
                    sb.open("https://api.ip.sb/ip")
                    print(f"📍  当前出口IP: {sb.get_text('body')}")
                except Exception:
                    pass

                if login(sb):
                    ok = renew_server(sb)   # 返回 True 才表示续期流程走完
                else:
                    print("\n❌ 登录失败，终止该账号后续续期操作。")
                    send_tg_message("❌", "登录失败")
        except Exception as e:
            # 单个账号异常不影响后续账号；异常文本可能含明文密码，需脱敏
            safe = _safe_error(e, password)
            print(f"❌ 账号 {_mask_email(email)} 运行异常: {safe}")
            send_tg_message("❌", f"运行异常: {safe}")
        finally:
            # 无论成功失败，都清理本账号的 profile；清理异常不得中断后续账号
            try:
                _reset_browser_state(profile_dir)
            except Exception as ce:
                print(f"⚠️ 清理 profile 时出错（已忽略）: {ce}")

        results.append((email, ok))

    # 汇总（ok 表示「登录成功并已执行续期流程」，具体续期结果见各账号的 TG 通知）
    ok_cnt = sum(1 for _, o in results if o)
    print("\n" + "#" * 25)
    print(f"  汇总：登录成功 {ok_cnt} / 共 {len(results)}")
    for email, ok in results:
        print(f"   {'✅' if ok else '❌'} {_mask_email(email)}")
    print("#" * 25)

    # 全部账号失败时以非零退出码结束，让 Actions 标记为失败
    if ok_cnt == 0:
        print("❌ 所有账号均未成功，退出码 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
