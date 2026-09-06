# GameFlow

GameFlow 是一个本机、离线控制面的游戏自动化编排器。正式产物将第三方自动化运行时放在 `resources/tools`，代码和配置只使用相对项目根路径，不会在运行时下载脚本或检查版本。

## 运行

1. 双击 `start.bat`。
2. 使用恢复后的旧版作战面板选择任务、调整顺序和并行数，然后开始每日流程；GameFlow 会按实例编号启动对应模拟器。

页面仅监听 `127.0.0.1`。旧版面板保持原有布局和功能，不增加端口控件；`data/runtime_settings.json` 按工作流保存实际发现的模拟器端点。端点只传给所属工作流和对应内置工具，不会覆盖其他雷电或 MuMu 实例。QQ 阅读始终使用产物内的 `resources/runtime/adb.exe`。

旧版面板保留任务开关、SKIPPED 标签、拖拽与上下排序、并行数、单任务运行、强制重跑、逐任务取消、全部停止、实时日志、运行历史和今日完成状态。

## 构建

项目不自动安装依赖。构建机需预先安装 `requirements.txt` 中的包，然后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

构建脚本会依次运行测试、便携审计、PyInstaller 正式构建，并将 `config` 与 `resources` 复制到 `dist/GameFlow`。产物可以整体复制到其他 Windows 主机。

资源复制由 `config/resource_manifest.json` 控制；测试、缓存和历史日志不会进入产物。新版本先在 `build/release-staging` 完整生成并审计，成功后替换 `dist/GameFlow`，上一版本保留在 `dist/GameFlow.previous`。

## 内置运行时

| 工作流 | 内置目录 | 入口 |
|---|---|---|
| 明日方舟 | `resources/tools/maa` | `MAA.exe` |
| 碧蓝档案 | `resources/tools/baas` | `baas.exe` |
| 碧蓝航线 | `resources/tools/alas` | `Alas.exe` |
| 火影忍者 | `resources/tools/naruto` | `MFAAvalonia.exe` |
| 不思议迷宫 | `resources/tools/gumballs` | `MFAAvalonia.exe` |
| 终末地 | `resources/tools/endfield` | `MaaEnd.exe` |
| 绝区零 | `resources/tools/zenless` | `OneDragon-Launcher.exe` |
| 星穹铁道 | `resources/tools/star_rail` | `March7th Launcher.exe` |
| QQ 阅读测试流程 | `resources/tools/qq_reader` | `MaaQQReaderGUI.exe` |

ADB 位于 `resources/runtime/adb.exe`。雷电、MuMu 和终末地游戏本体属于宿主机安装，不写入便携的 `config/workflow.json`；首次启动会从环境变量、Program Files 和各本地盘的常见安装目录中自动发现，并保存到 `data/host_settings.json`。

自动发现失败或迁移到新电脑后，可用目录或可执行文件重新配置：

```powershell
python .\main.py configure-host --ldplayer "<LDPlayer9目录>" --mumu "<MuMu shell目录>" --endfield "<Endfield.exe或所在目录>"
```

不带参数执行 `configure-host` 可查看当前发现/保存的路径。流程启动前会验证其声明的宿主机程序；路径无效时会直接报告字段，不会启动自动化脚本。

## 部署边界

模拟器虚拟机、游戏客户端、账号数据及 Windows 图形桌面不是构建资源。目标主机仍需安装雷电或 MuMu；终末地流程还需安装游戏本体。其他 PC 游戏自动化工具继续使用其自身的便携配置或启动器发现游戏。

版本检查、自动更新、公告/推广界面、邮件发送、远程图片和定时后台任务已从 GameFlow 运行路径移除。第三方运行时的活动配置也已关闭更新检查；运行时不会替换内置文件。
