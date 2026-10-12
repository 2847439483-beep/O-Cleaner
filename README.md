# O-Cleaner · 磁盘清理助手

Windows 上的磁盘清理与空间占用查看工具，界面为浏览器中打开的本地网页。

- 只使用 Python 标准库，不需要 pip 安装任何依赖
- 不采集使用数据；除用户主动点击「下载 WizTree 便携版」外不发起网络请求
- 默认不删除任何内容，需要手动勾选并二次确认；删除目标受保护名单拦截

详细使用说明见 [docs/USAGE.md](docs/USAGE.md)。

## 主要能力

| 能力 | 说明 |
|---|---|
| 常规清理 | 预定义清理目标与一键预设；每项附带「是什么 / 能否重建 / 删除影响 / 建议保留」说明 |
| 自动发现 | 在所选盘顶层查找被知识库判定为可清理的目录，覆盖预定义清单之外的个人软件缓存 |
| 空间占用查看 | 按体积降序展示整盘占用并支持逐层下钻；可将目录拆分为「可清理」与「不可清理」两部分 |
| Steam 游戏库 | 读取 Steam 的清单文件（不扫描磁盘），列出已安装游戏、体积与最近游玩时间 |
| 卸载残留 | 列出磁盘上存在但 Steam 已无对应记录的目录 |

## 运行要求

- Windows
- Python 3（代码未使用 3.9 及以上版本的语法与标准库 API；CI 环境为 Python 3.12）
- 查看清单、清理用户级缓存不需要提权；清理系统目录（Windows 更新缓存、系统日志等）需要以管理员身份运行

## 快速开始

1. 确认已安装 Python 3（命令行执行 `python --version` 能显示版本号即可）
2. 双击 `start-ocleaner.bat`
3. 浏览器自动打开 `http://127.0.0.1:8770/`

启动后会弹出一个命令行窗口，它是服务进程本身；过程中关闭该窗口会停止服务。

需要管理员权限时，在提权的命令行中执行：

```
cd /d "你的 O-Cleaner 目录"
start-ocleaner.bat
```

命令行参数：

| 参数 | 作用 |
|---|---|
| `--port 9000` | 指定端口（默认 8770，被占用时自动向后查找） |
| `--no-browser` | 启动时不自动打开浏览器 |
| `--scan-only` | 只输出文字版扫描清单，不启动服务（可配合 `--drive D`） |
| `--fullscan-only` | 只执行全盘扫描并输出占用树 |

## 清理范围

预定义目标覆盖常见位置，例如 Windows 临时目录、Windows 更新缓存、回收站、开发工具与浏览器缓存。提供五个一键预设：

| 预设 | 选中范围 |
|---|---|
| 安全清理 | 删除后会自动重建的缓存与临时文件 |
| 深度清理 | 在安全清理基础上加入显卡着色器缓存、缩略图、崩溃转储、系统日志 |
| 开发者清理 | uv 与 pip 包缓存 |
| 只清回收站 | 回收站内容，可展开挑选单个文件 |
| 全部取消 | 清空勾选 |

