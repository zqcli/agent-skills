# ssh-exec 测试规范

> 修改 `ssh-exec.sh` 后执行 `bash tests/run-tests.sh` 回归验证，将结果回填至本文档的「最后结果」列。

---

## 测试环境

> 运行测试前，设置以下环境变量配置测试目标：
> ```bash
> export TEST_SERVER=<server-ip>
> export TEST_PORT=<server-port>
> export TEST_USER=<ssh-user>
> export TEST_KEY_PATH=<path-to-ssh-key>
> export TEST_PASSWORD=<key-passphrase-or-password>
> export TEST_SOCKS_PROXY=<socks5-proxy-addr>
> export TEST_HTTP_PROXY=<http-proxy-addr>
> ```
> 然后执行 `bash tests/run-tests.sh`

| 项目 | 值 |
|------|-----|
| 脚本路径 | `bash ssh-exec.sh`（从 skill 根目录执行） |
| 目标服务器 | 由 `TEST_SERVER` 环境变量指定 |
| SSH 端口 | 由 `TEST_PORT` 环境变量指定 |
| 认证方式 | `-a key` |
| 运行环境 | MSYS2 UCRT64 Bash |

---

## 测试用例

### Group 1 — 参数校验

#### T1.1 缺少必填参数 `-s`

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-u root -c "hostname" -P "test"` |
| **预期结果** | 输出 `Error: Missing required parameters` + usage，退出码 ≠ 0 |
| **测试方案** | 不传 `-s`，验证脚本提前拦截不会尝试连接 |
| **最后结果** | ✅ PASS — `Error: Missing required parameters` + usage，EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.2 缺少必填参数 `-u`

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -c "hostname" -P "test"` |
| **预期结果** | 输出 `Error: Missing required parameters` + usage，退出码 ≠ 0 |
| **测试方案** | 不传 `-u`，验证提前拦截 |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.3 缺少必填参数 `-c`

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -u root -P "test"` |
| **预期结果** | 输出 `Error: Missing required parameters` + usage，退出码 ≠ 0 |
| **测试方案** | 不传 `-c`，验证提前拦截 |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.4 `-s` 缺值

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s -u root -c "hostname" -P "test"` |
| **预期结果** | `Error: -s requires a value`，EXIT=1 |
| **测试方案** | `-s` 后跟下一个 flag，验证不把 flag 当值 |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.5 `-u` 缺值

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -u -c "hostname" -P "test"` |
| **预期结果** | `Error: -u requires a value`，EXIT=1 |
| **测试方案** | `-u` 后跟下一个 flag，验证不把 flag 当用户名 |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.6 `-c` 缺值

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -u root -c -P "test"` |
| **预期结果** | `Error: -c requires a value`，EXIT=1 |
| **测试方案** | `-c` 后跟下一个 flag，验证不把 flag 当命令 |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.7 `-P` 缺值

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -u root -c "hostname" -P` |
| **预期结果** | `Error: -P requires a value`，EXIT=1，不出现 unbound variable 错误 |
| **测试方案** | `-P` 作为最后一个参数，`$2` 为空 — 验证 `${2:-}` 兜底 |
| **最后结果** | ✅ PASS — `Error: -P requires a value`，EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.8 `-a key` 缺少 `-k`

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -u root -a key -c "hostname" -P "test"` |
| **预期结果** | `Error: -k/--key is required when using key authentication`，EXIT=1 |
| **测试方案** | 密钥认证但未提供密钥路径 |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.9 非法认证方式

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -u root -a cert -c "hostname" -P "test" -k /tmp/key` |
| **预期结果** | `Error: Invalid auth method. Use 'key' or 'password'`，EXIT=1 |
| **测试方案** | 传入 `key`/`password` 之外的值 |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.10 未知选项

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -u root -c "hostname" -P "test" --foo` |
| **预期结果** | `Error: Unknown option: --foo` + usage，EXIT=1 |
| **测试方案** | 传入未定义的 flag |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.11 `-p` 缺值

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -u root -p -c "hostname" -P "test"` |
| **预期结果** | `Error: -p requires a value`，EXIT=1 |
| **测试方案** | `-p` 后跟下一个 flag |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.12 `--proxy` 缺值

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -u root -c "hostname" -P "test" --proxy` |
| **预期结果** | `Error: --proxy requires a value`，EXIT=1，不出现 unbound variable 错误 |
| **测试方案** | `--proxy` 作为最后一个参数 |
| **最后结果** | ✅ PASS — `Error: --proxy requires a value`，EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.13 `-k` 缺值

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-s 10.0.0.1 -u root -a key -k -c "hostname" -P "test"` |
| **预期结果** | `Error: -k requires a value`，EXIT=1 |
| **测试方案** | `-k` 后跟下一个 flag |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T1.14 `-h` 帮助

| 项 | 内容 |
|----|------|
| **分类** | 参数校验 |
| **测试命令** | `-h` |
| **预期结果** | 输出 usage，EXIT=0 |
| **测试方案** | 帮助信息正常输出，退出码为 0（非错误） |
| **最后结果** | ✅ PASS — `Usage: ssh-exec.sh [OPTIONS]`，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

---

### Group 2 — 功能测试

#### T2.1 hostname

| 项 | 内容 |
|----|------|
| **分类** | 功能测试 |
| **测试命令** | `-s <test-server> -p <test-port> -u root -a key -k <key> -P <pw> --proxy <proxy> -c "hostname"` |
| **预期结果** | 输出远程主机名 `localhost`，EXIT=0 |
| **测试方案** | 最基本的功能验证 |
| **最后结果** | ✅ PASS — `localhost`，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

#### T2.2 whoami

| 项 | 内容 |
|----|------|
| **分类** | 功能测试 |
| **测试命令** | 同上，`-c "whoami"` |
| **预期结果** | 输出 `root`，EXIT=0 |
| **测试方案** | 验证远端用户身份 |
| **最后结果** | ✅ PASS — `root`，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

#### T2.3 uptime

| 项 | 内容 |
|----|------|
| **分类** | 功能测试 |
| **测试命令** | 同上，`-c "uptime"` |
| **预期结果** | 输出系统运行时间信息，EXIT=0 |
| **测试方案** | 验证多行输出正确传递 |
| **最后结果** | ✅ PASS — 正常 uptime 输出，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

#### T2.4 多词命令（管道）

| 项 | 内容 |
|----|------|
| **分类** | 功能测试 |
| **测试命令** | `-c "ls -la /root | head -5"` |
| **预期结果** | 输出 `/root` 前 5 行文件列表，EXIT=0 |
| **测试方案** | 验证含空格、管道的复杂命令正确传递 |
| **最后结果** | ✅ PASS — 5 行文件列表，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

#### T2.5 退出码传播

| 项 | 内容 |
|----|------|
| **分类** | 功能测试 |
| **测试命令** | `-c "exit 42"` |
| **预期结果** | 无输出，EXIT=42 |
| **测试方案** | **关键**：验证远端退出码正确传播到本地（不是被吞掉） |
| **最后结果** | ✅ PASS — EXIT=42 |
| **最后测试日期** | 2026-04-28 |

#### T2.6 命令执行失败

| 项 | 内容 |
|----|------|
| **分类** | 功能测试 |
| **测试命令** | `-c "nonexistent_command_xyz"` |
| **预期结果** | `command not found`，EXIT=127 |
| **测试方案** | 验证远端错误正确回传 |
| **最后结果** | ✅ PASS — `command not found`，EXIT=127 |
| **最后测试日期** | 2026-04-28 |

---

### Group 3 — 代理测试

#### T3.1 SOCKS5 代理

| 项 | 内容 |
|----|------|
| **分类** | 代理测试 |
| **测试命令** | `--proxy <proxy-addr> --proxy-type socks5 -c "hostname"` |
| **预期结果** | 通过 SOCKS5 成功连接，EXIT=0 |
| **测试方案** | 显式指定 socks5 代理类型 |
| **最后结果** | ✅ PASS — `localhost`，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

#### T3.2 HTTP 代理

| 项 | 内容 |
|----|------|
| **分类** | 代理测试 |
| **测试命令** | `--proxy <proxy-addr> --proxy-type http -c "hostname"` |
| **预期结果** | 通过 HTTP CONNECT 代理成功连接，EXIT=0 |
| **测试方案** | 验证 HTTP 代理类型正常工作 |
| **最后结果** | ✅ PASS — `localhost`，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

---

### Group 4 — 边缘情况

#### T4.1 密码含特殊字符

| 项 | 内容 |
|----|------|
| **分类** | 边缘情况 |
| **测试命令** | `-P '<password>'` (含 `$` `\|` `;` `#` `:` `@` `/` `?`) |
| **预期结果** | 正常认证和执行，不发生 shell 转义问题 |
| **测试方案** | 单引号包裹密码，验证 askpass 脚本正确透传 |
| **最后结果** | ✅ PASS — `password handled correctly`，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

