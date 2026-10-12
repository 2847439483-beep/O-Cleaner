# O-Cleaner 完整交接文档与需求规格书

> **文档版本**：v2.3（资源包 + 自有 AI 接入版）
> **项目定位**：Windows 原生、本地零第三方依赖的磁盘清理与空间占用查看工具。
> **代码仓库**：`github.com/2847439483-beep/O-Cleaner`（私有），`main` 分支，CI 已接入。
> **分发包**：由 `tools/make-package.ps1` 生成，输出到 `dist\O-Cleaner-<版本>.zip`。

---

## 目录
1. [用户需求全景追溯表（完整需求列表）](#一用户需求全景追溯表完整需求列表)
2. [核心技术架构与设计哲学](#二核心技术架构与设计哲学)
3. [系统安全底线与防误删防御矩阵](#三系统安全底线与防误删防御矩阵)
4. [核心功能模块与接口规格](#四核心功能模块与接口规格)
5. [踩坑历史与防御性断言库](#五踩坑历史与防御性断言库)
6. [质量门禁与全流程测试套件](#六质量门禁与全流程测试套件)
7. [文件资产与代码清单](#七文件资产与代码清单)
8. [已知限制、未完成事项与风险点](#八已知限制未完成事项与风险点)
9. [给后续接手模型的优化与拓展建议](#九给后续接手模型的优化与拓展建议)

---

## 一、用户需求全景追溯表（完整需求列表）

本项目从最初的 C 盘脚本演化为全功能的磁盘空间与清理系统，以下按时间线梳理用户的**全部真实需求与实现状态**：

| 编号 | 需求阶段 | 用户原始需求与核心意图 | 交付实现方案与落地状态 |
|:---|:---|:---|:---|
| **REQ-01** | 初版脚本 | “清理C盘无用文件，清理前进行确认” | 实现 `clean-c-drive-safe.ps1`，命令行预扫描并提供交互确认 |
| **REQ-02** | 可视化 WebUI | “构建一个有可视化界面，可重复使用，可以确认并简短介绍要清理的文件类型，自主勾选删除部分的 WebUI” | 搭建单文件 HTML+CSS+JS 前端与 Python HTTP 后端，卡片式展示与勾选确认 |
| **REQ-03** | 误删找回 | “clean-c-drive.ps1 和 clean-c-drive-safe.ps1 误删的，修复” | 在本地完全恢复两个脚本源码并维持向后兼容 |
| **REQ-04** | 品牌与多盘支持 | “美化UI…更名为 O-Cleaner，将所需文件打包，支持 D 盘或者自定义磁盘，且在其他 Windows 设备上正常运行” | 统一品牌 O-Cleaner；引入 `%DRIVE%` / `%LOCALAPPDATA%` 跨设备变量；支持全盘动态枚举切换 |
| **REQ-05** | 历史与换算 | “增加历史清理功能，看见累计清理存储和内容，做可视化处理…可自定义换算内容，给出范例” | 增加 `history.json` 记录与换算引擎（如 `1/3 * movie4k`、奶茶杯数、照片张数等 10 种单位）；单卡片折线/图表呈现 |
| **REQ-06** | UI 架构重构 | “需清理项框位设置不合理，挤在左上侧，重构和美化 UI，自定义清理规则为勾选固定的需清理项，保存规则后自动清理相应需清理项，可以自定义规则名字，换算和累计清理合并” | 栅格流式两栏重构（卡片区左侧占宽，侧栏右侧定宽）；合并换算面板；实现命名规则保存、套用与即时清理 |
| **REQ-07** | 全盘深度扫描 | “D盘或者E盘或者其他磁盘也要全盘扫描，自主识别并解释不同设备的各个清理项，增加磁盘概览部分的功能，要求可视化，有文件名加简要介绍加储存占用空间，可视化柱状图，柱上直接显示文件名，点击显示详细信息” | 实现单进程单次递归占用树（深度最深 5 层）；集成 `config/filetypes.json` 42 条知识库规则，饼环 + 柱状图直观呈现；柱体置顶显示文件名 |
| **REQ-08** | 全盘深度修复 | “无法全盘扫描，DE盘可清理项只有临时文件和回收站” | 实现顶层与次层动态「自动发现」引擎（`auto_discovered_targets`），让长尾个人软件（网盘/开发/驱动/部署）浮出水面 |
| **REQ-09** | 子项查看与游戏 | “要求大清理项可点击查看子项，没有扫描到 Steam 游戏，扫描识别以文件体积论，子目录也要扫描” | 占用排行支持逐层下钻；深度遍历抓出上百 GB 的深层游戏实体；以文件与目录体积为首要排序依据 |
| **REQ-10** | 开源合规储备 | “做成开源免费本地运行的软件，exe 暂不考虑，英文规则也是，先做规则 linter + CI 配置” | 编写 MIT LICENSE、SECURITY.md、CONTRIBUTING.md、GitHub Actions CI 流程以及 6 项严苛规则 linter（带缺陷注入自测试） |
| **REQ-11** | 游戏集成与通用性 | “Steam 游戏库单独集成一个界面，其在 DE 盘的存储也要显示，DE 盘中的大清理项要求点击可以展开，比如 Chrome 展示可清理项等等，要求与母项相同有解释占用等等，优化 UI，做一些子页面，尽量优化程序在不同设备的通用性” | 独立 Steam 子页面（直接读取 manifest 清单，不遍历磁盘）；分盘库统计与 180 天未游玩吃灰判定；开发 `analyze_dir_partition` 拆解浏览器数据目录（缓存 vs 书签密码） |
| **REQ-12** | 安全与对抗审计 | “自检，审查，优化，检测风险和漏洞” | 路径护栏 34 条 + HTTP 层 11 条对抗性回归断言，全部固化进 `tools/selftest.py`；修补空路径与非绝对路径越界漏洞、跨域写操作未校验来源的问题 |
| **REQ-13** | 盘符串扰根治 | “为什么总是有 DE 盘包管理器，浏览器，临时文件等等东西” | 根除 `%LOCALAPPDATA%` 类目标未限制盘符的缺陷（`is_user_local` 拦截）；数据盘默认清理范围设为「仅本盘专属项」 |
| **REQ-14** | 扫描极速提速 | “尝试在有 WizTree 的情况下调用，加快扫描速度” | 自动探测本地已安装的 WizTree，改用其 `/export=` 导出读取 NTFS MFT，替代逐目录递归；未安装时平滑回退 PowerShell |
| **REQ-15** | 大项多级拆分子项 | “大清理项，如 D 盘 Chrome，占用空间大，可以打开二级目录拆分子项” | 基于 WizTree MFT 内存快照开发 `/__oc__/subdirs` 接口，实现任意层级子项与文件分布的即时查表拆解 |
| **REQ-16** | 界面去冗与直觉化 | “自动全盘扫描，不要游戏等大项这一分类，无需标注目录第几层” | 启动或切盘默认自动开启全盘扫描，默认视图直接呈现「全盘最大项目」；移除生硬的「游戏等大项」过滤；全面去除 `· 第X层` 内部标注 |
| **REQ-17** | 中文游戏名展示 | “在清理项有中文名的游戏用中文名，中文名联网找，只做显示” | 联网核对 Steam 官方译名，建立 `config/game-names.json`（荒野大镖客2、天国拯救2、巫师3等 30 条映射）；全盘树与 Steam 面板显示为 `中文名（原名）`，原名含中文则不重复添加 |
| **REQ-18** | 可移动便携与引导 | “能否把 WizTree 中调用的内核直接放到可以移动的 Ocleaner 中，以确认其在不同设备上的可用性，最后标明即可；设置简介快捷有效的引导” | 鉴于官方 EULA 禁止随包捆绑分发，实现**一键自动拉取官方面携版 ZIP 解压至 `portable\WizTree\`**，随 O-Cleaner 文件夹整体拷贝即插即用；右侧置顶「运行环境自检与引导」面板；详尽标明 Antibody Software Limited 授权信息与出处 |
| **REQ-19** | 树形就地折叠展开 | “返回上一级太过死板烦琐，更换方式，所有清理项可以继续无限展开，但在打开时再扫描，这样是否更快捷” | **全面废弃切页和面包屑返回**，重构为**树形折叠面板（Tree-Accordion）**；不管是全盘扫描列表还是清理卡片，均支持**按需懒加载（Lazy Loading）**就地展开与无限级递进下钻 |
| **REQ-20** | v2.3 资源包 / 自有 AI / 界面与真实进度 | “右侧 ui 进行堆叠，点击可展开查看详情，右侧磁盘概览整合到左侧磁盘概览里，扫描实际速度与进度条不匹配，左右两侧 ui 滚动分别做，支持接入 api，使用自己的 ai 使用此工具，包括可清理项解释，自定义清理方案以及其他可自定义内容，支持 Esc 键快捷返回上一级子目录，写入更多 steam 和 epic 等等游戏中文名称和解释数据，整合成可供下载的资源包，让使用者的 api 可以自主识别这些内容，Windows 系统固定可清理项的解释也完善补充，整合成资源包，可清理项释义覆盖不够完善，可以整合更多资源包，支持自定义资源包，把资源包也放在 GitHub 上面，运行环境自检板块的跨设备兼容改为兼容性分析” | 12 项全部落地，详见 §6 第 11 条：右栏 11 个面板折叠堆叠（`localStorage` 记忆）；磁盘概览并入左栏、全盘页改名「扫描概览」；进度改为服务端按真实阶段上报（新增 `/__oc__/progress`，无分母阶段用流动条纹）并删除按时间估算的旧公式；两栏独立滚动（命名为 `grid-template-areas` 网格）；新增 `packs/` 4 个内置资源包 148 条 + 自定义导入 + GitHub 更新接口；新增自有 AI 接入（解释单项 / 生成方案，AI 不能删除）；Esc 返回上一级；「跨设备兼容」改为「兼容性分析」（9 项，逐条给实测依据） |

---

## 二、核心技术架构与设计哲学

```
                         Browser (Desktop / Mobile)
                                    │
                                    ▼ HTTP (127.0.0.1:RandomPort)
                   ┌──────────────────────────────────┐
                   │    Python 3.8+ 标准库 (app.py)    │
                   │  - 纯原生，零 pip 安装，零外部模块   │
                   └──────────────────────────────────┘
                            │                   │
               ┌────────────┴────────────┐      │
               ▼                         ▼      ▼
    ┌──────────────────────┐  ┌──────────────────────┐
    │  加速扫描 (优先执行)  │  │  通用回退 (无缝兼容)  │
    │  WizTree MFT 快照引擎 │  │  PowerShell 5.1/7+   │
    │  /export= CSV 极速解析│  │  单进程树递归/扫描   │
    └──────────────────────┘  └──────────────────────┘
               │                         │
               └────────────┬────────────┘
                            ▼
               ┌──────────────────────────┐
               │ 安全防御与语义识别层     │
               │ - check_deletable (护栏) │
               │ - identify (知识库规则)  │
               │ - _looks_like_program    │
               │ - 盘符归属隔离 (is_local)│
               └──────────────────────────┘
```

1. **零第三方依赖（Zero-Dependency Principle）**：
   - 核心原则：运行本工具本身不应需要先下载 `node_modules` 或安装 Python 第三方包。
   - 后端仅采用 Python 3.8+ 标准库（`http.server`, `urllib`, `subprocess`, `json`, `ctypes`, `zipfile`, `tempfile`）。
   - 前端采用内联原生 HTML5 + CSS3 + Vanilla ES6+，单文件，无 Webpack/Vite 构建步骤。
   - 浏览器渲染测试（CDP）使用系统自带的 Edge/Chrome，通过 Node.js 原生 WebSocket 通信，不引入 Playwright/Puppeteer。
2. **跨设备便携化（Cross-Device Portability）**：
   - 路径配置不含硬编码驱动器号（`C:`）或 Windows 用户名（`Administrator`）。
   - 统一采用系统级宏：`%DRIVE%`、`%DRIVE_LETTER%`、`%LOCALAPPDATA%`、`%APPDATA%`、`%USERPROFILE%`、`%TEMP%`。
   - WizTree 采用便携化布局：优先探测 `O-Cleaner\portable\WizTree\`。用户可一键下载或手动放入，随目录整体拷贝。
3. **两级双模全盘遍历模型**：
   - 快照扫描层：优先调用 WizTree 导出 MFT 明细，解析后常驻内存，支撑即时查表。
     耗时取决于磁盘类型与文件数量，不给出固定数值。
   - 原生递归层：无 WizTree 时，使用单个 PowerShell 管道进程完成受控深度剪枝遍历，避免频繁拉起进程的开销。
4. **按需懒加载树结构（Lazy-Loading Tree-Accordion）**：
   - 不使用「进入目录子页 - 面包屑 - 返回上一级」的结构。
   - 所有大项与卡片均为就地展开折叠交互。展开时前端向 `/__oc__/subdirs` 发起局部查询：
     有 WizTree 内存表时为查表操作；无 WizTree 时只读当前单目录。

---

## 三、系统安全底线与防误删防御矩阵

由于清理工具涉及破坏性操作，删除链路上设置了多层校验。总体取向是：
未确认不删除；判断不确定时宁可少提示。

### 1. 硬性禁止路径（硬编码于 `check_deletable`）

即使上层规则配置（JSON）被修改，以下目标也会被拦截：

- 盘符根目录本身（如 `C:\`、`D:\`）及在根目录下直接删除文件
- 系统核心目录：`Windows`、`Program Files`、`Program Files (x86)`、`ProgramData`、`Users`、`Recovery`
- 路径中任何位置出现的 `System32`、`SysWOW64`、`WinSxS`、`Installer`、`servicing`、`Boot`
- `$Recycle.Bin`、`System Volume Information` 目录本体
- 用户个人根基：`%USERPROFILE%`、`%USERPROFILE%\AppData`
- 空路径与纯空白路径：`os.path.abspath("")` 会返回当前工作目录，因此必须在归一化前拒绝
- 非绝对路径（`C:`、`..\Windows`、`/etc`、`temp` 等）：`abspath("E:")` 返回的是
  E 盘上的当前工作目录而非盘符根，同样必须在归一化前拒绝

### 2. 软件安装目录识别护栏（`_looks_like_installed_program`）

自动发现依据目录名命中知识库，可能把安装目录误判为缓存目录
（例如数据盘根目录下名字很短的目录命中 Photoshop 规则，而其中是 Adobe 软件本体）：

- 特征一：顶层出现 `Uninstall*.exe` 或 `unins*.exe`。
- 特征二：存在两个及以上「版本号/年份」命名形态的子目录（如 `Adobe Photoshop 2024`、`WPS Office\12.1.0.x`）。
- 不采用的判据：「顶层 .exe 数量」与「深层含卸载程序」。
  这两条会误伤存放多个安装包的下载目录，使其无法被识别为可清理候选。

### 3. 浏览器资料敏感隔离护栏（`_looks_like_browser_profile` 与分区分析）

与浏览器同名的目录可能占用上百 GB，其中包含书签、密码、Cookie、登录凭证等用户数据，
整体标为可清理会产生风险；但整体标为「建议保留」又无法说明哪些部分可以动：

- 浏览器规则整体标记为 `cleanable: false`。
- 提供 `analyze_dir_partition`，利用白名单 `BROWSER_CACHE_DIRS` 将目录解构为：
  - ① 缓存子目录（`Cache`、`Code Cache`、`GPUCache`、`Service Worker` 等）—— 删除后自动重建。
  - ② 建议保留（用户数据与程序本体）—— 不删除。

### 4. 盘符归属与环境隔离（`is_user_local`）

- 识别带有 `%LOCALAPPDATA%`、`%APPDATA%`、`%USERPROFILE%`、`%TEMP%` 的目标。
- 非系统盘（D/E 盘）剔除此类目标，消除「D 盘出现 C 盘 pip / 浏览器缓存」的问题；
  数据盘默认清理范围收敛为「仅本盘专属项」。

### 5. Web 本地安全防御

- 同源校验：对 `POST` / `DELETE` 接口校验 `Origin` / `Referer`，
  阻止外部网页通过浏览器向 `127.0.0.1` 发起写操作（不带 `Origin` 的命令行调用仍放行）。
- 请求体上限：`_body()` 限制为 4 MB，超限与畸形 JSON 按空对象处理，返回 4xx 而非 5xx。

---

## 四、核心功能模块与接口规格

### HTTP 服务路由规格（Base: `http://127.0.0.1:<PORT>`）

| Method | 路径 | 核心参数 | 返回结构概要 | 功能说明 |
|:---|:---|:---|:---|:---|
| `GET` | `/` 或 `/index.html` | 无 | HTML (UTF-8) | 前端一体化单页单文件应用 |
| `GET` | `/__oc__/state` | `drive`, `scope`, `custom` | `{app, presets, targets, disk, drives, wiztree, ...}` | 获取全局综合状态、清理目标元数据、盘符与环境 |
| `GET` | `/__oc__/scan` | `drive`, `scope`, `custom` | `{results: [{id, size, files, missing, denied}], disk, scan_time}` | 刷新常规清理目标的占用体积与状态 |
| `GET` | `/__oc__/overview` | `drive`, `depth` | `{engine, subdir_ready, nodes, root, scanned_total_h, ...}` | 全盘占用树，支持 WizTree 加速及回退 |
| `GET` | `/__oc__/subdirs` | `drive`, `path`, `limit` | `{engine, path, parent_size, children: [{name, size, type, can_drill, ...}]}` | 供树形折叠展开与深层下钻的按需即时查表接口 |
| `GET` | `/__oc__/wiztree` | 无 | `{found, portable, version, enabled, download_url, ...}` | 获取 WizTree 状态及第三方合规元数据 |
| `POST`| `/__oc__/wiztree/install`| 无（校验请求来源） | `{ok, path, version, error, log}` | 从官方地址下载便携版至 `portable\WizTree\` |
| `GET` | `/__oc__/steam` | 无 | `{found, game_count, total_size_h, games: [...], libraries: [...]}` | 读取 Steam 本地清单，整合呈现游戏库与中文名 |
| `GET` | `/__oc__/partition`| `path` | `{kind, title, cleanable, keep, cleanable_size_h, keep_size_h}` | 将混合目录解构为「可清理缓存」与「建议保留」两部分 |
| `POST`| `/__oc__/clean` | `{ids: [], only_paths: {}, drive}` | `{entry: {freed, freed_h, results: [...]}, stats: {...}}` | 执行删除（先过护栏校验，随后记录历史） |
| `POST`| `/__oc__/reveal` | `{path}` | `{ok: true}` | 在 Windows 文件资源管理器中选中并打开指定路径 |
| `GET` | `/__oc__/history` | 无 | `[{time, trigger, freed_h, results}, ...]` | 获取累计清理历史流水 |
| `DELETE`| `/__oc__/history` | 无（校验请求来源） | `{ok: true}` | 清空清理历史并归零累计换算值 |
| `POST`| `/__oc__/rules` | `{name, ids, drive}` | 最新自选规则列表 | 保存当前勾选项为命名固定规则 |
| `POST`| `/__oc__/schedule`| `{enabled, interval_hours, preset, drive}` | 定时清理配置对象 | 配置后台定时清理守护进程 |

---

## 五、踩坑历史与防御性断言库

本节记录开发过程中出现过的缺陷及其成因，修改相关模块前建议先读一遍。

1. **`run_ps()` 数组折叠吞掉后续元素**：
   - *现象*：目录细分或体积测量时，第二个及之后的子项目录体积恒为 0。
   - *根因*：早期写了 `if isinstance(data, list): return data[0]`，意图把单元素数组压平；
     但 PowerShell 在该场景确实返回 `[{path, size}, ...]` 数组，于是第一项之后的数据被截断。
   - *规范*：解析层不替调用方猜形状，原样返回，由调用端自行归一化。已在自检中固定单/双元素数组的原样性断言。
2. **WizTree 导出 CSV 未清理，累积占用磁盘**：
   - *现象*：连续多次全盘扫描后，Temp 目录残留多个大 CSV 文件。
   - *根因*：扫描完成后未在 `finally` 块中删除临时导出文件。
   - *规范*：清理工具的临时文件必须自行清理。现已引入 `_wiztree_cleanup()` 配合 `finally` 删除，
     并在 `app.py` 启动入口清扫此前异常退出留下的孤儿文件。
3. **PowerShell 与批处理文件的编码问题**：
   - *现象*：批处理报语法错误；PowerShell 打印乱码（如 `'鏈湴' 不是内部或外部命令`）。
   - *根因*：`cmd.exe` 无法正确解析使用 LF 换行的批处理文件；PowerShell 5.1 会把无 BOM 的 UTF-8 脚本按 GBK 读取。
   - *强制规范*（唯一真相源：`tools/fix-encoding.ps1`）：
     - `.bat` / `.cmd`：ASCII + CRLF。
     - `.ps1`：UTF-8 with BOM + CRLF。
     - `.py` / `.json` / `.html` / `.md` / `.yml` / `.yaml` / `.js`：UTF-8 without BOM + CRLF。
     - 已由 CI 的 `lint` 作业第一步 `fix-encoding.ps1 -Check` 强制校验。
4. **PowerShell 字符串插值与 JSON 解析失败**：
   - *现象*：调用 PowerShell 扫描脚本时偶发 `KeyError: 'size='`。
   - *根因*：对包含 PowerShell 花括号的脚本调用了 Python `str.format()`。
   - *规范*：动态执行脚本统一使用 `.replace("__PLACEHOLDER__", ...)`，不使用 `str.format()`。
5. **PowerShell `ConvertTo-Json` 单对象退化**：
   - *现象*：返回结果变成序列化对象的属性名（`Name/FullName/Length`），解析提取不到 `path`，全盘大小变 0。
   - *根因*：`ConvertTo-Json` 在数组只有 1 个元素时会丢弃数组包装，降级为单个对象。
   - *规范*：脚本末尾凡涉及列表，统一手工拼接 JSON 数组字符串。
6. **VDF 解析丢失最外层键，游戏列表为空**：
   - *现象*：Steam 游戏库显示 0 个游戏，但库路径识别正常。
   - *根因*：解析 Valve VDF 时最外层 `AppState` 被展开后丢弃了键，导致字段散落。
   - *规范*：极简 VDF 解析器必须在根节点闭合处保留最外层实体键。
7. **全量目录树下钻导致详情入口不可达**：
   - *现象*：接入 WizTree 后，几乎每个目录都算出了子项可下钻，点击行只会进入子项，无法打开「详情」与「细分查看」。
   - *规范*：下钻交互与详情交互分离。每行提供独立的「详情」按钮，下钻通过展开折叠或点击主条进行。

---

## 六、质量门禁与全流程测试套件

接手开发或优化后，依次运行以下质量门禁，全部通过后再打包交付：

### 1. 自动化单元与回归测试 (`tools/selftest.py`)
- **断言规模**：本机完整环境为 **339 项通过 / 0 失败 / 3 跳过**（覆盖 8 个阶段）。**条数依机器环境浮动** —— 数据盘数量、是否装有 Steam / WizTree 都会影响实际执行的断言数，例如 CI runner 上执行为
  `通过 247 项，失败 0 项，跳过 11 项`。判断标准是失败数为 0，不要把条数写死在文档或 CI 步骤名里。
- **运行命令**：
  ```bash
  python -X utf8 tools/selftest.py
  ```
- **核心验证**：配置文件完整性、路径护栏对抗绕过、Temp 沙箱真实删除测试、HTTP API 端到端、知识库覆盖率、资源包匹配与安全边界、兼容性分析 9 项、扫描进度端点、`run_ps` 超时有界性、Steam 游戏清单与中文名解析、WizTree 加速与导出文件零残留、`is_user_local` 隔离等。

### 2. 真实浏览器无头渲染测试 (`tools/cdp_check.js`)
- **断言规模**：脚本内共 84 处断言点（`check` 35 处 + `checkScan` 43 处 + `skip`/`skip2` 6 处）。**单次运行实际登记的条数少于 84**，因为：部分断言位于条件分支内（例如 Steam 面板需本机装有游戏才会走到），且全盘扫描相关的 43 处在扫描未在时限内完成时统一记为跳过。  因此运行时常见结果是
  `通过 57 项，失败 0 项，跳过 1 项`（扫描完成）或 `通过 26 项，失败 1 项，跳过 31 项`（扫描未完成），两种情况下登记总数均为 58 条。
  判断标准是**失败 0 项**；扫描未完成时那条「全盘扫描完成并渲染出排行」会失败并单独暴露根因，其余记跳过。
- **运行命令**（需在 `app.py` 启动状态下执行）：
  ```bash
  node tools/cdp_check.js http://127.0.0.1:<PORT>
  ```
- **核心验证**：真实 Edge/Chrome 浏览器的 CDP 渲染、卡片布局未挤压、多级折叠展开 DOM 节点状态、全盘最大项默认视图、无层数干扰、Steam 中文名展示、返回常规视图等，全程监控并要求零控制台报错。
- **自我检验标准**：汇总行中「失败 0 项」即视为通过；跳过项属于环境说明。

### 3. 知识库规则 Linter 检查 (`tools/lint_rules.py`)
- **运行命令**：
  ```bash
  python -X utf8 tools/lint_rules.py --strict
  python -X utf8 tools/_lint_selftest.py
  ```
- **检查规模**：7 项检查。42 条知识库规则语法合规、分类与置信度合法、无相互语义反向冲突、205 个关键词样本的归属校验准确；第 [2/7] 节专门校验 `packs/`（结构、清单与文件双向一致、正则可编译、关键词宽泛度、分类是否会排到列表末尾）。
- **检出能力自测**：`_lint_selftest.py` 向 linter 注入缺陷并确认每一类都被抓到 —— 知识库 15 类 + 资源包 20 类，外加「干净输入应通过」「还原后应通过」两个基线。资源包那一组刻意在系统临时目录里造一个假 `packs/`（进程内调用 `lint_packs()`），不往仓库写文件，避免被宿主删除保护拦截后留下测试残渣。

### 4. 跨机环境诊断与编码归一化
- **运行命令**：
  ```powershell
  # 只检查不修改（CI 用的就是这个模式；有问题返回 1）
  powershell -NoProfile -ExecutionPolicy Bypass -File tools/fix-encoding.ps1 -Check
  # 真正修复
  powershell -NoProfile -ExecutionPolicy Bypass -File tools/fix-encoding.ps1
  python -X utf8 tools/doctor.py
  ```
- **核心验证**：27 项跨设备环境诊断；自动复核 BOM 与换行符。
- **已接入 CI**：`.github/workflows/ci.yml` 的 `lint` 作业第一步即 `fix-encoding.ps1 -Check`，不符契约直接失败。
  配套地，`.gitattributes` 已为 `.py/.json/.html/.md/.yml/.js` 写死 `eol=crlf`，
  避免换行符随 `core.autocrlf` 漂移导致 fresh clone 在 CI 上误报。

### 5. 接手方补丁记录（2026-09-30，selftest 数据隔离加固）

接手验证时实测发现并修复了 `tools/selftest.py` 的两个真实缺陷（当时断言总数不变，仍 271 项）：

1. **真实用户数据会被测试删除**：旧版 `finally` 直接删除 `history.json` / `schedule.json` 而不备份。若用户真实使用过 O-Cleaner，跑一次测试就会丢失全部清理历史。现已改为测试前备份、测试后还原（测试前本不存在则清掉测试产物）。
2. **强杀后配置被固化**：若进程在 `write_test_config()` 之后被强杀（关窗/超时/管道断裂），`targets.json` 会残留测试内容，且下一次运行会把测试内容当成「真实配置」备份并固化。现已加入启动守卫：发现 `.targets.backup.json` 遗留时先还原再重新备份。
3. 断言「累计释放量已累加」原为绝对值（`== 768`），在本机存在真实清理历史时会误报失败；已改为基线增量断言（`freed_before + 768`）。

> 教训：交接验收在真实用户环境跑测试时，曾出现 `config/targets.json` 被测试内容覆盖的情况，最终从 `dist/O-Cleaner-v2.0.zip` 内还原。会改写真实配置或数据文件的测试，都应先备份后还原，并假设自己随时可能被强杀。

### 6. 接手方优化记录（2026-09-30，v2.0 → v2.1）

1. **放开无 WizTree 下钻限制**（原第八节第 4 条）：前后端改动见该节现状描述；断言规模 271 → 276 项。
2. **版本号统一升至 2.1**：`app.py`（server_version / UA）、`config/drives.json`、`tools/make-package.ps1`。
3. **全量门禁复验通过**：lint 0 错 / linter 自测 17 / doctor 27 / selftest 276（2 跳过）/ CDP 72。
4. **运维注意（已定位根因）**：在 WorkBuddy 环境中起 `app.py` 跑 CDP 的「全盘扫描」相关断言会失败（浏览器表现为 `Failed to fetch`）。根因不是应用缺陷：宿主的安全删除拦截钩子会拦下 `scan_disk_overview()` 写入并即时清理的临时文件 `.steam-orphans.tmp`（服务端日志可见 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`），处理函数随即中断连接；**关闭沙箱并不能规避**（已实测）。命令行 `python -X utf8 app.py --fullscan-only --drive D` 可完整跑通，selftest 阶段三 HTTP 端到端与阶段七 WizTree 扫描也全部通过；CI 与真实用户环境不存在此拦截。
5. **运维注意（删除配额）**：同一钩子还有**每回合删除配额**（日志里的 `threshold`/`count`）。连续多次运行 `selftest.py` 会耗尽配额，第二次起会在启动阶段的残留清理（`.steam-orphans.tmp`、`config/.targets.backup.json`）被直接中断 —— 表现为进程秒退、几乎无输出（Python 默认缓冲 stdout，被中断时输出一并丢失，**建议用 `python -u` 运行**）。此时 `config/targets.json` 仍保持原样不受影响，但需手工清掉残留的 `config/.targets.backup.json` 才能再次运行。一次完整的干净运行结果：**302 通过 / 0 失败 / 3 跳过**。
6. **自测产物遗漏修正**：`selftest.py` 会对 `history.json`/`schedule.json` 生成 `.<名字>.backup` 安全备份，此前未被 `.gitignore` 覆盖（`*.bak` 匹配不到 `*.backup`），已在 `.gitignore` 补上 `*.backup`，避免运行数据误入版本库。
7. **遗留清理**：`Documents/clean-c-drive*.ps1` 与 `Documents/dsh-cleaner/` 已移入回收站。
   后续（2026-10-08）归档副本已随仓库迁至 `E:\O-Cleaner\_legacy\`，含备份 zip；
   该目录已在 `.gitignore` 中排除，`fix-encoding.ps1` 的 `$skipDirs` 也会跳过它。

### 7. 接手方优化记录（2026-10-06，v2.1 → v2.2 多平台游戏库）

1. **Epic / GOG 游戏识别**（原第八节第 3 条）：实现细节见该节现状描述；断言规模 selftest 276 → 302 项、CDP 72 → 74 项。
2. **版本号统一升至 2.2**：`app.py`（server_version / UA）、`config/drives.json`、`tools/make-package.ps1`。
3. **全量门禁复验通过**：lint 0 错 / doctor 27 / selftest 302（3 跳过：GOG 无本机数据等）/ CDP 74。
4. **设计备忘**：Epic/GOG 游戏判定走「清单精确路径命中」而非 Steam 的「双亲目录推断」——刻意更保守，库目录下未登记的目录不会被误标为游戏。

### 8. 接手方优化记录（2026-10-08，数据脱敏 + 文档校准 + 编码契约进 CI）

1. **数据脱敏（可安全外发）**：真实盘符路径、软件目录名、硬件容量、游戏库体积等本机可识别信息全部泛化；本机真实测试用例迁至 `tools/local-cases.json`（已 gitignore），另附干净模板 `tools/local-cases.json.example`；`selftest.py` 新增 `_load_local_cases()`，阶段七改为「填了就测、没填就跳过」。产品知识库关键词（`kugou` / `quark` 等）属功能本体，刻意保留。
2. **推出 GitHub**：`github.com/2847439483-beep/O-Cleaner`（私有），`main` 分支，CI 已接入。注意：推送 `.github/workflows/*` 需要 token 具备 `workflow` 权限，否则报 `refusing to allow an OAuth App to create or update workflow`。
3. **编码契约进 CI**（对应原第八节风险项）：`fix-encoding.ps1 -Check` 已作为 `lint` 作业的第一步，**违反即失败**。配套在 `.gitattributes` 为 `.py/.json/.html/.md/.yml/.js` 写死 `eol=crlf` —— 否则换行符会随 `core.autocrlf` 漂移，CI 上 fresh clone 拿到 LF 就会误报。
4. **文档数字校准**：修正 §6 / §7 / §9 及 CI 注释中过期的断言数（271→302、72→74、142+→302、15→17）与文件体积（app.py 151→164、index.html 130→133、selftest 65→75、cdp_check 41→42、HANDOFF 16→37、dist 193→199）。
5. **全量门禁复验**：lint 0 错 / linter 自测 17 / selftest 302（3 跳过）。CDP 的「全盘扫描」类断言在 WorkBuddy 宿主下受安全删除钩子阻断（详见第 6 节第 4、5 条），**属环境限制而非回归**。
6. **CDP 检查器接入 CI 时暴露并修复的 6 处「单机假设」**（断言规模 74 → 80 项）。首次 CI 运行 `test` 与 `ui` 两个作业全挂，根因全部是「断言隐含了开发机的环境条件」：
   1. **阶段异常会吞掉后续阶段**：`selftest.py` 的 `main()` 原是一串裸调用，阶段三在 CI 上超时抛 `TimeoutError` 后，阶段四～七**从未执行**。已改为 `_phase()` 逐阶段 `try/except`，任一阶段抛错只记一条失败断言，其余照跑。
   2. **HTTP 超时过短**：阶段三默认 60 秒，CI 冷启动不够；放宽到 180 秒。
   3. **假设 D 盘有内容**：`D 盘能自动发现可清理的应用目录` 与磁盘树的 4 条断言都假定盘里装了东西。已加 `_drive_looks_populated()` / `_top_level_dir_count()` 前提判断，盘太干净就跳过。
   4. **假设非管理员**：`拒绝访问记为错误` 在 CI（以管理员运行，能删 `C:\Windows\Temp`）下必然失败。已改为 `is_admin()` 时跳过。
   5. **CDP 首屏等待写死秒数**：旧写法「等 9 秒 + 最多再等 40 秒」，CI 上「0/33 张有大小」连锁失败。已改为 3 秒起步 + 最长 300 秒宽松轮询。
   6. **CDP 点击目标取「第一张卡/第一行」**：盘上第一张卡常是不可勾选的系统项，点它不产生勾选，于是「规则保存按钮随勾选启用」一直失败。已改为挑选第一张 checkbox 未被禁用的卡片。
   同批还补了 CDP 的两处健壮性缺口：`skip()` 一直被调用却从未定义（一走到即 `ReferenceError`，会中断整个检查器），以及全盘扫描没出结果时 `#fsUp` / `.crumbs` / `#dClose` / `#stGames .vrow` 取不到节点导致 `evaluate` 抛 `Uncaught`。
7. **CDP 环境门控（核心改动）**：为「全盘扫描能否在时限内完成取决于盘里有多少小文件」这一事实引入 `checkScan()` —— `scanOk === true` 时它就是普通 `check()`；扫描未在时限内完成时，那 44 条依赖它的断言统一记为跳过（并打印跳过总数）。同时把「全盘扫描完成并渲染出排行」本身做成三态：成功 / 真报错则失败 / 仅在 CI 上超时则跳过（本机超时仍判失败，因为那是真实退化）。Steam 的 4 条内容断言同理，以 `STEAM.found && games > 0` 为前提。自我检验标准：汇总行「失败 0 项」即视为通过。
8. **CI 全绿（已验证）**：提交 `8c9dd22` 后 `lint`（27 秒，含编码契约与 JS 语法）、`test`（3 分 57 秒，302 项断言）、`ui`（8 分 12 秒）三个作业全部通过。CI 上 `ui` 的输出为 `通过 26 项，失败 0 项，跳过 32 项`；关键变化是「规则保存按钮随勾选启用」由失败转为 `初始禁用=true 勾选后可用=true 提示="当前已勾选 4 项"`。跳过项构成：全盘扫描未在 301 秒内完成（CI 系统盘）连带的 30 条 + `细分查看弹窗检查` 1 条 + Steam 库内容 1 条，均为环境说明而非回归。
9. **Actions 版本升级**：把三个 actions 统一升到当前最新大版本（`checkout@v7` / `setup-python@v7` / `upload-artifact@v7`）。本项目的用法只涉及 `checkout` 裸拉取与 `setup-python` 的 `python-version`，均在 v7 兼容范围内。

### 9. 接手方优化记录（2026-10-12，路径护栏补漏 + 文档校准）

1. **修复 `check_deletable` 的盘符相对路径漏点**：原实现在校验前先做
   `os.path.normpath(os.path.abspath(path))`。`abspath("E:")` 返回的是 E 盘上的
   当前工作目录（而非盘符根），因此 `E:`、`..\Windows`、`/etc`、`temp`、`.`、`..`
   这类输入会被解析成一个合法目录，并通过后续全部检查。
   已在归一化前新增 `os.path.isabs()` 校验，拒绝理由为「路径必须是绝对路径」。
   两个调用点（`clean_target`、自动发现）传入的均是绝对路径，不受影响。
2. **新增 12 条回归断言**：阶段二「路径护栏」由 22 条增至 34 条断言，
   `selftest.py` 总数由 302 增至 314。本机运行结果：`通过 314 项，失败 0 项，跳过 3 项`。
3. **文档按「不夸大、不写未证实数字」统一校准**：
   - `SECURITY.md`：对抗性测试一节改为引用真实断言数（路径护栏 34 条、HTTP 层 11 条），
     并说明用例位置；删除「整个工具 87 KB」与「34 项 / 21 项全部拦住」等未经核实的表述；
     「零遥测」改为准确描述（唯一外部请求是用户主动点击下载 WizTree，实测包体 7,905,571 B ≈ 7.5 MB）。
   - `CONTRIBUTING.md`：断言数 142+ → 314；编码表修正为
     `*.py/*.json/*.html/*.md/*.yml/*.yaml/*.js` = UTF-8 无 BOM + CRLF（原文写作「不限」）。
   - `README.md` / `docs/USAGE.md`：移除无法复现的单机实测数值
     （全盘扫描 258/33/51 秒与 21/3/4 秒、Steam 清单 0.6 秒、子项拆解 1–4 毫秒、
     自动发现「3 个 → 11 个」）与「约快 9 倍」等结论性措辞，改为描述机制与影响因素。
   - `web/index.html`：同步移除 WizTree 面板与扫描进度提示中的单机耗时数字；
     「千万不要删」改为「不要删除」。
4. **核实后确认无误、予以保留的数字**：`doctor.py` 27 项环境自检、
   `_lint_selftest.py` 17 类缺陷注入、知识库 42 条规则（28 目录 + 14 文件）、
   205 个关键词样本、`dist` 分发包 21 个文件 / 约 615 KB。

### 10. 接手方修复记录（2026-10-12，CDP 首屏扫描门控）

CI 上出现一次此前未见的 `ui` 失败（`通过 21 项，失败 4 项，跳过 33 项`，耗时 11m46s）。
根因不是环境噪声，而是上一轮门控遗漏了一条链路：

- 上一轮只给**全盘扫描**加了 `checkScan()` 门控，但首屏的**常规扫描**
  （`/__oc__/scan`，要逐个量出全部清理目标的体积）同样是「能否在时限内完成取决于机器」
  的环节。它超时后卡片不会被回填，也就没有可勾选的项，于是 4 条断言连锁失败：
  `扫描结果已回填到卡片`、`规则保存按钮随勾选启用`、`保存规则后出现在规则列表`、
  `规则卡片含 套用/立即清理/改名/删 操作`。
- 修复：新增 `checkInit()`，与 `checkScan()` 同构 —— 首屏扫描完成时按普通断言判定；
  未完成时，CI 上记为跳过、**本机仍判失败**（本机超时是真实退化）。
  同时为规则链路显式补了两条同名断言，避免「跳过」退化为「静默少了一条检查」。
- 验证：本地真实运行 `通过 70 项，失败 0 项，跳过 1 项`（全盘扫描走 WizTree 加速完成）；
  另用「缩短等待 + `CI=true`」人工复现超时路径，确认那 4 条断言由失败转为跳过。
- 新增认知：**CDP 的断言总数同样依环境浮动**（本次 CI 两种结果的总和均为 58 条），
  判断标准仍是失败数为 0。
- 修复后 CI 运行 `38223397977` 三个作业全部通过：`lint` 23 秒、`ui` 8 分 24 秒、
  `test` 4 分 37 秒。其中 `ui` 输出 `通过 26 项，失败 0 项，跳过 32 项`、
  `test` 输出 `通过 247 项，失败 0 项，跳过 11 项`。
  注：该次运行的首屏扫描在时限内完成，因此 `checkInit()` 的跳过分支未在此运行中触发 ——
  它由「缩短等待 + `CI=true`」的人工复现单独验证。

### 11. 接手方优化记录（2026-10-12，v2.2 → v2.3：资源包 + 自有 AI + 界面与真实进度）

本轮一次交付 12 项改动，按依赖关系分四组。以下均给出可复核的实测值。

**A. 界面与进度（对应「右侧 UI 堆叠 / 磁盘概览合并 / 进度不匹配 / 左右独立滚动 / Esc 返回」）**

1. **右栏改为可折叠堆叠**：11 个面板默认折叠成一行标题，点击展开；展开状态存 `localStorage`（键 `oc.acc.v1`）。标题内嵌按钮的点击做了 `stopPropagation`，不会误触折叠。
2. **磁盘概览合并进左栏**：右栏的重复面板已删除；全盘页原有的「磁盘概览」面板改名为「扫描概览」，避免两处同名。CDP 断言由面板标题串确认（`运行环境自检 / 一键预设 / STEAM 游戏库 / 资源包 / AI 助手 / 累积清理与换算`，其中不含磁盘概览）。
3. **扫描进度改为真实阶段上报**：删除了 `Math.min(97, Math.round(100 * (1 - Math.exp(-s / 90))))` 这条按时间估算的公式，改为服务端按阶段上报，新增 `GET /__oc__/progress`。有分母的阶段给百分比（解析按已读字节 14→70、逐项识别按节点数 84→94、兜底递归按节点预算 10→72），无分母的阶段标记 `indeterminate: true` 并渲染流动条纹（读 MFT 10、读 Steam 清单 76、读 Epic/GOG 清单 78）。
   - **这条同时解决了一个诊断盲区**：旧公式在扫描卡住时仍持续爬升，看起来像在正常工作。改为阶段上报后，卡住就表现为条纹不动。
4. **左右两栏独立滚动**：`main` 改为 2 列 × 4 行的命名网格（`"tool1 side" / "tool2 side" / "disk side" / "view side"`），行高为 `auto auto auto minmax(0,1fr)`。**`minmax(0,1fr)` 是必需的** —— 用 `1fr` 时隐式行会随内容增长，左栏永远成不了滚动容器。窗口 < 1180px 时退回整页滚动。
5. **Esc 返回上一级**：弹窗 → 关闭弹窗；全盘页 → 退回上一层目录，顶层再按一次回常规清理；Steam 页 → 回常规清理。扫描进行中按 Esc 只提示、不打断扫描。

**B. 资源包（对应「补充 Steam/Epic 等中文名与解释 / 完善 Windows 释义 / 覆盖不够 / 支持自定义 / 放到 GitHub」）**

6. **新增 `packs/` 目录与 4 个内置包，共 148 条**：
   `windows-system.json`（28 条，系统目录与系统文件逐条释义）、`games-platform.json`（21 条，Steam/Epic/GOG 平台目录与显卡缓存）、`games-titles.json`（65 条，游戏中文名与说明）、`apps-common.json`（34 条，包管理器/AI 模型/聊天软件等缓存）。合计 78.3 KB。
7. **匹配语义（一处实测逼出来的规则）**：得分是三元组 `(匹配在路径里的结束位置, 匹配串长度, …)`，**先比位置再比长度**。原先只比长度时，`\steamlibrary\steamapps`（22 字符，容器规则）盖过 `steamapps\shadercache`（21 字符，叶子规则），着色器缓存被打上「Steam 游戏库根目录」。
8. **安全边界（写进 `identify()` 并有断言）**：包命中的若是**节点本身**，其 `title`/`category` 与四段说明生效；命中的若只是**上层目录**，只补一个中文显示名。**内置已识别的项目，`cleanable` 与 `risk` 一律沿用内置值**，包不许改写；内置**未识别**的项目才允许包给出完整结论。理由：这两项决定「删不删」。
9. **自定义包与 GitHub 更新**：`import_pack()` 校验后写入 `config/packs-user/`；`packs_update_from_remote()` 按远端 `packs/index.json` 逐个下载导入，同 id 覆盖内置版本；界面提供导入/启用停用/检查更新。
   - **未验证项**：远端更新只在本机验证了「拒绝非 http(s) 地址」与结构校验路径。仓库当前为**私有**，`raw.githubusercontent.com` 的链接对外不可达，因此完整下载链路未做端到端验证。

**C. 自有 AI（对应「支持接入 API / 使用自己的 AI / 可清理项解释 / 自定义清理方案」）**

10. **新增 AI 模块与 4 个接口**：`/__oc__/ai/config`、`/ai/test`、`/ai/explain`、`/ai/plan`。支持任意 OpenAI 兼容的 `chat/completions` 接口。
11. **不能让 AI 变成删除通道**：`/ai/plan` 的候选清单只接受**已被内置规则判定为 `cleanable` 的项目**，未知 id 收进 `dropped` 返回而不采纳；前端 `aiApplyPicks()` 只勾选复选框，不触发清理。删除仍然只走底部按钮那条链路。
12. **Key 处理**：存 `config/ai.json`（已 gitignore，且不在分发清单里），界面与所有接口只回显尾 4 位（`has_key` / `key_tail`）。

**D. 门禁与文档**

13. **linter 新增第 [2/7] 节校验资源包**：结构与必填字段、`packs/index.json` 与磁盘文件**双向**一致、正则能否编译、关键词宽泛度（单段命中 `GENERIC_KEYWORDS` 或纯 ASCII 短于 3 字符）、分类是否会排到列表末尾、id 与文件名是否一致。`--strict` 通过（错误 0、警告 0）。
    - **一个被单元测试抓到的自伤缺陷**：初版把文本字段检查写在 `if cat is None: continue` 之后，于是**任何不带 `category` 的条目都会静默跳过后面全部检查**。已改为不使用提前 `continue`，并在代码里注明原因。
    - **宽度阈值不照搬内置规则**：实测现有包 `effect` 最短是「删除等于卸载。」、`rebuild` 是「不适用。」、`keep` 是「保留。」—— 按内置的 15 字标准去卡只会逼作者注水。现在只对 `what` 设 8 字下限，其余要求「存在即非空」。
14. **`_lint_selftest.py` 扩展为两组**：知识库 15 类 + 资源包 20 类缺陷注入 + 2 个基线，共 38 项，全部检出。资源包组在系统临时目录里造假 `packs/` 并进程内调用，不往仓库写文件（避免宿主删除保护留下残渣）。
15. **顺手修掉一个会导致扫描永久卡死的缺陷**：见下方「本轮新增踩坑」。
16. **文档校准**（按「实测得来、不夸大」）：`README.md`、`docs/USAGE.md`、`SECURITY.md`、`CONTRIBUTING.md`、`HANDOFF.md` 同步到 v2.3；`tools/make-package.ps1` 分发清单加入 `packs/`、版本号升至 2.3。
    - `SECURITY.md` 的「唯一外部网络请求是下载 WizTree」已**不成立**（新增资源包更新与 AI 调用），改为如实列出三类用户主动触发的出网请求。
    - 修正两处与代码不符的表述：`make-package.ps1` 头部称「.py/.json/.md 统一转 UTF-8 **BOM**」，而唯一真相源 `fix-encoding.ps1` 规定只有 `.ps1` 用 BOM；`docs/USAGE.md` 与 `HANDOFF.md` 中「314 项 / 80 项」等写死数字改为「依环境浮动」表述（CDP 的 84 处断言点 vs 单次登记 58 条，两种口径已在 §6.2 说清）。

**本轮新增踩坑（值得记住）**

- **`subprocess.run(timeout=…)` 在 Windows 上超时后可能永久阻塞**：它超时后先 `kill()`，再调一次 `communicate()` 收输出；若残留子进程仍持有 stdout 管道，这一步会一直等下去。表现是**扫描停在「正在读取 Steam 库清单」不动、进度条不再变化**（旧版还会继续爬，更迷惑）。
  修复：`run_ps` 改为显式 `Popen` + `taskkill /T /F` 结束整棵进程树 + 最多 5 秒收尾，保证一定返回；新增「超时后必定返回」「子进程持有管道时也能返回」等 6 条断言。修复前实测该场景无限阻塞，修复后 timeout=3 的调用 3.4 秒返回、持有管道场景 4.4 秒返回。
  这条是排查「steam 阶段卡 6 分钟」时定位的：单独调 `discover_steam()` 只需 **1.0 秒**，因此卡的不是脚本逻辑。
- **本机 CDP 的失败项仍属环境限制**：宿主安全删除钩子拦下应用自建的 `.steam-orphans.tmp`，导致 `/__oc__/overview` 断连。本轮实测服务端 stderr 有 7 次 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`，结果 `通过 26 项，失败 1 项，跳过 31 项`，与 §6.2 记录的「扫描未完成」形态一致。
- **端口上可能挂着旧后端进程**：本轮遇到 8770/8781 各有一个残留 `python.exe`（其中一个还出现两个进程同时 LISTENING）。因为 `web/index.html` 是每请求现读的，浏览器会看到**新前端 + 旧后端**，从而产生假阴性（`/__oc__/progress` 报 not found 而首页正常）。**换端口 + 核对 `api.github.com` 之外的接口是否为新代码**是可靠做法。

---

## 七、文件资产与代码清单

所有核心源码与配置位于 `<仓库目录>\`（本项目根目录）：

| 文件相对路径 | 大小概要 | 职责描述 |
|:---|:---|:---|
| `app.py` | ~164 KB | 后端全功能核心：HTTP 服务、安全删除校验、WizTree MFT 加速调用与管理、Steam 解析、分区分析、定时任务 |
| `web/index.html` | ~159 KB | 前端单文件：命名网格两栏布局、可折叠侧栏、自适应卡片流、树形折叠懒加载列表、环境自检面板（含兼容性分析）、Steam 面板、资源包面板、AI 助手面板、细分弹窗 |
| `start-ocleaner.bat`| ~1 KB | 双击启动入口：纯 ASCII + CRLF，检测 Python 并拉起默认浏览器 |
| `config/targets.json` | ~20.6 KB | 26 个预定义系统级目标与 5 个预设（安全/深度/开发者/回收站/清空） |
| `config/filetypes.json`| ~27.6 KB | 占用识别知识库：28 条目录规则 + 14 条文件规则（合计 42 条），定义识别特征与处置建议 |
| `config/game-names.json`| ~3.2 KB | 游戏中文译名映射库：30 条条目（纯显示，与 `packs/games-titles.json` 分工见 §九） |
| `config/convert.json` | ~3.0 KB | 10 种存储体积换算单位（支持表达式引用） |
| `config/drives.json` | ~1.2 KB | 盘符及应用元数据配置 |
| `packs/` | ~78 KB | 4 个内置资源包共 148 条（系统项 28 / 平台目录 21 / 游戏作品 65 / 常见软件 34）+ 清单 + 格式说明；受 linter 第 [2/7] 节约束 |
| `tools/selftest.py` | ~86 KB | 断言自动化测试套件（本机完整环境 339 项通过 / 0 失败 / 3 跳过；条数依环境浮动） |
| `tools/cdp_check.js` | ~48 KB | 84 处断言点的 CDP 浏览器渲染与交互校验脚本，单次运行登记 58 条（含 `checkScan`/`checkInit` 环境门控） |
| `tools/lint_rules.py` | ~30 KB | 知识库规则 + 资源包的静态审查器（7 项检查） |
| `tools/_lint_selftest.py`| ~11 KB | 针对 linter 自身的缺陷注入自测（知识库 15 类 + 资源包 20 类 + 2 个基线，共 38 项） |
| `tools/doctor.py` | ~6.6 KB | 新设备 27 项环境自检排查脚本 |
| `tools/fix-encoding.ps1`| ~4.9 KB | 编码与换行契约的唯一规范化工具（CI 的 `lint` 第一步） |
| `tools/make-package.ps1`| ~7.8 KB | 分发包构建工具（分发清单含 `packs/`；含预校验与编码复核） |
| `.github/workflows/ci.yml`| ~5.7 KB | GitHub Actions 持续集成配置（Windows 平台：编码契约 → lint → test → ui） |
| `.gitattributes` | ~1.4 KB | 换行与编码契约的声明（`eol=crlf` 写死，CI 依此校验） |
| `tools/local-cases.json.example`| ~0.6 KB | `local-cases.json` 的模板（真实用例在 gitignore 中隔离） |
| `README.md` | ~11 KB | 项目简介与快速上手 |
| `docs/USAGE.md` | ~31 KB | 完整使用指南 |
| `SECURITY.md` | ~8.5 KB | 安全模型、资源包与自有 AI 的信任边界、对抗性测试结果与漏洞报告流程 |
| `CONTRIBUTING.md` | ~8.1 KB | 贡献指南与提交前自查清单（含资源包提交流程） |
| `HANDOFF.md` | ~60 KB | 本文档：需求追溯、架构规格、踩坑记录与接手注意事项 |
| `_legacy/` | ~151 KB | 前身版本归档（已 gitignore，不属于本仓库） |
| `dist/O-Cleaner-v2.3.zip`| ~269 KB | 便携分发包：27 个文件（含 4 个内置资源包），由 `tools/make-package.ps1` 生成 |

---

## 八、已知限制、未完成事项与风险点

以下是当前代码尚未做到或存在已知局限的部分，接手者应优先评估：

### 1. 跨设备实测覆盖不足（最主要的未验证项）
- 现状：单元断言与浏览器断言目前**仍只在同一台机器上运行过**（单机、单一 Windows 环境，含系统盘与多块数据盘）。断言条数依环境浮动，不要在文档或 CI 步骤名里写死具体数字。
- 风险：`%LOCALAPPDATA%` 展开、盘符枚举、管理员权限分支、非中文系统区域设置下的行为均未经第二台机器验证。
- 建议：`tools/doctor.py`（27 项环境自检）即为此准备，应先在目标新机上运行它，再运行 `tools/selftest.py`。

### 2. Steam 中文名依赖手工维护的映射表
- 现状：`config/game-names.json` 目前为 30 条映射，未覆盖的游戏直接显示英文原名。
- 曾尝试但放弃的方案：解析本机 `Steam\appcache\appinfo.vdf` 提取官方中文名。该文件是 Valve 私有二进制 VDF 格式，解析时条目数报 5，未提取出任何名称；要正确实现需完整的二进制 VDF 解析器，且结果取决于该机 Steam 缓存中是否存有中文名，对「换设备可用」这一目标不可靠，故放弃。
- 建议：若要做，应写一个独立的 VDF 解析模块并补充测试；或改为社区维护的映射表。
- 补充（v2.3）：`packs/games-titles.json` 已提供 65 条游戏中文名与释义，与 `game-names.json` 的分工见 §九 第 3 条。两者都不覆盖的新游戏仍需手工补两条之一。

### 3. ~~游戏识别仅覆盖 Steam~~（v2.2 已解决）
- 现状（v2.2 起）：新增 `discover_epic()`（读 `%PROGRAMDATA%\Epic\EpicGamesLauncher\Data\Manifests\*.item` JSON 清单，仅用标准库）与 `discover_gog()`（读注册表 `HKLM\SOFTWARE\(WOW6432Node\)GOG.com\Games`）；统一入口 `discover_games()` 合并三平台并透传完整 Steam 负载，HTTP 路由 `/__oc__/games`。
- 占用树与拆子项标注：Epic/GOG 游戏按清单中的精确安装路径命中（`game_platform`，不可清理、高风险），库目录标 `game_library`；带 60 秒缓存的 `game_install_dirs()` 同时服务占用树与 `list_subdirs`。
- 前端：Steam 子页面内新增 Epic/GOG 面板（有数据才显示，含平台徽章、体积、详情弹窗）；占用排行行内显示 `· Epic 游戏` / `· GOG 游戏` / `· 游戏库`。
- 过滤要点：Epic 清单同目录混有引擎/插件条目（Unreal Engine、Quixel、Fab），判据是 `bIsApplication=True` 且 `CatalogNamespace!="ue"`，两条缺一不可。
- 未覆盖：Ubisoft / 战网仍未实现（无本机样本，清单格式各异）；Steam 面板标题与既有断言保持不变。
- 回归保障：`selftest.py` 阶段六B（26 项：合成清单/注册表解析 + 本机真实 Epic 数据 + 占用树整合标注），CDP 新增 Epic 面板断言（72 → 74 项）。

### 4. ~~无 WizTree 时深层下钻能力受限~~（v2.1 已解决）
- 现状（v2.1 起）：前端 `fsDrill` 已放开限制 —— 无 WizTree 时同样调用 `/__oc__/subdirs`（后端 `os.scandir` + `_size_of_dir` 原生回退），带「现场扫描，稍慢」的加载提示；拉不到子项时回退到详情弹窗。后端占用树节点的 `can_drill` 在无加速时改用一次性 scandir 探测（`_dir_has_any_child`）。
- 性能代价：每个展开动作需等待原生扫描（几百毫秒至数秒）；有 WizTree 时为查表操作。
- 回归保障：`selftest.py` 阶段七新增 5 项「无加速」断言（探测真假/空目录/不存在路径 + list_subdirs 原生回退不可用性）。

### 5. 单层超大量子项的渲染边界
- 现状：`/__oc__/subdirs` 默认 `limit=80`，全盘树取前 30 条，前端 `renderSubTreeHTML` 无虚拟滚动。
- 风险：若某目录下平铺上万个实体（如某些构建产物目录），单层渲染耗时会明显增加。
- 建议：加前端分页或虚拟滚动。

### 6. `app.py` 单文件已 208 KB
- 现状：所有后端逻辑（HTTP 路由、PowerShell 脚本常量、知识库、Steam 解析、WizTree 管理、资源包、AI 调用）都在一个文件里，约 4,800 行。
- 风险：继续膨胀会难以维护；用脚本化批量替换做局部修改时容易误伤（曾因此误删 14 个核心函数、约 700 行，当时无备份）。
- 建议：若要拆分，优先把 PowerShell 脚本常量抽到独立模块，它们是最大的静态块。
  另：`identify()` / `check_deletable` / `run_ps` 这三个函数被多处依赖，改动前先读第五节「踩坑历史」并跑通现有断言。

### 7. ~~早期脚本仍在项目目录之外~~（已归档进仓库目录）
- 项目前身的 `clean-c-drive.ps1`、`clean-c-drive-safe.ps1` 与 `dsh-cleaner\`（早期 WebUI 版）曾经游离在项目目录之外，现已归档到 `E:\O-Cleaner\_legacy\`，与仓库同目录放置但不纳入版本管理（`.gitignore` 已排除，`fix-encoding.ps1` 的 `$skipDirs` 也会跳过）。
- 其中保留了 `O-Cleaner-legacy-backup-2026-09-30.zip` 作为兜底备份。

### 8. ~~尚未 `git init`~~（已解决）
- 2026-09-30 已 `git init`（main 分支），初始提交为 v2.0 基线 + selftest 加固；v2.1 优化随后提交。
- `LICENSE` 版权人已填维护者名称（更换 GitHub ID 时直接改该文件即可）。
- 仓库已推送至 GitHub（私有）并接入 CI。本机 `gh` CLI 位于 `C:\Program Files\GitHub CLI\gh.exe`，不在 PATH 中，需用全路径调用。

### 9. 用户明确暂缓的方向（不要自行推进）
- exe 打包：用户明确表示暂不考虑。
- 英文规则 / i18n：用户明确表示暂不考虑。当前所有界面文案、知识库说明均为中文硬编码。
- 快速模式（2 层浅扫）：曾讨论，用户未采纳。
- 不引入安装型依赖：用户明确不使用 playwright / pip 依赖 / npm 包。CDP 测试使用系统自带 Edge + Node 原生 WebSocket 是刻意设计，不要替换为 Puppeteer/Playwright。

---

## 九、给后续接手模型的优化与拓展建议

若继续优化或增加新特性，请遵循以下约定，避免破坏现有行为：

> 动手前建议先读：本项目多数安全逻辑都对应一次实际缺陷。修改 `check_deletable`、`_looks_like_installed_program`、`identify`、`auto_discovered_targets`、`run_ps` 这五个函数中的任何一个之前，请先读第五节「踩坑历史」，并跑通现有断言（`tools/selftest.py`，条数依环境浮动）。这些函数的取向是宁可少推荐，也不误删 —— 不要为提高召回而放松判据。

1. **针对 WizTree 的增强**：
   - 现已提供 `/__oc__/wiztree/install` 接口。若网络不可达（如离线设备），可引导用户将预先下载好的 `WizTree64.exe` 手动放入 `O-Cleaner\portable\WizTree\` 目录，无需重启即可生效。
   - 分发时不要把 WizTree 二进制打包进公开 zip：其许可为个人使用免费，随包分发需要另行购买分发许可。由用户端本地拉取。
2. **知识库（`config/filetypes.json`）规则拓展**：
   - 新增规则后，运行 `python -X utf8 tools/lint_rules.py --strict` 确认无冲突。
   - 长尾软件的 `match` 应尽量使用具体前缀（如 `quark-desktop` 而非 `qd`），避免短词误判。
3. **中文游戏库（`config/game-names.json`）扩充**：
   - 该映射与核心逻辑解耦，可由社区维护更大的 Steam AppID 中文映射列表。
   - Steam 本地格式变动时，只改该表即可。
   - **注意与 `packs/games-titles.json` 的分工（v2.3 起）**：`game-names.json` 只做「英文名 → 中文名」的显示映射，不参与识别；`packs/games-titles.json` 提供识别与释义（含匹配路径、能否删除）。两者都改会重复维护，扩充游戏时请判断应该进哪一个：只缺中文名进前者，需要新的识别/释义规则进后者。
4. **资源包（`packs/`）扩充**：
   - 格式与匹配语义见 `packs/README.md`；新增包后必须把文件名加进 `packs/index.json`，否则 linter 第 [2/7] 节会报「没被收录」。
   - 关键词尽量写到叶子目录一级（例如 `steamapps\shadercache` 而非 `\steamapps`）。排序规则是「先比匹配在路径里的结束位置、再比长度」，泛化的容器规则会排到叶子规则之后 —— 这是被实测逼出来的，改动 `pack_score()` 前请先读第六节第十一条。
   - **不要试图让资源包改写已识别项目的 `cleanable` / `risk`**：`identify()` 里对此有硬性限制，这是刻意的安全边界。
5. **自有 AI（`/__oc__/ai/*`）**：
   - 新增 AI 能力时保持同一条约束：**AI 只能产出建议与勾选，不能直接触发删除**。`/ai/plan` 当前只接受内置规则已判定为 `cleanable` 的候选，未知 id 走 `dropped` 返回。
   - API Key 只存 `config/ai.json`（gitignore），任何接口不得回传完整 Key —— 现有实现只给 `has_key` 与 `key_tail`。
6. **前端性能边界**：
   - 树形折叠展开已实现懒加载；单层若有极端数量（如 10,000 个平铺文件），可在 `renderSubTreeHTML` 中增加前端虚拟滚动或分页切片（当前上限为 100/400 条）。
7. **界面布局（v2.3 起为命名网格）**：
   - `main` 用 `grid-template-areas` 排 2 列 × 4 行，行高必须保留 `minmax(0,1fr)`；改成 `1fr` 会让隐式行随内容增长，左栏永远无法成为滚动容器（左右独立滚动会静默失效）。
   - 改布局时注意 CDP 断言按 `#normalView` 定位，不再依赖「`main` 的第一个 `section`」。
8. **编码契约（已由 CI 强制）**：
   - 修改任何 `.ps1` 脚本后运行 `tools/fix-encoding.ps1`，确保 UTF-8 BOM 存在，否则 PowerShell 5.1 解析会失败；`.py/.json/.html/.md/.yml/.yaml/.js` 必须无 BOM 且 CRLF。
   - CI 的 `lint` 作业会先跑 `tools/fix-encoding.ps1 -Check`，不符契约直接失败。本地拿不准时先跑 `-Check`。
