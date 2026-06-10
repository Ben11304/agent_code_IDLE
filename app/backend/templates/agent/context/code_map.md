# {{id}} Code map

Files/modules agent này sở hữu hoặc tham chiếu.

## Owned (agent là author/maintainer — được ghi)
- `./AGENT.md` — role + contract (ít sửa).
- `./inputs/manifest.md` — synced bởi `sync.sh`, không sửa tay.
- `./outputs/manifest.md` — bump theo Bump rule sau mỗi artifact.
- `./state/progress.md` — prepend mỗi turn có nghĩa.
- `./context/code_map.md` — file này; cập nhật khi scope mở rộng.
{{owned_extra}}

## Read-only references (consume, không sửa)
- `../shared/` — research_integrity, tool_conventions, handoff_schema,
  scope_decisions, glossary.
- Upstream agents liệt kê trong `./inputs/manifest.md`.

## Out of scope (KHÔNG đụng)
- Folder của agent khác.
- File config ở project root trừ khi liệt kê dưới Owned.
