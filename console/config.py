# Xiangxiong Console 配置

# Claude Code 命令路径
# 根据你的环境修改

# 选项1: 如果claude在PATH中
# CLAUDE_COMMAND = "claude"

# 选项2: 完整路径
# CLAUDE_COMMAND = "C:/path/to/claude.exe"

# 选项3: 临时测试 - 使用Python echo脚本
# 这只是为了测试UI，不是真正的Claude
CLAUDE_COMMAND = "claude"
# 需要创建一个简单的echo脚本来模拟对话

# 工作目录（通常是xiangxiong根目录）
WORKING_DIR = None  # None表示使用默认（console的父目录）

# 服务器配置
HOST = "127.0.0.1"
PORT = 8000
