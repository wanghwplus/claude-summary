#!/usr/bin/env python3
"""
claude-summary: Claude Code 对话经验自动总结工具

SessionStart hook 触发 → 提取上一次 transcript → 队列化 → claude -p 总结 → 按天保存
周一自动合并周报 + 清理 + 月度归档
"""

import json
import sys
import os
import fcntl
import glob
import hashlib
import time
import subprocess
import shutil
from datetime import datetime, date, timedelta
from pathlib import Path

# ============== 配置 ==============
BASE_DIR = os.environ.get("CLAUDE_SUMMARY_DIR", os.path.expanduser("~/.claude-summary"))
DAILY_DIR = os.path.join(BASE_DIR, "daily")
WEEKLY_DIR = os.path.join(BASE_DIR, "weekly")
MONTHLY_DIR = os.path.join(BASE_DIR, "monthly")
QUEUE_DIR = os.path.join(BASE_DIR, "queue")
SUMMARIZED_DIR = os.path.join(BASE_DIR, ".summarized")
LOCK_FILE = os.path.join(BASE_DIR, ".lock")
LOG_FILE = os.path.join(BASE_DIR, "claude-summary.log")

MIN_MESSAGES = int(os.environ.get("CLAUDE_SUMMARY_MIN_MESSAGES", "4"))
MODEL = os.environ.get("CLAUDE_SUMMARY_MODEL", "claude-sonnet-4-6")

SUMMARY_PROMPT = """你是一个经验总结助手。分析以下 Claude Code 对话记录，提取关键经验。

要求：
1. 只总结有价值的技术经验，忽略闲聊和简单问答
2. 如果对话太简单没有值得记录的经验，只回复一个词 SKIP
3. 用中文输出，简洁有力

格式：
## 📋 对话主题
[一句话描述]

## 🎯 解决的问题
[简要列出]

## 💡 关键经验
[可复用的具体经验]

## 🔧 有用的代码/命令
[如果有值得记录的片段]

## ⚠️ 踩坑记录
[如果有]

对话记录：
"""


def ensure_dirs():
    for d in [DAILY_DIR, WEEKLY_DIR, MONTHLY_DIR, QUEUE_DIR, SUMMARIZED_DIR]:
        os.makedirs(d, exist_ok=True)


