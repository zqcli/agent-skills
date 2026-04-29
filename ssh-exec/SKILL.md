---
name: ssh-exec
description: 在远程服务器执行 SSH 命令，支持密钥密码短语和密码认证。使用 SSH_ASKPASS 机制，适用于 MSYS2 UCRT64 / Git Bash 环境。
license: MIT
compatibility: opencode
allowed-tools: Bash(scripts/ssh-exec.sh:*)
metadata:
    version: "0.7"
  platform: windows, macos, linux
  category: remote-execution
  auth-methods: key, password
  environments: public, enterprise
  runtime: bash
---

# SSH Exec 技能

SSH 远程执行工具，使用 SSH_ASKPASS 机制实现自动密码输入。

---

## 功能特性

- **双认证方式**：
  1. SSH 密钥 + 密码短语
  2. 传统用户名/密码
  
- **代理支持**：通过 connect.exe 支持 SOCKS5 和 HTTP 代理
- **干净输出**：自动过滤 SSH 警告信息
- **参数化设计**：无硬编码服务器信息
- **原生 Bash**：无需 PowerShell，适合 MSYS2 UCRT64 环境

---

## 前置条件

### 必需
- **OpenSSH**：MSYS2 UCRT64 自带
- **Bash**：MSYS2 UCRT64 环境

### 代理连接需要
- **connect.exe**：Git for Windows / MSYS2 自带 (mingw64/bin)

---

## 参数说明

| 参数 | 必需 | 默认值 | 描述 |
|------|------|--------|------|
| `-s, --server` | 是 | - | SSH 服务器地址或 IP |
| `-u, --user` | 是 | - | SSH 用户名 |
| `-c, --command` | 是 | - | 要执行的远程命令 |
| `-p, --port` | 否 | 22 | SSH 端口 |
| `-a, --auth` | 否 | key | 认证方式：`key` 或 `password` |
| `-k, --key` | 否* | - | SSH 私钥路径（key认证时必需） |
| `-P, --password` | 否 | - | 登录密码或密钥密码短语 |
| `--proxy` | 否 | - | 代理地址（格式：host:port） |
| `--proxy-type` | 否 | socks5 | 代理类型：`socks5` 或 `http` |
| `-t, --timeout` | 否 | 30 | 连接超时时间（秒） |
| `-h, --help` | 否 | - | 显示帮助信息 |

---

## 使用示例

### 方式一：SSH 密钥 + 密码短语

```bash
# 基本用法（密钥无密码短语）
scripts/ssh-exec.sh -s example.com -u admin -k "$USERPROFILE/.ssh/id_rsa" -c "hostname"

# 带密码短语
scripts/ssh-exec.sh -s example.com -u admin -k "$USERPROFILE/.ssh/id_ed25519" -P "passphrase" -c "uptime"

# 通过 SOCKS5 代理
scripts/ssh-exec.sh -s example.com -u admin -k "$USERPROFILE/.ssh/id_rsa" -P "passphrase" --proxy 127.0.0.1:1080 -c "whoami"
```

### 方式二：传统密码登录

```bash
# 基本用法
scripts/ssh-exec.sh -s example.com -u admin -a password -P "your-password" -c "hostname"

# 自定义端口
scripts/ssh-exec.sh -s example.com -u admin -p 443 -a password -P "your-password" -c "ls -la"

# 通过 HTTP 代理
scripts/ssh-exec.sh -s example.com -u admin -a password -P "your-password" --proxy proxy.example.com:8080 --proxy-type http -c "uptime"
```

### 实际案例

```bash
# 连接内网服务器（密码认证）
scripts/ssh-exec.sh -s <server-ip> -u <user> -p <port> -a password -P "<password>" -c "hostname && whoami"

# 连接外网服务器（密钥认证+代理）
scripts/ssh-exec.sh -s <server-ip> -u <user> -p <port> -P "<passphrase>" --proxy <proxy-addr> -c "hostname"

# 长时间脚本（实时输出）
scripts/ssh-exec.sh -s example.com -u admin -c "for i in 1 2 3; do echo line-\$i; sleep 1; done"
```

---

## 安全建议

### SSH 密钥认证
1. **密码短语来源**：通过参数传递，避免硬编码
2. **密钥权限**：确保私钥仅当前用户可读
   ```bash
   chmod 600 ~/.ssh/id_rsa
   ```

### 密码认证
1. **避免硬编码**：动态传递密码，不要写在脚本中
2. **使用环境变量**：将密码存储在环境变量中
   ```bash
   export SSH_PASSWORD="your-password"
    scripts/ssh-exec.sh -s example.com -u admin -a password -P "$SSH_PASSWORD" -c "hostname"
   ```

---

## 代理配置

### 平台感知

代理工具由 `build_proxy_command()` 函数自动检测：

| 平台 | 代理工具 | ProxyCommand 示例 |
|------|---------|-------------------|
| Windows (MSYS2) | `connect.exe` | `connect.exe -S proxy:1080 %h %p` |
| macOS / Debian / Ubuntu | `nc` (netcat-openbsd) | `nc -X 5 -x proxy:1080 %h %p` |

如果没有检测到代理工具，脚本会报错并给出安装指引。

### 用户责任
- 用户自行负责启动和维护代理服务
- 技能不会启动/停止代理

### 代理使用
```bash
# SOCKS5 代理
scripts/ssh-exec.sh -s example.com -u admin -P "password" --proxy 127.0.0.1:1080 -c "hostname"

# HTTP 代理
scripts/ssh-exec.sh -s example.com -u admin -P "password" --proxy proxy.corp.com:8080 --proxy-type http -c "hostname"
```

