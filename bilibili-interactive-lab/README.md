# 视频交互实验室 · Bilibili Interactive Lab

输入 B 站链接、上传媒体或粘贴字幕，让 AI 把视频中的方法转成可交互、可离线使用的 HTML。

这是在自己电脑运行的个人工具。**生成、保存、公开分享是三个独立动作**：生成后保存到本机，不会自动公开。

## 功能

- 优先获取字幕，无字幕时尝试下载音频并转写。
- 支持粘贴文本、上传视频或音频，跳过 B 站获取。
- 支持 DeepSeek、OpenAI 和豆包 API，先设计交互再生成网页。
- 本机历史、隔离预览、下载字幕与独立 HTML。

AI 生成的内容与功能需要自己验收，尤其是按钮、计算、计时、记录与手机布局。转写和视频观点可能有错误。

## 安装与启动

建议 Python 3.11 或 3.12，确保使用较新的 yt-dlp。准备自己模型服务的 API Key；普通聊天账号不等于 API 账号。

下载源码 ZIP 并解压，或用 VS Code 的 `Git: Clone` 克隆仓库。在包含本 README、requirements.txt 和 app 文件夹的目录打开终端。

### macOS / Linux

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000/>。停止时在终端按 `Ctrl+C`。以后不必重新安装：macOS / Linux 激活环境后运行最后一条命令，Windows 运行最后一条命令。

## 第一次使用

1. 在页面选择模型服务，填写自己账号可用的模型名称或接入点 ID 和 API Key，保存配置。
2. 测试连接会消耗少量 API 额度。
3. 输入链接，点击“先获取视频内容”，检查字幕；也可上传媒体或粘贴文本。独立获取内容不要求生成模型配置。
4. 填写期望的交互方式，点击“用这份内容生成网页”或“一键生成交互网页”。生成会把文本和要求发送给所选模型服务，使用 API 额度。
5. 预览并验收，下载 HTML。需要公开时，另行上传成品至 GitHub Pages。

DeepSeek 生成采用非深度思考模式减少等待；先提炼设计，再生成紧凑 HTML。耗时取决于模型、网络与作品复杂度。

## 字幕、登录与转写

- B 站账号不是必需的。部分字幕需要登录，可主动保存自己的 SESSDATA 并检查登录。登录也不保证解除 412 限制；失败时上传已取得的媒体或粘贴文本，避免连续重复请求。
- 本机 Whisper 默认 Base 模型，首次从 Hugging Face 下载，之后在 CPU 本机转写，无需转写 API Key。
- OpenAI 转写可以复用 OpenAI 生成密钥或填写独立密钥。音频会发送至 OpenAI 并使用 API 额度；DeepSeek 密钥不能用于该转写。
- FFmpeg 可由 imageio-ffmpeg 包提供，无需单独安装系统版本。
- 支持普通 B 站链接与 b23.tv 短链接，目前只处理第一分 P。
- 上传上限 200 MB、两小时；生成文本上限 4 万字符；一次一个任务。

## 核心文件与依赖

| 文件 | 作用 |
| --- | --- |
| app/main.py | 后台接口、任务流程、保存、预览与下载 |
| app/services.py | 模型调用、提示词、本机配置 |
| app/media.py | 字幕、下载与转写 |
| static/ | HTML、CSS、JavaScript 界面，无需 React 或 Node 构建 |
| requirements.txt | FastAPI、Uvicorn、httpx、yt-dlp、faster-whisper、imageio-ffmpeg、python-dotenv |
| tests/ | 流程与数据隔离测试 |

测试：`python -m unittest discover -s tests -v`。

## 本机数据与隐私

每个使用者配置自己的账号。运行时自动建立 data 目录：

- data/config.json：生成模型配置与密钥。
- data/media-config.json：转写配置与密钥。
- data/bilibili-cookies.txt：B 站登录凭证。
- data/jobs/：字幕、设计与 HTML 历史。
- data/uploads/：上传媒体。
- data/models/：本机转写模型。

密钥不回传页面，不写进生成 HTML；B 站登录不发送给模型。预览隔离 iframe 中可能无法使用本地存储，可使用作品提供的导出功能。

**不要直接分享整个运行目录。** 分享源码只包含 app、static、tests、requirements.txt、README.md、.gitignore，以及空的 data/.gitkeep。不包含其他 data 文件、.env、虚拟环境、日志或 .git 历史。测试中的 test-secret 等均为虚构测试值。

.gitignore 会忽略本机数据，但不能保护已经提交过的凭证。若曾误上传，应立即使凭证失效并清理历史。公开成品之前，也应检查文字是否适合分享。

## GitHub Pages 与多人使用

仓库用于下载源码；GitHub Pages 用于托管已生成的 HTML。**Pages 无法运行 Python 后台**：下载本工具的人需在自己电脑启动它。

当前后台仅监听 127.0.0.1。多人在线生成需要服务器部署、用户登录、数据隔离与额度控制，再开放到公网。
