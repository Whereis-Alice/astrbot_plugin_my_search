# 开发与验证

插件要求 Python 3.10+、AstrBot >=4.16,<5，运行时额外依赖见 `requirements.txt`。

在隔离的开发环境中安装依赖和 AstrBot SDK，再从仓库根目录执行：

```sh
python -m pip install -r requirements.txt 'astrbot>=4.16,<5'
python -m unittest discover -s tests -v
```

测试使用真正的 AstrBot 指令过滤器、FunctionTool、消息组件和 OneBot 适配器，搭配本地模拟协议端与 HTTP 服务；不需要聊天账号或 Anysearch API Key，不会向真实群聊发送消息。

覆盖合并转发的群聊与私聊行为、发送失败回退、普通文本平台、空参数与多词参数、三个 AI 工具的 `call()` 路径、输出切分与截断、批量参数验证、URL 验证、缓存与并发请求合并、HTTP/JSON-RPC/SSE 错误处理，以及卸载清理。

`main.py` 负责插件配置、指令和发消息；`search_service.py` 统一参数校验与输出整理；`client.py` 管理 HTTP、缓存和资源释放；`tools.py` 定义独立命名的 AI 工具。

Logo 的 SVG 源文件位于 `assets/logo.svg`。若安装 Pillow，可运行 `python scripts/build_logo.py` 重新生成插件根目录的 `logo.png`；Pillow 不是插件运行时依赖。

上游基础版本：AgIzT/astrbot_plugin_anysearch，commit `983a7acb2f6940ab441a521be684085e0913738e`（v0.3）。改造内容包括独立标识、显式转发与失败回退、当前 API 参数适配、修复 `run()`/`call()` 不匹配、多文本块读取、失败结果识别、缓存和并发控制。
