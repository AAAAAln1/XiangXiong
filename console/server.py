"""
Xiangxiong Console - Backend Server (Chat UI Version)
聊天式界面，纯展示层，不干扰工作流
"""
import asyncio
import json
import os
from pathlib import Path
from typing import Optional, Set
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# 尝试加载配置
try:
    from config import CLAUDE_COMMAND, WORKING_DIR, HOST, PORT
except ImportError:
    CLAUDE_COMMAND = "claude"
    WORKING_DIR = None
    HOST = "127.0.0.1"
    PORT = 8000

app = FastAPI()

# WebSocket连接池
active_connections: Set[WebSocket] = set()


class RuntimeClient:
    """直接调用Claude Code，不抽象"""
    
    def __init__(self):
        self.process: Optional[asyncio.subprocess.Process] = None
    
    async def start(self):
        """启动Claude Code进程"""
        work_dir = WORKING_DIR if WORKING_DIR else str(Path(__file__).parent.parent)
        
        try:
            self.process = await asyncio.create_subprocess_exec(
                CLAUDE_COMMAND,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir
            )
            print(f"[Console] Started process: {CLAUDE_COMMAND}")
            print(f"[Console] Working directory: {work_dir}")
        except FileNotFoundError as e:
            print(f"[Console] ERROR: Cannot find command '{CLAUDE_COMMAND}'")
            print(f"[Console] Please edit console/config.py")
            raise
    
    async def send_input(self, text: str):
        """发送输入"""
        if self.process and self.process.stdin:
            self.process.stdin.write(text.encode('utf-8'))
            await self.process.stdin.drain()
    
    async def read_output(self):
        """读取输出（非阻塞）"""
        if self.process and self.process.stdout:
            try:
                data = await asyncio.wait_for(
                    self.process.stdout.read(8192),
                    timeout=0.1
                )
                if data:
                    return data.decode('utf-8', errors='replace')
            except asyncio.TimeoutError:
                pass
        return None
    
    async def read_error(self):
        """读取错误输出（非阻塞）"""
        if self.process and self.process.stderr:
            try:
                data = await asyncio.wait_for(
                    self.process.stderr.read(8192),
                    timeout=0.1
                )
                if data:
                    return data.decode('utf-8', errors='replace')
            except asyncio.TimeoutError:
                pass
        return None
    
    def is_running(self) -> bool:
        """检查进程是否运行"""
        return self.process is not None and self.process.returncode is None
    
    async def stop(self):
        """停止进程"""
        if self.process:
            self.process.terminate()
            await self.process.wait()


class MemoryWatcher(FileSystemEventHandler):
    """监听Memory.md文件变化"""
    
    def __init__(self, memory_path: Path):
        self.memory_path = memory_path
        self.last_content = ""
        
    def on_modified(self, event):
        """文件修改时触发"""
        if event.src_path == str(self.memory_path):
            # 读取新内容并广播
            try:
                content = self.memory_path.read_text(encoding='utf-8')
                if content != self.last_content:
                    self.last_content = content
                    asyncio.create_task(broadcast_memory_update(content))
            except Exception as e:
                print(f"[Console] Error reading memory.md: {e}")


async def broadcast_memory_update(content: str):
    """广播Memory更新到所有连接"""
    message = json.dumps({
        "type": "memory_update",
        "content": content
    })
    for ws in active_connections:
        try:
            await ws.send_text(message)
        except:
            pass


async def broadcast_message(message: str):
    """广播消息到所有连接"""
    for ws in active_connections:
        try:
            await ws.send_text(message)
        except:
            pass


@app.get("/")
async def get_index():
    """返回Console主页"""
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding='utf-8'))
    else:
        return HTMLResponse(
            content="<h1>Error: index.html not found</h1>",
            status_code=500
        )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket端点"""
    await websocket.accept()
    active_connections.add(websocket)
    
    # 创建Runtime客户端
    runtime = RuntimeClient()
    
    try:
        # 启动Claude Code
        await runtime.start()
        await websocket.send_text(json.dumps({
            "type": "system",
            "content": "Claude Code started"
        }))
        
        # 发送初始Memory内容
        memory_path = Path(__file__).parent.parent / ".claude" / "memory.md"
        if memory_path.exists():
            content = memory_path.read_text(encoding='utf-8')
            await websocket.send_text(json.dumps({
                "type": "memory_update",
                "content": content
            }))
        
        # 启动Memory监听
        observer = Observer()
        memory_handler = MemoryWatcher(memory_path)
        observer.schedule(memory_handler, str(memory_path.parent), recursive=False)
        observer.start()
        
        # 双向转发
        async def forward_output():
            """转发stdout到WebSocket"""
            while runtime.is_running():
                # 读取stdout
                output = await runtime.read_output()
                if output:
                    await websocket.send_text(json.dumps({
                        "type": "agent_output",
                        "content": output
                    }))
                
                # 读取stderr
                error = await runtime.read_error()
                if error:
                    await websocket.send_text(json.dumps({
                        "type": "agent_output",
                        "content": error
                    }))
                
                await asyncio.sleep(0.05)
        
        async def forward_input():
            """转发WebSocket输入到stdin"""
            while True:
                data = await websocket.receive_text()
                await runtime.send_input(data)
        
        # 并发运行
        await asyncio.gather(
            forward_output(),
            forward_input()
        )
    
    except WebSocketDisconnect:
        print("[Console] WebSocket disconnected")
    except Exception as e:
        print(f"[Console] Error: {e}")
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "content": str(e)
            }))
        except:
            pass
    finally:
        # 清理
        active_connections.discard(websocket)
        await runtime.stop()
        try:
            observer.stop()
            observer.join()
        except:
            pass
        print("[Console] Claude Code stopped")


@app.get("/api/artifacts")
async def get_artifacts():
    """获取Artifacts列表"""
    results_dir = Path(__file__).parent.parent / "results"
    artifacts = []
    
    if results_dir.exists():
        for target_dir in results_dir.iterdir():
            if target_dir.is_dir():
                # 查找所有artifacts
                for file in target_dir.rglob("*"):
                    if file.is_file():
                        artifacts.append({
                            "name": file.name,
                            "path": str(file.relative_to(results_dir))
                        })
    
    return {"items": artifacts}


@app.get("/api/artifact/{path:path}")
async def get_artifact_content(path: str):
    """获取Artifact内容"""
    results_dir = Path(__file__).parent.parent / "results"
    file_path = results_dir / path
    
    if file_path.exists() and file_path.is_file():
        try:
            content = file_path.read_text(encoding='utf-8')
            return {"content": content}
        except:
            return {"error": "Cannot read file"}
    else:
        return {"error": "File not found"}


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "service": "xiangxiong-console"}


if __name__ == "__main__":
    import uvicorn
    print("[Console] Starting Xiangxiong Console")
    print(f"[Console] Server: http://{HOST}:{PORT}")
    print(f"[Console] Command: {CLAUDE_COMMAND}")
    print("[Console] Chat UI - Pure Presentation Layer")
    print()
    uvicorn.run(app, host=HOST, port=PORT)
