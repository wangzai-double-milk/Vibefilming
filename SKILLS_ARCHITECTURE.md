# Git 本地版的 Skills 扩展

系统提示词只保留业务硬约束与 `{{SKILLS_INDEX}}`、`{{TOOLS_INDEX}}` 占位符。
`film/skill_registry.py` 从 `skills/*/SKILL.md` 的 frontmatter 读取元信息，
`agentmain.py` 按任务 description 动态匹配，按需读取正文。Skill 正文消费中立
产物合同，不写运行环境根路径，也不要求读取另一个 Skill。

新增目录即可扩展，无需修改系统提示词、路由表或现有 Skill：

```text
skills/my_feature/
├── SKILL.md
├── references/          可选，按需加载的领域手册
└── scripts/             可选，独立执行入口
```

```yaml
---
name: my-feature
description: 当用户需要某项新能力时使用。负责输出对应产物，不接管其他阶段。
runtime: local
entrypoint: scripts/main.py
requirements: requirements.txt
---
```

`name` 与 `description` 必填。`runtime` 可省略（通用），或设为 `local`；
`entrypoint` 与 `requirements` 均可省略，若声明必须是真实存在的包内相对文件，
不得使用绝对路径、父目录或越界符号链接。入口只进入索引，匹配任务且读取 Skill
调用合同后才执行，不在启动时运行或自动安装依赖。声明重名或非法入口会明确报错。
无有效元信息的散文/SOP 不进入路由。

本地工具在 `film/tools.py` 用 `@film_tool` 声明，自动注册 handler 与 schema。
新增原生工具无需手写系统提示词或 JSON 速查表。独立脚本型 Skill 则维护自己的
调用合同与依赖，使用当前项目路径作为输入，不硬编码源码安装位置。

制作总控维护阶段、执行模式与审批；专业 Skill 只维护对应判断和产物。
中立合同由 `film/artifact_contracts.py` 定义，仍保留完整母片时间轴、单变量锁定、
简笔画箭头故事板真实绑定、原音轨和接缝审查。当前本地执行器提供
`environment_check`、`generation_unit_status`、`project_file_list` 等原生能力，
无需托管环境 CLI。

维护时先更新当前版本自己的 Skill。需要两版共有的业务改进时，分别同步正文，
保留各自的执行入口与环境约定；两版不通过软链接或父目录 import 共享源码。
