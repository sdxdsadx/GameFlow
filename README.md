# GameFlow

> 一个本机、离线控制面的**游戏每日任务自动化编排器**。它以统一的 Web 控制面板驱动多个第三方游戏自动化运行时（MAA、BAAS、Alas、MaaAutoNaruto、MaaGumballs、MaaEnd、OneDragon、March7th 等），按每日计划自动启动模拟器/游戏、执行对应脚本、采集日志与截图证据，并汇总当日完成状态。

---

## 简介

GameFlow 不是一个游戏脚本本身，而是一个**编排层**：

- 把 9 个游戏的每日流程统一到一个「作战面板」里管理；
- 每个流程本质上是一系列 **step（步骤）**，每个 step 指定 **runner（执行器）+ 参数**；
- runner 由 `gameflow/runners.py` 提供（`adb`、`maa_gui`、`baas_gui`、`alas_gui`、`log_gui_daily`、`mumu_wait` 等）；
- 第三方自动化运行时（如 `MAA.exe`）是**外部程序**，GameFlow 负责按编组启动/监控/关闭它们。

正式产物把第三方运行时放在 `resources/tools`，代码和配置只使用相对项目根路径，**不会在运行时下载脚本或检查版本**（保持离线、可控、可复现）。

---

## 功能特性

- **统一面板**：任务开关（SKIPPED 标签）、拖拽与上下排序、并行数、单任务运行、强制重跑、逐任务取消、全部停止、实时日志、运行历史、今日完成状态。
- **多模拟器**：按工作流（`exclusive_group`）区分雷电/MuMu 实例，端点只传给所属工作流和对应内置工具，不互相覆盖。
- **健壮性**：黑屏看门狗、日志停滞检测、登录恢复、维护标记、卡死重启、失败重试、每日重试。
- **离线安全**：版本检查、自动更新、公告/推广、邮件发送、远程图片、定时后台任务均已移除；第三方运行时活动配置也已关闭更新检查。
- **证据留存**：每个流程结束自动截图到 `logs/`，便于排查。

---

## 支持的每日流程

| 工作流 id | 显示名 | 用到的 runner | 第三方运行时（放入 `resources/tools/<目录>`） | 依赖的宿主机 |
|---|---|---|---|---|
| `daily_game` | 明日方舟每日流程 | `maa_gui` | `maa/MAA.exe` | 雷电模拟器（`ldconsole`） |
| `blue_archive_daily` | 碧蓝档案每日流程 | `baas_gui` | `baas/baas.exe` | 雷电模拟器（`ldconsole`） |
| `azur_lane_daily` | 碧蓝航线每日流程 | `alas_gui` | `alas/Alas.exe` | MuMu 12（`mumu_manager`+`mumu_adb`） |
| `naruto_daily` | 火影忍者每日流程 | `log_gui_daily` | `naruto/MFAAvalonia.exe` | 雷电模拟器（`ldconsole`） |
| `gumballs_daily` | 不思议迷宫每日流程 | `gumballs_gui` | `gumballs/MFAAvalonia.exe` | 雷电模拟器（`ldconsole`） |
| `endfield_daily` | 终末地每日流程 | `maaend_gui` | `endfield/MaaEnd.exe` | 终末地游戏本体（`endfield_game`） |
| `zenless_daily` | 绝区零每日流程 | `log_gui_daily` | `zenless/.venv/Scripts/pythonw.exe`（OneDragon 自带 venv） | 绝区零游戏本体 |
| `star_rail_daily` | 星穹铁道每日流程 | `log_gui_daily` | `star_rail/March7th Launcher.exe` | 星穹铁道游戏本体 |
| `qq_reader_trial` | QQ 阅读每日任务 | `qq_reader_trial` | `qq_reader/MaaQQReaderGUI.exe` | MuMu 12（`mumu_manager`+`mumu_adb`） |

> **说明**：`resources/tools/*` 下的第三方运行时**不随本仓库分发**（体积过大、且各有独立上游）。请按 `docs/tools_upstream_index.md` 中的上游仓库索引逐个下载，放入对应目录。
> ADB 通用运行时位于 `resources/runtime/adb.exe`（随源码自带）。

---

## 运行

1. 先启动目标模拟器，并在模拟器设置中开启 ADB。
2. 双击 `start.bat`（或以管理员运行 `launch_admin.ps1 web`）。
3. 页面打开后选择任务、调整顺序与并行数，点击「✦ 开始每日作战」。

**页面仅监听 `127.0.0.1`**（本地回环，不对外暴露）。最后一次有效模拟器端口保存在 `data/runtime_settings.json`，点击运行时该端口会覆盖所有工作流的相关 ADB 字段，并同步写入每个内置工具的活动设备配置。QQ 阅读始终使用产物内的 `resources/runtime/adb.exe`。

### 命令行用法

```powershell
# 启动 Web 控制面板（默认 127.0.0.1:8765，不自动开浏览器）
python .\main.py web --no-browser

# 运行单个工作流
python .\main.py run daily_game

# 强制重跑某个工作流
python .\main.py run daily_game --force

# 查看最近运行记录
python .\main.py status

# 配置本机模拟器/游戏路径（见「本机路径配置」）
python .\main.py configure-host --ldplayer "<LDPlayer9目录>" --mumu "<MuMu shell目录>" --endfield "<Endfield.exe或所在目录>"
```

