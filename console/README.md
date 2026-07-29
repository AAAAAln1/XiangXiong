# 象雄 Console - Chat UI

**纯展示层，不干扰工作流**

类似Claude Desktop的聊天界面，专为象雄设计。

## 特性

✅ **聊天式界面**
- 用户消息（蓝色气泡）
- Agent消息（灰色气泡）
- 系统消息（绿色）

✅ **Memory.md实时预览**
- 自动监听文件变化
- Markdown渲染
- 代码高亮

✅ **Artifacts浏览**
- 列出所有artifacts
- 点击查看内容

✅ **100%被动展示**
- 不解析语义
- 不改变输出
- 不控制流程

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置Claude命令

编辑 `config.py`：

```python
# 设置你的Claude命令路径
CLAUDE_COMMAND = "claude"

# 或完整路径
# CLAUDE_COMMAND = "C:/path/to/claude.exe"

# 测试用（不运行真实Claude）
# CLAUDE_COMMAND = "powershell"
```

### 3. 启动

从xiangxiong根目录：
```bash
start_console.bat
```

或直接：
```bash
cd console
python server.py
```

### 4. 打开浏览器

访问 http://localhost:8000

## 界面预览

```
┌──────────────────────────────────────────────┐
│ 象雄 Xiangxiong              ● Connected    │
├────────────────────┬─────────────────────────┤
│                    │                         │
│  聊天窗口          │  Memory.md 实时预览     │
│                    │                         │
│  You:              │  ## Current Hypothesis  │
│  测试这个网站      │  H3: ...                │
│                    │                         │
│  Xiangxiong:       │  ## Experiments         │
│  我将检查...       │  ✓ Test A               │
│                    │  □ Test B               │
│                    │                         │
├────────────────────┴─────────────────────────┤
│ 输入消息...                          [发送]  │
└──────────────────────────────────────────────┘

下方Tab: [Memory] [Artifacts]
```

## 核心原则

**纯展示层：**
- ✅ 美化排版
- ✅ Markdown渲染
- ✅ 语法高亮
- ✅ 实时更新

**不干扰：**
- ❌ 不解析语义
- ❌ 不改变输出
- ❌ 不控制流程
- ❌ 不使用LLM/ML

## 技术栈

**Backend:**
- FastAPI + WebSocket
- Watchdog（文件监听）
- JSON消息格式

**Frontend:**
- Vanilla JavaScript（无框架）
- Marked.js（Markdown渲染）
- Highlight.js（代码高亮）

## 消息格式

Backend发送JSON消息到Frontend：

```json
{
  "type": "agent_output",
  "content": "Agent输出内容"
}

{
  "type": "memory_update",
  "content": "Memory.md完整内容"
}

{
  "type": "system",
  "content": "系统消息"
}
```

Frontend发送纯文本到Backend（透传给Claude）。

## 文件结构

```
console/
├── index.html       # 聊天界面
├── server.py        # WebSocket服务器 + Memory监听
├── config.py        # 配置（Claude命令路径）
├── requirements.txt # Python依赖
└── README.md        # 本文件
```

## 与象雄的关系

**Console不是象雄的一部分，只是展示层。**

```
你 → Console界面 → WebSocket → Claude Code → 象雄

Console做的：
- 美化显示
- Markdown渲染
- Memory实时预览

Console不做的：
- 不改变象雄行为
- 不解析Memory结构
- 不控制测试流程
```

## 故障排除

### Cannot find command 'claude'

编辑 `config.py`，设置正确的Claude路径。

### Memory.md不更新

检查文件路径是否正确：`.claude/memory.md`

### WebSocket断开

检查Claude进程是否正常运行。

## 限制

1. **单用户**：一次只支持一个连接
2. **本地运行**：仅localhost
3. **无历史**：刷新页面丢失聊天历史
4. **依赖Claude**：需要能运行的Claude命令

## 未来功能（Phase 2）

如果Phase 0验证成功：
- [ ] 聊天历史持久化
- [ ] Artifact内容查看器
- [ ] Target切换器
- [ ] 搜索功能

## License

与象雄主项目相同。
