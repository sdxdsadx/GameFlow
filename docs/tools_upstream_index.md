# GameFlow `resources/tools` 第三方运行时 — 上游 GitHub 仓库索引

> 用途：GameFlow 重构后将 `resources/tools`（9.95GB）剥离出仓库，本表供你在台式机/笔记本**逐个下载测试**这些第三方运行时。
> 每个工具对应一个游戏，放入 `resources/tools/<表内目录名>` 即可被 GameFlow 识别。

| # | GameFlow 内置目录 | 游戏 | 工具名 | 上游 GitHub 仓库 | Release 下载页 | 备注 |
|---|---|---|---|---|---|---|
| 1 | `resources/tools/maa` | 明日方舟 | MAA | [`MaaAssistantArknights/MaaAssistantArknights`](https://github.com/MaaAssistantArknights/MaaAssistantArknights) ★23072 | [Releases](https://github.com/MaaAssistantArknights/MaaAssistantArknights/releases) | 官方主程序，入口 `MAA.exe` |
| 2 | `resources/tools/baas` | 碧蓝档案 | BAAS | [`0x504B0304/baas`](https://github.com/0x504B0304/baas) | [Releases](https://github.com/0x504B0304/baas/releases) | Blue Archive Auto Script（v5.2.0），入口 `baas.exe` |
| 3 | `resources/tools/alas` | 碧蓝航线 | Alas | [`LmeSzinc/AzurLaneAutoScript`](https://github.com/LmeSzinc/AzurLaneAutoScript) | [Releases](https://github.com/LmeSzinc/AzurLaneAutoScript/releases) | 官方 README 明确标注，入口 `Alas.exe` |
| 4 | `resources/tools/naruto` | 火影忍者 | MaaAutoNaruto | [`duorua/narutomobile`](https://github.com/duorua/narutomobile) | [Releases](https://github.com/duorua/narutomobile/releases/latest) | README 标注，入口 `MFAAvalonia.exe`；也兼容 [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights) 生态 |
| 5 | `resources/tools/gumballs` | 不思议迷宫 | MaaGumballs | [`KhazixW2/MaaGumballs`](https://github.com/KhazixW2/MaaGumballs) | [Releases](https://github.com/KhazixW2/MaaGumballs/releases) | README 明确标注，入口 `MFAAvalonia.exe`，官网 gamer [maagb.xyz](https://maagb.xyz/) |
| 6 | `resources/tools/endfield` | 终末地 | MaaEnd | [`MaaEnd/MaaEnd`](https://github.com/MaaEnd/MaaEnd) | [Releases](https://github.com/MaaEnd/MaaEnd/releases) | README 明确标注（Powered by MaaFramework/MXU），入口 `MaaEnd.exe` |
| 7 | `resources/tools/zenless` | 绝区零 | OneDragon | [`OneDragon-Anything/ZenlessZoneZero-OneDragon`](https://github.com/OneDragon-Anything/ZenlessZoneZero-OneDragon) ★7096 | [Releases](https://github.com/OneDragon-Anything/ZenlessZoneZero-OneDragon/releases) | 绝区零一条龙，入口 `OneDragon-Launcher.exe`；CNB 镜像 [cnb.cool](https://cnb.cool/OneDragon-Anything/ZenlessZoneZero-OneDragon) |
| 8 | `resources/tools/star_rail` | 星穹铁道 | March7th | [`moesnow/March7thAssistant`](https://github.com/moesnow/March7thAssistant) | [Releases](https://github.com/moesnow/March7thAssistant/releases/latest) | 三月七助手，入口 `March7th Launcher.exe`；依赖 [Auto_Simulated_Universe](https://github.com/CHNZYX/Auto_Simulated_Universe) |
| 9 | `resources/tools/qq_reader` | QQ 阅读 | MaaQQReader | ⚠️ 未发现公开 GitHub 上游 | — | 基于 MaaFramework 的**自研/私有**项目（`MaaPracticeBoilerplate` 模板 + `@nekosu/maa-tools`），入口 `MaaQQReaderGUI.exe`。若需重新获取，请从原构建机打包，或在 `resources/tools/qq_reader` 保留现有副本 |

---

## 表外说明

- **`resources/runtime/adb.exe`**（6MB）：通用 ADB 运行时，不属于上面任何工具。**保留进仓库**（小体积），台式机无需另装 adb。如需升级，来自 [Android platform-tools](https://dl.google.com/android/repository/platform-tools-latest-windows.zip)。
- **`maa_naruto/`**（11.9MB，项目根）：**不是第三方副本**——它是 GameFlow 火影忍者 runner 的依赖资源（`runners.py` 多处调用 `ctx.root / "maa_naruto"`），**必须保留**。
- **`gameflow/*.py`**：GameFlow 自身源码，保留。

## 剥离后目录约定

重构后仓库**不含** `resources/tools/*`。运行时需你手动把以上工具放置到对应目录。GameFlow 的 `ctx.tool("maa_gui")` 等会从 `config/workflow.json` 的 `tools.<key>` 路径或 `tools/discovery` 模式自动发现；找不到时返回原名字（优雅降级），配合新增的「主机配置 GUI」可手动指定路径。

## 每个工具的游戏 / 模拟器 / 端口配置（GUI 里要暴露的字段）

GameFlow 需要为每个内置工具记住：
- **工具入口路径**（`executable`，如 `MAA.exe`）
- **模拟器启动程序**（`ldconsole.exe` / `MuMuManager.exe`，整机级）
- **游戏路径**（如 `Endfield.exe`，整机级）
- **模拟器 ADB 端口号**（1–65535，按工作流保存到 `data/runtime_settings.json`）

---

## 各每日流程实际依赖的脚本运行时（从 `config/workflow.json` 提取）

> 下表最有用——**你逐个下载时照这个对照**：每个每日流程需要哪个工具、放在哪个目录、依赖哪个模拟器/游戏。
> `→` 表示该工具是**第三方运行时**（从表一上游下载，放入 `resources/tools/<目录>`）；`⚙` 表示**宿主机安装**（模拟器/游戏本体，需另装）。

| 每日流程 id | display_name | 用到的 GameFlow runner | 依赖的**第三方运行时**→ 放入目录 | 依赖的**宿主机**⚙ |
|---|---|---|---|---|
| `daily_game` | 明日方舟 | `maa_gui` | `resources/tools/maa/MAA.exe` | 雷电模拟器 (`ldconsole`) |
| `blue_archive_daily` | 碧蓝档案 | `baas_gui` | `resources/tools/baas/baas.exe` | 雷电模拟器 (`ldconsole`) |
| `azur_lane_daily` | 碧蓝航线 | `alas_gui` | `resources/tools/alas/Alas.exe` | MuMu 12 (`mumu_manager`+`mumu_adb`) |
| `naruto_daily` | 火影忍者 | `log_gui_daily` | `resources/tools/naruto/MFAAvalonia.exe` | 雷电模拟器 (`ldconsole`) |
| `gumballs_daily` | 不思议迷宫 | `gumballs_gui` | `resources/tools/gumballs/MFAAvalonia.exe` | 雷电模拟器 (`ldconsole`) |
| `endfield_daily` | 终末地 | `maaend_gui` | `resources/tools/endfield/MaaEnd.exe` | 终末地游戏本体 (`endfield_game`) |
| `zenless_daily` | 绝区零 | `log_gui_daily` | `resources/tools/zenless/.venv/Scripts/pythonw.exe`（OneDragon 自带 Python 3.11 venv） | 绝区零游戏本体 |
| `star_rail_daily` | 星穹铁道 | `log_gui_daily` | `resources/tools/star_rail/March7th Launcher.exe` | 星穹铁道游戏本体 |
| `qq_reader_trial` | QQ阅读 | `qq_reader_trial` | `resources/tools/qq_reader/MaaQQReaderGUI.exe` | MuMu 12 (`mumu_manager`+`mumu_adb`) |

**全部流程共用**：`resources/runtime/adb.exe`（ADB 运行时，已保留进仓库，无需下载）。

### 要点

1. **`zenless`（绝区零）特殊**：入口不是单一 exe，而是 `resources/tools/zenless/.venv/Scripts/pythonw.exe` + `launch_args -m zzz_od.gui.app` + `cwd=.../zenless/src`。OneDragon 必须**完整解压整个工具目录**（含 `.venv`），不能只拷 `OneDragon-Launcher.exe`。
2. **`naruto` 与 `gumballs` 用同一个 `MFAAvalonia.exe`**——但它们是**两个不同项目**（MaaAutoNaruto / MaaGumballs），需分别从各自上游下载，分别放入各自目录。
3. **`maa` 和 `baas`**：`MAA.exe`（MaaAssistantArknights）、`baas.exe`（Blue Archive Auto Script）。
4. **`endfield` / `star_rail` / `zenless`** 是 **PC 游戏**，不依赖安卓模拟器，改为把游戏窗口置顶 + 启动游戏本体（`endfield.exe` / `StarRail.exe` / `ZenlessZoneZero.exe`），`required_host_tools` 只有游戏本体（endfield 需 `endfield_game`）。
5. **`qq_reader_trial`**：`required_host_tools` 是 `mumu_manager` + `mumu_adb`，走 MuMu 模拟器，但有独立测试流程（`once_per_day=False`）。
