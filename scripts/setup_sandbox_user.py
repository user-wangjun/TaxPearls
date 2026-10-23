"""创建判题沙箱专用低权限账号（Windows，一次性，需管理员运行）。

用法（管理员 PowerShell/CMD）：
    .venv\\Scripts\\python.exe scripts\\setup_sandbox_user.py

做三件事：
1. 创建本地用户 tpjudge（随机密码，密码不过期、用户不可改密）；
2. 从 Users 组移除（可选动作，若系统策略强制 Users 则保留）；
3. 打印需要写入系统服务环境的两个变量（写入启动环境的 .env 或服务配置）：
       TAXPEARLS_SANDBOX_USER=tpjudge
       TAXPEARLS_SANDBOX_PASSWORD=...
未配置这两个变量时，判题回退普通路径（功能可用，边界同 v2）。
账号用途单一：仅用于 CreateProcessWithLogonW 运行学生代码，可随时删除：
    net user tpjudge /delete
"""
from __future__ import annotations

import secrets
import subprocess
import sys

USER = "tpjudge"


def main() -> int:
    password = secrets.token_urlsafe(18)
    for args in (
        ["net", "user", USER, password, "/add",
         "/passwordchg:no", "/passwordreq:yes", "/comment:TaxPearls-judge-sandbox"],
        ["wmic", "useraccount", "where", f"name='{USER}'",
         "set", "PasswordExpires=false"],
    ):
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"命令失败: {' '.join(args)}\n{proc.stdout}{proc.stderr}", file=sys.stderr)
            return 1
    # 尝试移出 Users 组（部分系统强制保留，失败不影响使用）
    subprocess.run(["net", "localgroup", "Users", USER, "/delete"],
                   capture_output=True, text=True)
    print(f"已创建受限账号 {USER}。请把以下两行写入系统服务启动环境：")
    print(f"TAXPEARLS_SANDBOX_USER={USER}")
    print(f"TAXPEARLS_SANDBOX_PASSWORD={password}")
    print("注意：密码只显示这一次，请立即妥善保存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
