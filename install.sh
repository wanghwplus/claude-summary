#!/bin/bash
set -euo pipefail
GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo -e "${CYAN}🚀 安装 claude-summary...${NC}\n"

# 1. 检查 claude 命令
if ! command -v claude &>/dev/null; then
    echo -e "${RED}❌ 未找到 claude 命令，请先安装 Claude Code${NC}"
    exit 1
fi
echo -e "${GREEN}✅ 检测到 claude 命令${NC}"

# 2. 安装主脚本
INSTALL_DIR="$HOME/.local/share/claude-summary"
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/claude-summary.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/claude-summary.py"
echo -e "${GREEN}✅ 主脚本 → $INSTALL_DIR/${NC}"

# 3. 创建 CLI 入口
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/claude-summary" << WRAPPER
#!/bin/bash
exec python3 "$INSTALL_DIR/claude-summary.py" "\$@"
WRAPPER
chmod +x "$HOME/.local/bin/claude-summary"
echo -e "${GREEN}✅ CLI → ~/.local/bin/claude-summary${NC}"

# 4. 初始化目录
python3 "$INSTALL_DIR/claude-summary.py" hook < /dev/null 2>/dev/null || true
mkdir -p "$HOME/.claude-summary/"{daily,weekly,monthly,queue,.summarized}
echo -e "${GREEN}✅ 目录已初始化 → ~/.claude-summary/${NC}"

# 5. 配置 Hook
SETTINGS="$HOME/.claude/settings.json"

# hook 命令
HOOK_CMD="python3 $INSTALL_DIR/claude-summary.py hook &"

if [[ -f "$SETTINGS" ]]; then
    if grep -q "claude-summary" "$SETTINGS" 2>/dev/null; then
        echo -e "${YELLOW}⚠️ Hook 已配置，跳过${NC}"
    else
        echo -e "${YELLOW}⚠️ 已有 settings.json，请手动添加 hook 配置:${NC}"
        echo ""
        echo -e "  在 Claude Code 中运行 ${CYAN}/hooks${NC} 添加:"
        echo -e "  事件: ${CYAN}SessionStart${NC}"
        echo -e "  Matcher: ${CYAN}startup${NC}"
        echo -e "  命令: ${CYAN}${HOOK_CMD}${NC}"
        echo ""
        echo -e "  或手动编辑 ${SETTINGS} 合并以下内容:"
        echo ""
        cat << HOOKJSON
  {
    "hooks": {
      "SessionStart": [
        {
          "matcher": "startup",
          "hooks": [
            {
              "type": "command",
              "command": "${HOOK_CMD}",
              "timeout": 10
            }
          ]
        }
      ]
    }
  }
HOOKJSON
    fi
else
    mkdir -p "$HOME/.claude"
    cat > "$SETTINGS" << SETTINGSJSON
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "${HOOK_CMD}",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
SETTINGSJSON
    echo -e "${GREEN}✅ Hook → ~/.claude/settings.json${NC}"
fi

# 6. PATH 检查
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo -e "\n${YELLOW}⚠️ 请添加到 PATH:${NC}"
    echo '  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc && source ~/.bashrc'
fi

echo -e "\n${CYAN}━━━ 安装完成 ━━━${NC}\n"
cat << 'USAGE'
🔄 自动模式:
  每次 /new 开新对话，自动在后台总结上一次对话
  总结保存到 ~/.claude-summary/daily/
  周一自动生成周报 + 清理 + 月度归档

📖 查看命令:
  claude-summary show today     今天
  claude-summary show week      本周
  claude-summary list           列表
  claude-summary search '词'    搜索
  claude-summary status         状态

⚙️ 环境变量:
  CLAUDE_SUMMARY_DIR            存储目录（默认 ~/.claude-summary）
  CLAUDE_SUMMARY_MIN_MESSAGES   最少对话轮次（默认 4）
USAGE