---

## 本机路径配置

模拟器（雷电/MuMu）、终末地游戏本体属于宿主机安装，**不写入**便携的 `config/workflow.json`。首次启动会从环境变量、Program Files 和各本地盘的常见安装目录自动发现，并保存到 `data/host_settings.json`。

自动发现失败或迁移到新电脑后，可手动配置：

```powershell
python .\main.py configure-host --ldplayer "<LDPlayer9目录>" --mumu "<MuMu shell目录>" --endfield "<Endfield.exe或所在目录>"
```

**也可通过 Web 面板的「主机配置」标签页配置**（推荐）：打开 `http://127.0.0.1:8765/#hostConfig`，可视化填写各工具入口路径、模拟器启动程序、游戏路径与模拟器端口，点「保存」。面板会校验路径有效性，无效会直接报错并提示字段。

---

## 构建（从源码打包）

项目不自动安装依赖。构建机需预先安装 `requirements.txt` 中的包，然后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

构建脚本会依次：
1. 运行测试（`tests/`，`-SkipTests` 可跳过）；
2. 便携审计（`tools/portable_audit.py`）；
3. PyInstaller 正式构建（`gameflow/` + `main.py` → `dist/GameFlow`）；
4. 按 `config/resource_manifest.json` 将 `config` 与 `resources/runtime` 复制到产物；
5. 版本审计 + 自检，成功后激活发布。

产物可整体复制到其他 Windows 主机。`resources/tools/*` 的第三方运行时**不进构建产物**，需运行时在目标机手动放置。

```text
resources/runtime/adb.exe     # 随产物打包
resources/tools/<name>/       # 需手动放置（来自各自上游）
```

---

## 目录结构

```text
GameFlow/
├── main.py                   # 入口（web / run / status / configure-host）
├── build.ps1                 # 构建脚本
├── requirements.txt          # 依赖：numpy/opencv/Pillow/psutil/pyautogui/pywin32/PyInstaller
├── config/
│   ├── workflow.json         # 9 个每日流程的编排配置（steps/runners/参数）
│   └── resource_manifest.json# 构建时资源清单
├── gameflow/                 # 核心源码
│   ├── config.py             # 加载/校验 workflow.json
│   ├── engine.py             # WorkflowManager：调度/启动/监控
│   ├── runners.py            # 各 runner 执行器（25 个）
│   ├── settings.py           # HostSettings/RuntimeSettings（本机路径+端口）
│   ├── store.py              # SQLite 运行历史
│   ├── web.py                # Web 控制面板
│   └── daily.py  diagnostics.py
├── resources/
│   ├── runtime/adb.exe       # 通用 ADB（随源码）
│   └── tools/<name>/         # 第三方运行时（手动放置）
├── docs/
│   └── tools_upstream_index.md  # 9 个运行时上游仓库索引
├── tests/                    # 单元测试
├── maa_naruto/               # 火影忍者 runner 依赖资源
└── start.bat  launch_admin.ps1  start_hidden.vbs
```

---

## 免责声明

> ⚠️ **使用本项目即表示您已阅读、理解并同意以下全部条款。**

1. **用途限制**：本项目仅用于**个人学习、研究与技术交流**，不应用于任何违反法律法规、游戏服务协议的行为。使用本项目产生的任何账号处罚、封禁、数据丢失或其他后果，由使用者自行承担。

2. **非官方产品**：本项目与游戏开发商、运营商、模拟器厂商、脚本原作者**无任何隶属、许可或合作关系**。所有涉及的第三方自动化运行时（MAA/BAAS/Alas/MaaAutoNaruto/MaaGumballs/MaaEnd/OneDragon/March7th/MaaQQReader）均归其各自作者所有，其分发与使用遵循各自的上游开源协议。

3. **风险自负**：自动化操作可能触发游戏反作弊检测、导致账号异常、影响游戏公平性。使用者应自行评估并承担全部风险。作者**不保证**本工具在任何环境下的稳定性、正确性、可用性，亦**不对**因使用本工具造成的直接或间接损失（包括但不限于账号损失、数据丢失、硬件损坏、时间浪费）承担责任。

4. **无担保**：本项目按“**现状**”（AS IS）提供，**不提供任何明示或默示的担保**，包括但不限于适销性、特定用途适用性、非侵权性。作者**不承诺**持续更新、修复缺陷或提供技术支持。

5. **使用责任**：使用者应遵守所在国家/地区的法律法规，并遵守所用游戏、模拟器、工具的**用户协议**与**服务条款**。若项目说明与任何强制性规则冲突，以规则为准。

6. **第三方内容**：本仓库内的 `resources/tools`、`maa_naruto` 等可能包含第三方资源或引用，其版权归各自作者；本仓库不转载、不分发受版权保护的资源本体，仅提供目录约定。

7. **禁止用途**：严禁将本项目用于任何营利性运营、代练、商业授权、或破坏游戏公平性的场景。

**使用本项目即表示您已接受上述免责声明；若不同意，请立即停止使用并删除全部相关文件。**

---

## 许可证

本项目源码部分的授权与使用，请以仓库中实际声明的许可证为准。第三方运行时（`resources/tools/*`）遵循各自上游项目的协议。