def log(msg: str):
    """写日志"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def transcript_hash(path: str) -> str:
    """transcript 路径的哈希，用于标记已总结"""
    return hashlib.md5(path.encode()).hexdigest()[:12]


def is_summarized(transcript_path: str) -> bool:
    """检查是否已经总结过"""
    marker = os.path.join(SUMMARIZED_DIR, transcript_hash(transcript_path))
    return os.path.exists(marker)


def mark_summarized(transcript_path: str):
    """标记为已总结"""
    marker = os.path.join(SUMMARIZED_DIR, transcript_hash(transcript_path))
    Path(marker).touch()


# ============== Transcript 解析 ==============

def count_user_messages(transcript_path: str) -> int:
    count = 0
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "user":
                        count += 1
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return count


def extract_conversation(transcript_path: str, max_chars: int = 20000) -> str:
    """从 JSONL transcript 提取对话文本，去掉工具调用细节"""
    if not os.path.exists(transcript_path):
        return ""

    messages = []
    total_len = 0

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type", "")

                # summary 行（如果有）
                if entry_type == "summary":
                    summary = entry.get("summary", "")
                    if summary:
                        messages.insert(0, f"[对话摘要]: {summary}")
                        total_len += len(summary)
                    continue

                if entry_type not in ("user", "assistant"):
                    continue

                msg = entry.get("message", {})
                content = msg.get("content", [])
                role = "用户" if entry_type == "user" else "Claude"

                if isinstance(content, str):
                    text = content[:600] + "...[截断]" if len(content) > 600 else content
                    messages.append(f"{role}: {text}")
                    total_len += len(text)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        # 只提取文本，跳过 tool_use / tool_result
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if not text:
                                continue
                            if len(text) > 600:
                                text = text[:600] + "...[截断]"
                            messages.append(f"{role}: {text}")
                            total_len += len(text)

                if total_len > max_chars:
                    messages.append("...[对话过长已截断]")
                    break

    except Exception as e:
        log(f"解析 transcript 失败: {e}")
        return ""

    return "\n\n".join(messages)


# ============== 查找上一次 Transcript ==============

def find_project_transcripts(cwd: str) -> list:
    """
    找到当前项目目录对应的所有 transcript 文件。
    Claude Code 将 transcript 存在 ~/.claude/projects/<encoded-path>/ 下。
    """
    claude_projects = os.path.expanduser("~/.claude/projects")
    if not os.path.exists(claude_projects):
        return []

    # Claude Code 用路径编码作为项目目录名（把 / 替换为 -）
    # 尝试匹配 cwd 对应的项目目录
    all_transcripts = []

    for project_dir in os.listdir(claude_projects):
        project_path = os.path.join(claude_projects, project_dir)
        if not os.path.isdir(project_path):
            continue

        # 检查目录名是否与 cwd 相关
        # Claude Code 编码规则：/Users/wang/project → -Users-wang-project
        encoded_cwd = cwd.replace("/", "-")
        if encoded_cwd.startswith("-"):
            encoded_cwd_check = encoded_cwd
        else:
            encoded_cwd_check = "-" + encoded_cwd

        if project_dir == encoded_cwd_check or project_dir.startswith(encoded_cwd_check):
            jsonl_files = glob.glob(os.path.join(project_path, "*.jsonl"))
            all_transcripts.extend(jsonl_files)

    # 如果匹配不到，退回到搜索所有项目
    if not all_transcripts:
        all_transcripts = glob.glob(
            os.path.join(claude_projects, "**", "*.jsonl"), recursive=True
        )

    return all_transcripts


def find_previous_transcript(current_session_id: str, cwd: str) -> str:
    """找到上一次的 transcript（排除当前 session）"""
    transcripts = find_project_transcripts(cwd)
    if not transcripts:
        return ""

    # 排除当前 session 的 transcript
    candidates = [t for t in transcripts if current_session_id not in os.path.basename(t)]

    if not candidates:
        return ""

    # 按修改时间排序，取最近的
    candidates.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    return candidates[0]


# ============== 队列管理 ==============

def enqueue_task(transcript_path: str, cwd: str, session_id: str):
    """写入队列"""
    ensure_dirs()
    task = {
        "transcript_path": transcript_path,
        "cwd": cwd,
        "session_id": session_id,
        "timestamp": time.time(),
        "created_at": datetime.now().isoformat()
    }
    filename = f"{int(time.time())}-{session_id[:8]}.json"
    filepath = os.path.join(QUEUE_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(task, f)
    log(f"入队: {filename} -> {transcript_path}")


def get_pending_tasks() -> list:
    """获取队列中待处理的任务，按时间排序"""
    tasks = []
    for filename in sorted(os.listdir(QUEUE_DIR)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(QUEUE_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                task = json.load(f)
            task["_filepath"] = filepath
            tasks.append(task)
        except Exception:
            continue
    return tasks


def remove_task(task: dict):
    """删除已处理的任务"""
    filepath = task.get("_filepath", "")
    if filepath and os.path.exists(filepath):
        os.remove(filepath)


# ============== 总结 ==============

def summarize_with_claude(conversation: str) -> str:
    """用 claude -p 生成总结"""
    prompt = SUMMARY_PROMPT + conversation
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", MODEL, prompt],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            log(f"claude -p 失败: {result.stderr}")
            return ""
    except FileNotFoundError:
        log("claude 命令未找到")
        return ""
    except subprocess.TimeoutExpired:
        log("claude -p 超时")
        return ""
    except Exception as e:
        log(f"claude -p 异常: {e}")
        return ""


def save_daily(summary: str, session_id: str = ""):
    """保存到当天的 daily 文件"""
    today = date.today().isoformat()
    now = datetime.now().strftime("%H:%M")
    daily_file = os.path.join(DAILY_DIR, f"{today}.md")

    if not os.path.exists(daily_file):
        with open(daily_file, "w", encoding="utf-8") as f:
            f.write(f"# 📅 {today} 经验总结\n")

    with open(daily_file, "a", encoding="utf-8") as f:
        f.write(f"\n---\n\n### ⏰ {now}")
        if session_id:
            f.write(f"  `{session_id[:8]}`")
        f.write(f"\n\n{summary}\n")

    log(f"已保存 daily: {daily_file}")


def process_task(task: dict) -> bool:
    """处理单个总结任务，返回是否成功"""
    transcript_path = task.get("transcript_path", "")
    session_id = task.get("session_id", "")

    if not transcript_path or not os.path.exists(transcript_path):
        log(f"transcript 不存在: {transcript_path}")
        return False

    # 检查是否已总结
    if is_summarized(transcript_path):
        log(f"已总结过，跳过: {transcript_path}")
        return True

    # 检查消息数
    msg_count = count_user_messages(transcript_path)
    if msg_count < MIN_MESSAGES:
        log(f"消息数 {msg_count} < {MIN_MESSAGES}，跳过")
        mark_summarized(transcript_path)
        return True

    # 提取对话
    conversation = extract_conversation(transcript_path)
    if not conversation:
        log("未提取到对话内容")
        mark_summarized(transcript_path)
        return True

    # 调用 claude -p 总结
    log(f"开始总结: {transcript_path}")
    summary = summarize_with_claude(conversation)

    if not summary:
        log("总结失败")
        return False

    if summary.strip() == "SKIP":
        log("对话无需总结")
        mark_summarized(transcript_path)
        return True

    # 保存
    save_daily(summary, session_id)
    mark_summarized(transcript_path)
    return True


# ============== 周一清理与归档 ==============

def is_monday() -> bool:
    return date.today().weekday() == 0


def last_week_range():
    """返回上周一和上周日的日期"""
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def generate_weekly():
    """合并上周的 daily 为周报"""
    last_mon, last_sun = last_week_range()
    weekly_file = os.path.join(WEEKLY_DIR, f"week-{last_mon.isoformat()}.md")

    # 已存在则跳过
    if os.path.exists(weekly_file):
        return

    daily_files = []
    for f in sorted(os.listdir(DAILY_DIR)):
        if not f.endswith(".md"):
            continue
        file_date_str = f.replace(".md", "")
        try:
            file_date = date.fromisoformat(file_date_str)
        except ValueError:
            continue
        if last_mon <= file_date <= last_sun:
            daily_files.append(os.path.join(DAILY_DIR, f))

    if not daily_files:
        log("上周无 daily 记录，跳过周报")
        return

    with open(weekly_file, "w", encoding="utf-8") as wf:
        wf.write(f"# 📊 周报: {last_mon.isoformat()} ~ {last_sun.isoformat()}\n")
        for df in daily_files:
            wf.write("\n")
            with open(df, "r", encoding="utf-8") as f:
                wf.write(f.read())

    log(f"周报已生成: {weekly_file}")

    # 删除上周的 daily 文件
    for df in daily_files:
        os.remove(df)
        log(f"已清理 daily: {df}")


def cleanup_summarized():
    """清理上周的已总结标记"""
    one_week_ago = time.time() - 7 * 86400
    for f in os.listdir(SUMMARIZED_DIR):
        filepath = os.path.join(SUMMARIZED_DIR, f)
        try:
            if os.path.getmtime(filepath) < one_week_ago:
                os.remove(filepath)
        except Exception:
            pass


def cleanup_queue():
    """清理超过一周的队列残留"""
    one_week_ago = time.time() - 7 * 86400
    for f in os.listdir(QUEUE_DIR):
        filepath = os.path.join(QUEUE_DIR, f)
        try:
            if os.path.getmtime(filepath) < one_week_ago:
                os.remove(filepath)
        except Exception:
            pass


def archive_monthly():
    """如果上个月结束了，把上个月的 weekly 移到 monthly 目录"""
    today = date.today()
    if today.day > 7:
        # 只在月初前7天执行
        return

    last_month = today.replace(day=1) - timedelta(days=1)
    last_month_str = last_month.strftime("%Y-%m")
    month_dir = os.path.join(MONTHLY_DIR, last_month_str)

    moved = False
    for f in os.listdir(WEEKLY_DIR):
        if not f.endswith(".md"):
            continue
        # week-2025-01-06.md → 提取日期
        try:
            week_date_str = f.replace("week-", "").replace(".md", "")
            week_date = date.fromisoformat(week_date_str)
        except ValueError:
            continue

        if week_date.strftime("%Y-%m") == last_month_str:
            os.makedirs(month_dir, exist_ok=True)
            src = os.path.join(WEEKLY_DIR, f)
            dst = os.path.join(month_dir, f)
            shutil.move(src, dst)
            log(f"月度归档: {f} -> {month_dir}/")
            moved = True

    if moved:
        log(f"月度归档完成: {last_month_str}")


def monday_maintenance():
    """周一维护任务"""
    if not is_monday():
        return

    # 用标记文件避免同一天重复执行
    marker = os.path.join(BASE_DIR, f".maintenance-{date.today().isoformat()}")
    if os.path.exists(marker):
        return

    log("执行周一维护...")
    generate_weekly()
    cleanup_summarized()
    cleanup_queue()
    archive_monthly()
    Path(marker).touch()

    # 清理旧的 maintenance 标记
    for f in glob.glob(os.path.join(BASE_DIR, ".maintenance-*")):
        try:
            fname = os.path.basename(f)
            d = date.fromisoformat(fname.replace(".maintenance-", ""))
            if d < date.today() - timedelta(days=7):
                os.remove(f)
        except Exception:
            pass

    log("周一维护完成")


# ============== 主流程：Hook 入口 ==============

def hook_main():
    """SessionStart hook 入口"""
    ensure_dirs()

    # 读取 hook JSON
    hook_data = {}
    try:
        if not sys.stdin.isatty():
            hook_data = json.load(sys.stdin)
    except Exception:
        pass

    session_id = hook_data.get("session_id", f"manual-{int(time.time())}")
    cwd = hook_data.get("cwd", os.getcwd())

    # 找上一次的 transcript
    transcript_path = find_previous_transcript(session_id, cwd)
    if not transcript_path:
        log("未找到上一次的 transcript")
        return

    if is_summarized(transcript_path):
        log(f"上一次对话已总结过: {transcript_path}")
        # 仍然检查周一维护
        monday_maintenance()
        return

    # 入队
    enqueue_task(transcript_path, cwd, session_id)

    # 尝试获取锁并处理队列
    process_queue()


def process_queue():
    """尝试获取锁，处理队列中所有任务"""
    lock_fd = None
    try:
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        # 没拿到锁，有其他进程在处理
        log("锁被占用，退出")
        if lock_fd:
            lock_fd.close()
        return

    try:
        # 循环处理直到队列空
        while True:
            tasks = get_pending_tasks()
            if not tasks:
                break

            task = tasks[0]
            success = process_task(task)
            remove_task(task)

            if not success:
                log(f"任务处理失败: {task.get('transcript_path', '?')}")

        # 队列处理完，执行周一维护
        monday_maintenance()

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


# ============== CLI 入口 ==============

def cli_show(target: str = "today"):
    """查看记录"""
    ensure_dirs()
    if target == "today":
        f = os.path.join(DAILY_DIR, f"{date.today().isoformat()}.md")
        if os.path.exists(f):
            print(open(f, encoding="utf-8").read())
        else:
            print("📭 今天没有记录")
    elif target == "week":
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        for f in sorted(os.listdir(DAILY_DIR)):
            if not f.endswith(".md"):
                continue
            try:
                fd = date.fromisoformat(f.replace(".md", ""))
                if fd >= monday:
                    print(f"\n{'━' * 40} {fd} {'━' * 40}")
                    print(open(os.path.join(DAILY_DIR, f), encoding="utf-8").read())
            except ValueError:
                continue
    elif target == "all":
        # daily
        for f in sorted(os.listdir(DAILY_DIR)):
            if f.endswith(".md"):
                print(f"\n{'━' * 40} {f} {'━' * 40}")
                print(open(os.path.join(DAILY_DIR, f), encoding="utf-8").read())
        # weekly
        for f in sorted(os.listdir(WEEKLY_DIR)):
            if f.endswith(".md"):
                print(f"\n{'━' * 40} {f} {'━' * 40}")
                print(open(os.path.join(WEEKLY_DIR, f), encoding="utf-8").read())
        # monthly
        for month_dir in sorted(os.listdir(MONTHLY_DIR)):
            month_path = os.path.join(MONTHLY_DIR, month_dir)
            if os.path.isdir(month_path):
                for f in sorted(os.listdir(month_path)):
                    if f.endswith(".md"):
                        print(f"\n{'━' * 40} {month_dir}/{f} {'━' * 40}")
                        print(open(os.path.join(month_path, f), encoding="utf-8").read())
    else:
        # 尝试作为日期
        f = os.path.join(DAILY_DIR, f"{target}.md")
        if os.path.exists(f):
            print(open(f, encoding="utf-8").read())
        else:
            print(f"❌ 找不到 {target} 的记录")


def cli_list():
    """列表概览"""
    ensure_dirs()
    print("📁 记录列表:\n")

    daily_files = sorted(glob.glob(os.path.join(DAILY_DIR, "*.md")))
    if daily_files:
        print("📅 每日:")
        for f in daily_files:
            d = os.path.basename(f).replace(".md", "")
            try:
                with open(f, encoding="utf-8") as fh:
                    n = fh.read().count("### ⏰")
            except Exception:
                n = 0
            print(f"  {d}  ({n} 条)")

    weekly_files = sorted(glob.glob(os.path.join(WEEKLY_DIR, "*.md")))
    if weekly_files:
        print("\n📊 本月周报:")
        for f in weekly_files:
            print(f"  {os.path.basename(f).replace('.md', '')}")

    monthly_dirs = sorted(glob.glob(os.path.join(MONTHLY_DIR, "*")))
    if monthly_dirs:
        print("\n📦 月度归档:")
        for md in monthly_dirs:
            if os.path.isdir(md):
                count = len(glob.glob(os.path.join(md, "*.md")))
                print(f"  {os.path.basename(md)}  ({count} 个周报)")


def cli_search(keyword: str):
    """搜索"""
    ensure_dirs()
    found = False
    for search_dir in [DAILY_DIR, WEEKLY_DIR]:
        for f in sorted(glob.glob(os.path.join(search_dir, "*.md"))):
            try:
                content = open(f, encoding="utf-8").read()
                lines = content.split("\n")
                for i, line in enumerate(lines, 1):
                    if keyword.lower() in line.lower():
                        fname = os.path.basename(f)
                        print(f"  {fname}:{i}: {line.strip()}")
                        found = True
            except Exception:
                continue

    # 搜索月度归档
    for month_dir in sorted(glob.glob(os.path.join(MONTHLY_DIR, "*"))):
        if os.path.isdir(month_dir):
            for f in sorted(glob.glob(os.path.join(month_dir, "*.md"))):
                try:
                    content = open(f, encoding="utf-8").read()
                    lines = content.split("\n")
                    for i, line in enumerate(lines, 1):
                        if keyword.lower() in line.lower():
                            rel = os.path.relpath(f, BASE_DIR)
                            print(f"  {rel}:{i}: {line.strip()}")
                            found = True
                except Exception:
                    continue

    if not found:
        print("未找到匹配结果")


def cli_status():
    """显示状态"""
    ensure_dirs()
    queue_count = len([f for f in os.listdir(QUEUE_DIR) if f.endswith(".json")])
    summarized_count = len(os.listdir(SUMMARIZED_DIR))
    lock_active = False
    try:
        fd = open(LOCK_FILE, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    except (IOError, OSError):
        lock_active = True

    print(f"📊 claude-summary 状态")
    print(f"  存储目录:     {BASE_DIR}")
    print(f"  队列中:       {queue_count} 个任务")
    print(f"  已总结:       {summarized_count} 个对话")
    print(f"  总结模型:     {MODEL}")
    print(f"  正在处理:     {'是' if lock_active else '否'}")
    print(f"  今天是周一:   {'是' if is_monday() else '否'}")

    if os.path.exists(LOG_FILE):
        print(f"\n📝 最近日志:")
        try:
            with open(LOG_FILE, encoding="utf-8") as f:
                lines = f.readlines()
                for line in lines[-10:]:
                    print(f"  {line.rstrip()}")
        except Exception:
            pass


def usage():
    print("""📘 claude-summary - Claude Code 对话经验自动总结工具

用法:
  claude-summary hook              SessionStart hook 入口（自动调用）
  claude-summary show [today|week|all|日期]  查看记录
  claude-summary list              列表概览
  claude-summary search <关键词>    搜索
  claude-summary status            查看状态
  claude-summary maintenance       手动执行周一维护""")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"

    if cmd == "hook":
        hook_main()
    elif cmd == "show":
        cli_show(args[1] if len(args) > 1 else "today")
    elif cmd == "list":
        cli_list()
    elif cmd == "search":
        if len(args) < 2:
            print("❌ 请提供关键词")
            sys.exit(1)
        cli_search(args[1])
    elif cmd == "status":
        cli_status()
    elif cmd == "maintenance":
        monday_maintenance()
    elif cmd in ("help", "--help", "-h"):
        usage()
    else:
        print(f"❌ 未知命令: {cmd}")
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
