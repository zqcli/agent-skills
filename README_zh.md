# Agent Skills

**中文** | [English](./README.md)

个人 [Agent Skills](https://agentskills.io/) 集合——一种标准化格式，用于为 AI agent 提供专业知识和工作流扩展能力。

## 目录结构

每个 skill 存放在独立的目录中：

```
agent-skills/
├── README.md
├── README_zh.md
├── my-skill/
│   ├── SKILL.md          # 必须：元数据 + 指令
│   ├── scripts/          # 可选：可执行代码
│   ├── references/       # 可选：文档
│   └── assets/           # 可选：模板、资源
└── another-skill/
    └── SKILL.md
```

## Skill 格式

每个 skill **必须**包含一个 `SKILL.md` 文件，包含 YAML 前置元数据和 Markdown 正文：

```markdown
---
name: skill-name
description: 描述此 skill 的功能及适用场景。
---

# 指令

Agent 的逐步操作指令...
```

### 前置元数据字段

| 字段             | 必填 | 说明                                                                |
| --------------- | ---- | ------------------------------------------------------------------- |
| `name`          | 是   | 1–64 字符，小写字母、数字和连字符。必须与目录名一致。               |
| `description`   | 是   | 1–1024 字符。描述 skill 的功能及适用时机。                           |
| `license`       | 否   | 许可证名称或指向附带许可证文件的引用。                               |
| `compatibility` | 否   | 环境要求说明（1–500 字符）。                                         |
| `metadata`      | 否   | 附加属性的键值对映射。                                                |
| `allowed-tools` | 否   | 预授权工具列表，空格分隔（实验性）。                                   |

### 可选目录

- **`scripts/`** — Agent 可运行的可执行代码（Python、Bash、JS 等）
- **`references/`** — 按需加载的补充文档
- **`assets/`** — 模板、图片、数据文件

### 编写建议

- `SKILL.md` 控制在 500 行以内，将详细参考内容移至单独文件。
- 引用其他文件时使用相对于 skill 根目录的相对路径。
- 文件引用保持一层深度，避免深层嵌套引用链。
- 在描述中包含具体关键词，帮助 agent 识别相关任务。

## 验证

使用 [skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref) 验证你的 skill：

```bash
skills-ref validate ./my-skill
```

## 规范

完整规范：[agentskills.io/specification](https://agentskills.io/specification)

## 许可证

MIT