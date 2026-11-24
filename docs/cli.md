# 从命令行使用

回到 [安装与部署](setup.md)，接口见 [api.md](api.md)。

## 命令行和 MCP

`cubes` 用后端自己的 HTTP 接口驱动正在运行的团队，适合脚本、终端和 Claude Desktop / Cursor 这类 MCP 客户端。它没有自己的数据，也不能替你点确认卡。

```bash
cd backend
scripts/cubes status         # 后端、模型、正在运行的任务和等你决定的事
scripts/cubes bots           # 机器人和群组（名字、ID、私聊会话）
scripts/cubes send 主管 "你好" --wait   # 交一句话，等它做完并打印回复
scripts/cubes send "内容组" "整理这周的稿子" --json
scripts/cubes result <run id>  # 这次任务的状态和最终回复
scripts/cubes runs           # 最近的任务
```

`backend/scripts/cubes` 用 `backend/.venv` 里的 Python 运行 `backend/cubes_cli.py`；把 `backend/scripts` 加进 `PATH`（或做个软链接）就能直接叫 `cubes`，在 `backend/` 里 `python -m cubes_cli` 等价。桌面版构建出来的后端没有这个脚本，直接用同一份 `cubes_cli.py` 也行。

- 连接：桌面版每次启动都会写 `~/Library/Application Support/com.ice.cubes.desktop/cli.json`（0600，`{"url": …, "token": …}`，退出时删掉），命令行默认读它，其次读 `~/.cubes/cli.json`。连服务器时用 `CUBES_URL` 加 `CUBES_TOKEN_FILE`（文件里只放令牌）——没有 `--token` 参数，因为 argv 别的进程也看得到；`CUBES_CLI_CONFIG` 可以指定另一个配置文件，空字符串表示不读文件。
- 退出码：0 完成；1 后端拒绝或任务没有成功结束；2 用法或名字不对；3 任务停在确认卡或问题上等你；4 等超时（任务会继续跑，用 `cubes result` 再看）；5 连不上后端。
- `--json` 只输出一个 JSON 文档：`send` 带 `run`、`reply`、`waiting`，`result` 带 `run` 和 `reply`。
- `cubes mcp` 在 stdio 上提供一个 MCP 服务，只有四个工具：`list_bots`、`send_task`、`wait_run`、`get_result`。它不能同意卡片、不能删除东西、不返回任何密钥，也不操作电脑；等用户回答的事只能在 Cubes 窗口里做。

Claude Desktop（`~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "cubes": {
      "command": "/Users/you/Documents/AI-Native/cubes/cubes/backend/scripts/cubes",
      "args": ["mcp"]
    }
  }
}
```

Cursor 的 MCP 设置里填同样的命令和参数；连服务器时在 `env` 里加 `CUBES_URL` 和 `CUBES_TOKEN_FILE`。
