#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bilibili-login: B站扫码登录，自动获取字幕所需 cookies.txt
==========================================================
零外部软件、零 MCP：纯 Python 调用 B站官方 passport 扫码登录接口。

流程：
  1. 生成登录二维码 → 保存 login_qr.png 并自动打开
  2. 你用手机 B站 App 扫码（自己的账号，建议小号）
  3. 脚本自动轮询登录状态，成功即捕获 SESSDATA 等 cookie
  4. 写入 cookies.txt（Netscape 格式），可直接用于 fetch_subtitles.py

说明：
  - 扫码登录是 B站官方登录方式，属于用户本人账号的正常登录行为
  - SESSDATA 有效期约 6 个月，期间无需重新登录
  - 密码不经脚本处理（不输入、不存储），账号安全由 B站官方扫码机制保证

用法：
  python bilibili_login.py [--out cookies.txt]
"""
import argparse
import os
import sys
import time
import webbrowser

import requests
import qrcode

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
GEN_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

POLL_CODES = {0: "登录成功", 86101: "等待扫码", 86090: "已扫码，请在手机上确认", 86038: "二维码已过期"}


def generate_qr(session):
    r = session.get(GEN_API, headers=HEADERS, timeout=15)
    d = r.json()
    if d.get("code") != 0:
        raise SystemExit(f"[失败] 二维码生成失败：{d.get('message')}")
    data = d["data"]
    return data["url"], data["qrcode_key"]


def poll_login(qrcode_key, session):
    r = session.get(POLL_API, params={"qrcode_key": qrcode_key, "source": "main-fe-header"},
                    headers=HEADERS, timeout=15)
    return r.json()


def extract_cookies(d, session):
    """从 poll 成功响应提取 cookie：新版在 data.cookie_info.cookies，旧版走 Set-Cookie。"""
    ck = {}
    data = d.get("data") or {}
    info = data.get("cookie_info") or {}
    for c in info.get("cookies", []):
        if c.get("name"):
            ck[c["name"]] = c.get("value", "")
    for k, v in session.cookies.items():
        ck.setdefault(k, v)
    return {k: v for k, v in ck.items()
            if k in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5",
                     "buvid3", "buvid4", "b_nut", "sid")}


def to_netscape(cookies_dict):
    lines = ["# Netscape HTTP Cookie File", ""]
    for k, v in cookies_dict.items():
        lines.append(f".bilibili.com\tTRUE\t/\tTRUE\t0\t{k}\t{v}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="B站扫码登录获取 cookies.txt")
    ap.add_argument("--out", default="cookies.txt", help="输出路径（默认 cookies.txt）")
    args = ap.parse_args()

    print("[1/3] 正在生成登录二维码 ...")
    session = requests.Session()
    session.headers.update(HEADERS)
    url, qrcode_key = generate_qr(session)
    print("  [调试] 匿名 cookie:", {k: v[:12] for k, v in session.cookies.items()})

    qr_path = os.path.abspath("login_qr.png")
    img = qrcode.make(url)
    img.save(qr_path)
    print(f"  二维码已保存：{qr_path}")
    try:
        webbrowser.open(f"file:///{qr_path.replace(os.sep, '/')}")
    except Exception:
        pass
    print("  请在浏览器中查看二维码（或手动打开上面的文件），用手机 B站 App 扫一扫登录")
    print("  （建议使用小号；登录一次有效期约 6 个月）")

    last_code = None
    deadline = time.time() + 300
    while time.time() < deadline:
        d = poll_login(qrcode_key, session)
        data = d.get("data") or {}
        code = data.get("code", d.get("code"))  # 真实状态在 data.code（顶层 code=0 仅表示接口调用成功）
        if code != last_code:
            print(f"  [{POLL_CODES.get(code, code)}]", flush=True)
            last_code = code
        if code == 0:
            break
        if code == 86038:
            print("  二维码过期，重新生成 ...")
            url, qrcode_key = generate_qr(session)
            qrcode.make(url).save(qr_path)
            last_code = None
        time.sleep(2)
    else:
        raise SystemExit("[失败] 扫码超时（5 分钟），请重试。")

    # 提取 cookie：新版接口在 data.cookie_info.cookies
    ck = extract_cookies(d, session)
    if not ck.get("SESSDATA"):
        # 诊断：打印响应结构（定位 cookie 位置）
        data = d.get("data") or {}
        print("  [诊断] data:", {k: (str(v)[:60]) for k, v in data.items()})
        print("  [诊断] session cookies:", {k: v[:12] for k, v in session.cookies.items()})
        # 兜底：重定向确认（旧版流程），跟随 data.url 触发 Set-Cookie
        try:
            url = data.get("url")
            if url:
                rr = session.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
                print(f"  [诊断] 跟随 url -> HTTP {rr.status_code}，cookies: {list(session.cookies.keys())}")
                ck = extract_cookies(d, session)
        except Exception as e:
            print(f"  [诊断] 跟随 url 失败: {e}")
    if not ck.get("SESSDATA"):
        raise SystemExit("[失败] 未捕获到 SESSDATA（登录可能未成功）")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(to_netscape(ck))
    os.remove(qr_path) if os.path.exists(qr_path) else None
    print(f"\n[完成] 登录成功！cookies 已写入：{os.path.abspath(args.out)}")
    print(f"  SESSDATA: {ck['SESSDATA'][:16]}...（{len(ck['SESSDATA'])} 字符）")
    print("  现在可以运行：python crawl_bilibili_daily.py --ps 20 --cookies cookies.txt --out-dir subs/")


if __name__ == "__main__":
    main()
