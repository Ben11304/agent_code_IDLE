# {{project_name}} — Agent system

{{project_description}}

Hệ multi-agent điều phối qua AgentUI. Mỗi agent là context boundary tự đủ,
giao tiếp qua manifest (không đọc internals của nhau).

## Agents
{{agent_table}}

## Cấu trúc
```
<project>/
├── .agentui/project.yaml     # khai báo graph agent (id, role, model, parents)
├── shared/                    # luật chung mọi agent đọc ở pre-flight
│   ├── research_integrity.md
│   ├── tool_conventions.md    # rtk + aas
│   ├── handoff_schema.md      # format manifest = contract giữa agent
│   ├── scope_decisions.md     # quyết định scope đã đóng (frozen)
│   └── glossary.md            # tên authoritative
├── sync.sh                    # pre-flight: copy producer/outputs → consumer/inputs
└── <AGENT>/                   # mỗi agent
    ├── AGENT.md               # system prompt (NOTICE→PRE-FLIGHT→Role→…→Quy tắc cứng)
    ├── inputs/manifest.md     # contract nhận từ producer (synced)
    ├── outputs/manifest.md    # artifact xuất cho downstream (versioned)
    ├── context/code_map.md    # file agent sở hữu/tham chiếu
    └── state/progress.md      # log mỗi turn (prepend)
```

## Quy tắc vận hành
- Giao tiếp duy nhất qua manifest; bump version khi đổi contract.
- Pre-flight `bash sync.sh <AGENT>` mỗi session trước khi action.
- Thiếu thông tin → DỪNG, escalate (xem `shared/research_integrity.md`).