---

## 文件结构

```
ssh-exec/
├── SKILL.md               # 本文档
├── scripts/
│   └── ssh-exec.sh        # 主脚本（Bash）
└── tests/
    ├── test-spec.md       # 测试规范
    └── run-tests.sh       # 自动化测试 runner
```

---

## 工作原理

### SSH_ASKPASS 机制

脚本使用 OpenSSH 原生的 SSH_ASKPASS 机制实现密码输入：

```
1. 创建临时 askpass 脚本
2. 设置环境变量：
   - SSH_ASKPASS=/tmp/tmp.XXXXXX
   - SSH_ASKPASS_PASSWORD=<password>
   - SSH_ASKPASS_REQUIRE=force
   - DISPLAY=dummy:0
3. SSH 需要密码时调用 askpass 脚本
4. 脚本返回密码
5. SSH 完成认证
6. 清理临时文件
```

**优势**：
- OpenSSH 原生支持
- 不依赖 pty/tt 欺骗
- 在 MSYS2 / Git Bash 环境下稳定工作
- 比 sshpass 更可靠

---

## 故障排查

### Permission denied (publickey,password)
**原因**：
1. 密钥未找到
2. 密码短语错误
3. 公钥未添加到服务器

**解决方案**：检查密钥路径和密码短语

### connect.exe not found
**原因**：mingw64/bin 目录不在 PATH 中

**解决方案**：
```bash
export PATH="/mingw64/bin:$PATH"
```

### 连接超时
**原因**：
1. 服务器不可达
2. 防火墙阻止
3. 代理未运行

**解决方案**：检查网络连接和代理状态

### 密码认证失败
**原因**：SSH_ASKPASS 机制未正确触发

**解决方案**：
```bash
# 确保设置了密码
scripts/ssh-exec.sh -s server -u user -a password -P "password" -c "command"

# 检查 DISPLAY 变量
echo $DISPLAY  # 应该显示 :0 或类似值
```

---

## 方式对比

| 特性 | SSH 密钥 + 密码短语 | 传统密码 |
|------|---------------------|----------|
| 安全性 | 更高 | 较低 |
| 设置 | 需要生成密钥 | 无需设置 |
| 自动化 | SSH_ASKPASS 机制 | SSH_ASKPASS 机制 |
| 推荐场景 | 生产环境 | 开发/测试 |

---

## 注意事项

- 技能**不管理** SSH 密钥 - 用户负责密钥生成和分发
- 技能**不启动/停止**代理 - 用户自行维护代理服务
- 密码/密码短语应动态传递，不要硬编码
- 输出已过滤 SSH 警告，结果更干净

---

## 边缘情况

| 场景 | 行为 | 退出码 |
|------|------|--------|
| 密钥需要密码短语但未提供 `-P` | SSH 认证失败，输出 `Permission denied (publickey)` | 255 |
| `connect.exe` 不在 PATH 中 | ProxyCommand 失败，SSH 报连接错误 | 255 |
| 密码包含特殊字符（`$` `\|` `;` `#` `@` `/` `?`） | 单引号包裹正确透传，askpass 脚本安全处理 | 0 |
| 同时指定 `-a password` 和 `-k` | `-k` 被忽略，使用密码认证 | 0/255 |
| 端口为非数字 | SSH 报错 `Bad port number` | 255 |
| 代理未运行 | 连接超时（默认 30 秒） | 255 |
| 远程命令执行失败 | 错误通过 stderr 返回，退出码正确传播 | 远程退出码 |
| `-P` 或 `--proxy` 作为最后一个参数无值 | 输出 `Error: <flag> requires a value` | 1 |
| 必填参数缺失（`-s`/`-u`/`-c`） | 输出 usage 并提示缺失项 | 1 |
| `-a password` 未提供 `-P` | 输出 `Error: Password required for password authentication` | 1 |
| 密钥文件不可读或不存在 | 输出 `Error: SSH key not found` | 1 |

---

## 技术细节

### 为什么不用 sshpass

在 MSYS2 / Git Bash 环境下，`sshpass` 与 `/usr/bin/ssh` 组合存在兼容性问题：

```text
debug1: read_passphrase: can't open /dev/tty: No such device or address
```

**原因**：
- `sshpass` 通过 pty 欺骗工作
- Git Bash / MSYS2 的伪终端处理与 Windows 原生不同
- 导致密码注入失败

**解决方案**：
- 使用 SSH_ASKPASS（OpenSSH 原生机制）
- 或使用 Windows 原生 OpenSSH (`/c/Windows/System32/OpenSSH/ssh.exe`)

本技能选择 **SSH_ASKPASS**，因为：
1. OpenSSH 官方支持
2. 在 MSYS2 UCRT64 环境下稳定
3. 无需切换 ssh 二进制

---

## 版本历史

- **3.2**：代理工具平台感知，封装 `build_proxy_command()` 函数，macOS/Linux 使用 `nc`，Windows 使用 `connect.exe`
- **3.1**：修复两个关键bug：路径转换使用 `$USERPROFILE`、持续输出改为实时流式输出
- **3.0**：完全重构为 Bash 版本，使用 SSH_ASKPASS 机制，适配 MSYS2 UCRT64 环境
- **2.0**：添加密码认证支持，移除硬编码服务器信息
- **1.0**：初始版本，支持 SSH_ASKPASS 密钥认证