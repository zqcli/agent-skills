# Agent Skills

[中文](./README_zh.md) | **English**

Personal collection of [Agent Skills](https://agentskills.io/) — a standardized format for extending AI agent capabilities with specialized knowledge and workflows.

## Structure

Each skill lives in its own directory under the repository root:

```
agent-skills/
├── README.md
├── README_zh.md
├── my-skill/
│   ├── SKILL.md          # Required: metadata + instructions
│   ├── scripts/          # Optional: executable code
│   ├── references/       # Optional: documentation
│   └── assets/           # Optional: templates, resources
└── another-skill/
    └── SKILL.md
```


## Skills

| Skill | Version | Description |
|---|---|---|
| [ssh-exec](./ssh-exec/) | 0.7 | Remote SSH command execution with key and password authentication |
| [obsidian-fastnotesync-skill](./obsidian-fastnotesync-skill/) | 1.1.0 | CRUD on remote Obsidian notes via Fast Note Sync Service REST API |
| [obsidian-livesync-skill](./obsidian-livesync-skill/) | 1.4.0 | CRUD on Obsidian LiveSync CouchDB database |

### Changelog

**obsidian-fastnotesync-skill**
- v1.1.0 — Add proxy support (SOCKS5/HTTP) for intranet access
- v1.0.0 — Initial release: CREATE, READ, UPDATE, DELETE, LIST, APPEND, PREPEND, REPLACE, RENAME, FRONTMATTER

**ssh-exec**
- v0.7 — Platform-aware proxy via build_proxy_command()
- v0.5 — Initial release

**obsidian-livesync-skill**
- v1.4.0 — Latest (git submodule)

## Skill Format

Every skill **must** contain a `SKILL.md` file with YAML frontmatter and Markdown body:

```markdown
---
name: skill-name
description: What this skill does and when to use it.
---

# Instructions

Step-by-step instructions for the agent...
```

### Frontmatter Fields

| Field           | Required | Description                                                        |
| --------------- | -------- | ------------------------------------------------------------------ |
| `name`          | Yes      | 1–64 chars, lowercase letters, numbers, and hyphens. Must match directory name. |
| `description`   | Yes      | 1–1024 chars. Describe what the skill does and when to use it.    |
| `license`       | No       | License name or reference to a bundled license file.               |
| `compatibility` | No       | Environment requirements (1–500 chars).                           |
| `metadata`      | No       | Arbitrary key-value mapping for additional properties.             |
| `allowed-tools` | No       | Space-separated list of pre-approved tools (experimental).         |

### Optional Directories

- **`scripts/`** — Executable code that agents can run (Python, Bash, JS, etc.)
- **`references/`** — Additional documentation loaded on demand
- **`assets/`** — Templates, images, data files

### Guidelines

- Keep `SKILL.md` under 500 lines; move detailed references to separate files.
- Use relative paths from the skill root when referencing other files.
- Keep file references one level deep; avoid deeply nested reference chains.
- Write descriptions that include specific keywords to help agents identify relevant tasks.

## Validation

Validate your skills with [skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref):

```bash
skills-ref validate ./my-skill
```

## Specification

Full specification: [agentskills.io/specification](https://agentskills.io/specification)

## License

MIT