除预定义目标外，常规扫描还会在所选盘顶层查找被知识库判定为可清理的目录（界面中带「自动发现」标记）。判定规则与排除条件见 [docs/USAGE.md](docs/USAGE.md#自动发现数据盘能用的关键)。

## 空间占用查看

- **全盘扫描**：扫描所选盘的全部占用，默认按体积降序展示「全盘最大项目」，可切换「本层目录 / 只看文件 / 全部可清理」
- **逐层下钻**：从任意目录继续展开下一级，展开深度不受扫描树深度限制
- **细分查看**：将目录拆分为「可清理的子目录」与「不可清理的部分」，逐项给出体积与理由；会先判断目录的实际身份（浏览器资料目录 / 游戏安装目录 / 无法确定）再给出结论
- **Steam 游戏库**：读取 `libraryfolders.vdf` 与 `appmanifest_*.acf`，不扫描磁盘；游戏列表可按体积或最近游玩时间排序

## 删除目标的保护规则

以下目标不会被执行删除，即使 `config\targets.json` 被修改：

- 盘符根目录本身，以及在根目录直接删除文件
- `Windows`、`Program Files`、`ProgramData`、`Users`、`Recovery` 等系统目录
- 出现在任意层级的 `System32`、`SysWOW64`、`WinSxS`、`Installer`、`servicing`、`Boot`
- `$Recycle.Bin`、`System Volume Information` 目录本身（回收站使用专用清理接口）
- `%USERPROFILE%` 及 `%USERPROFILE%\AppData`

不列入清理建议的内容包括用户文档、图片、桌面文件、已安装程序本体、已下载的 AI 模型、`Windows\Installer` 与 `WinSxS` 组件存储。

删除为直接删除（不经过回收站）。执行前会弹出确认窗口，逐项列出路径、体积与文件数。被其它进程占用的文件会被跳过并在结果中说明；需要管理员权限的项目会被标注，同批次其余项目不受影响。

上述拦截规则配有对抗性用例，并已固化为 `tools/selftest.py` 中的回归断言，细节见 [SECURITY.md](SECURITY.md)。

## 换机使用

路径使用 `%LOCALAPPDATA%`、`%USERPROFILE%` 等环境变量，不含固定盘符与用户名。将整个目录复制到另一台 Windows 机器后可直接运行，盘符、用户名与可用空间会自动识别。要求目标机器已安装 Python 3。

## 常见问题

**网页无法打开**：查看命令行窗口输出的「界面地址」一行；端口被占用时会自动向后查找。

**提示需要管理员权限**：`Windows\Temp`、Windows 更新下载缓存等项目本身需要提权才能访问，以管理员身份重新运行即可；同批次其余项目不受影响。

**提示部分文件被占用**：浏览器、剪辑软件或 Python 进程正在使用这些文件，关闭相关程序后重试。

**清理后游戏首次启动变慢**：清理了显卡着色器缓存，游戏需要重新编译着色器，之后恢复正常，不影响游戏文件。

**修改配置后是否需要重启**：不需要，修改 `config\*.json` 后刷新网页即生效。

更多问题见 [docs/USAGE.md](docs/USAGE.md#十二常见问题)。

## 目录结构

```
O-Cleaner\
├─ start-ocleaner.bat      启动脚本
├─ app.py                  主程序（扫描 / 占用分析 / HTTP 接口 / 删除 / 换算 / 定时任务）
├─ README.md               本文件
├─ docs\USAGE.md           完整使用指南
├─ config\                 drives.json · targets.json · filetypes.json
│                          game-names.json · convert.json · custom-rules.json
├─ web\index.html          界面（HTML + CSS + JavaScript 均在单个文件内）
└─ tools\                  selftest.py · lint_rules.py · cdp_check.js
                           doctor.py · fix-encoding.ps1 · make-package.ps1
```

## 测试与贡献

仓库包含三道检查，均在 Windows 上运行，已接入 GitHub Actions：

```
python -X utf8 tools/lint_rules.py --strict     # 规则检查（6 项检查，警告计为失败）
python -X utf8 tools/selftest.py                # 功能自测（300+ 项断言，完整环境 314 项）
node tools/cdp_check.js http://127.0.0.1:8770   # 浏览器渲染与交互检查（80 项断言）
```

`config\filetypes.json` 是占用识别的知识库，决定哪些目录会被判定为可清理。修改该文件必须通过 `tools/lint_rules.py`（CI 强制）。linter 会做结构校验、语义矛盾检测、关键词跨规则冲突检测，并使用实际识别引擎验证每条关键词的归属规则。

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全模型与漏洞报告见 [SECURITY.md](SECURITY.md)。

## 许可

见 [LICENSE](LICENSE)。第三方组件 WizTree 的版权、许可与分发限制见 [docs/USAGE.md](docs/USAGE.md#明确标明第三方许可及声明)。