#### T4.2 长管道命令

| 项 | 内容 |
|----|------|
| **分类** | 边缘情况 |
| **测试命令** | `-c "echo A && echo B && echo C && uname -a"` |
| **预期结果** | 全部 4 行输出，包含 kernel 信息，EXIT=0 |
| **测试方案** | 验证复合命令链正确执行 |
| **最后结果** | ✅ PASS — A/B/C + `Linux localhost ... x86_64`，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

#### T4.3 stderr 混合输出

| 项 | 内容 |
|----|------|
| **分类** | 边缘情况 |
| **测试命令** | `-c "echo stdout; echo stderr >&2"` |
| **预期结果** | stdout 和 stderr 均输出（`2>&1` 合并），EXIT=0 |
| **测试方案** | 验证 stderr 不被丢弃 |
| **最后结果** | ✅ PASS — `stdout` + `stderr`，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

#### T4.4 密钥无密码短语

| 项 | 内容 |
|----|------|
| **分类** | 边缘情况 |
| **测试命令** | `-a key -k <key>` 不传 `-P` |
| **预期结果** | `Permission denied (publickey)`，EXIT=255，**不** exit=0 |
| **测试方案** | 验证无密码短语回退路径不会用 `|| true` 吞掉错误（P0 修复后） |
| **最后结果** | ✅ PASS — `Permission denied (publickey)`，EXIT=255 |
| **最后测试日期** | 2026-04-28 |

