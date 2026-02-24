# claude-summary

Claude Code 对话经验自动总结工具。每次开新对话时，自动在后台总结上一次对话的经验教训，按天保存。

## 工作原理

```
Session A (对话中...)
    ↓
/new 开新对话
    ↓
Session B 启动 → SessionStart hook 触发
    ↓
后台：找到 Session A 的 transcript → 入队
    ↓
获取锁 → claude -p 总结 → 保存到 daily/
    ↓
检查是否周一 → 合并周报 + 清理 + 归档
```

## 并发安全

多个 tmux pane 同时 `/new` 时：
- 每个 hook 只是把任务写入 `queue/` 目录（一个文件一个任务）
- 第一个拿到 `flock` 锁的进程处理所有队列任务
- 其他进程发现锁被占用直接退出
- 先到的进程会循环处理完所有积压任务后才释放锁

## 安装

```bash
bash install.sh
```

不需要 API key，使用已授权的 `claude -p` 命令生成总结。

## 目录结构

```
~/.claude-summary/
├── daily/              # 本周的每日记录
│   ├── 2025-02-23.md
│   └── 2025-02-24.md
├── weekly/             # 本月的周报
│   └── week-2025-02-17.md
├── monthly/            # 历史归档
│   ├── 2025-01/
│   │   ├── week-2025-01-06.md
│   │   └── week-2025-01-13.md
│   └── 2025-02/
├── queue/              # 待处理队列
├── .summarized/        # 已总结标记
└── claude-summary.log  # 日志
```

## 自动维护（每周一）

- 合并上周 daily → 周报
- 删除上周 daily 文件
- 清理上周的 `.summarized/` 标记
- 清理 `queue/` 超一周的残留
- 如果上月结束，将上月 weekly 移入 `monthly/YYYY-MM/`

## 命令

```bash
claude-summary show today         # 今日记录
claude-summary show week          # 本周记录
claude-summary show 2025-02-20   # 指定日期
claude-summary show all           # 所有记录
claude-summary list               # 列表概览
claude-summary search "Next.js"   # 搜索
claude-summary status             # 状态信息
claude-summary maintenance        # 手动执行周一维护
```

## 配置

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `CLAUDE_SUMMARY_DIR` | 存储目录 | `~/.claude-summary` |
| `CLAUDE_SUMMARY_MIN_MESSAGES` | 最少消息数 | `4` |

## Hook 配置

安装脚本会自动配置。手动配置方式：

在 Claude Code 中运行 `/hooks`，添加：
- 事件: `SessionStart`
- Matcher: `startup`
- 命令: `python3 ~/.local/share/claude-summary/claude-summary.py hook &`