#### T4.5 完整密钥路径

| 项 | 内容 |
|----|------|
| **分类** | 边缘情况 |
| **测试命令** | `-k "<path-to-key>"` (Windows 绝对路径) |
| **预期结果** | 正常日期输出，EXIT=0 |
| **测试方案** | 验证 Windows 风格路径在 MSYS2 下正常工作 |
| **最后结果** | ✅ PASS — `Tue Apr 28 01:15:27 PM CST 2026`，EXIT=0 |
| **最后测试日期** | 2026-04-28 |

#### T4.6 密码认证无密码

| 项 | 内容 |
|----|------|
| **分类** | 边缘情况 |
| **测试命令** | `-a password` 不传 `-P` |
| **预期结果** | `Error: Password required for password authentication`，EXIT=1 |
| **测试方案** | 密码认证模式但没给密码，应提前报错 |
| **最后结果** | ✅ PASS — EXIT=1 |
| **最后测试日期** | 2026-04-28 |

#### T4.7 默认端口连接失败

| 项 | 内容 |
|----|------|
| **分类** | 边缘情况 |
| **测试命令** | 不传 `-p`（默认 22），连接目标服务器 |
| **预期结果** | 连接超时，EXIT=255（服务器 22 端口不通） |
| **测试方案** | 验证错误退出码为非零（不吞错误） |
| **最后结果** | ✅ PASS — `Connection timed out during banner exchange`，EXIT=255 |
| **最后测试日期** | 2026-04-28 |

---

## 统计

| 分组 | 用例数 | 通过 | 失败 | 跳过 |
|------|--------|------|------|------|
| Group 1 — 参数校验 | 14 | 14 | 0 | 0 |
| Group 2 — 功能测试 | 6 | 6 | 0 | 0 |
| Group 3 — 代理测试 | 2 | 2 | 0 | 0 |
| Group 4 — 边缘情况 | 7 | 7 | 0 | 0 |
| **合计** | **29** | **29** | **0** | **0** |

> 最后更新：2026-04-28
