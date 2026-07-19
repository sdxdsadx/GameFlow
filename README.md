# GameFlow 游戏每日自动化

GameFlow 是运行在本机的工作流控制器，负责启动雷电模拟器、等待 ADB、调用 MAA 或其他脚本、失败重试、记录历史并在最后清理模拟器。

## 首次配置

编辑 `config/workflow.json`：

1. 将 `tools.ldconsole` 改为雷电 9 的 `ldconsole.exe` 路径。
2. 将 `tools.adb` 改为雷电目录内的 `adb.exe` 路径。
3. 当前电脑已配置为使用 `E:\\MAA\\MAA-v5.3.1-win-x64\\MAA.exe` 桌面版，并通过其日志完成标记判断任务结束。
4. 确认主模拟器的 ADB 地址。雷电 9 主实例通常为 `127.0.0.1:5555`，多开实例通常依次为 `5557`、`5559`、`5561`。
5. MAA 已绑定雷电实例 `aknight`（实例编号 1、地址 `emulator-5556`），启动 MAA 后会直接运行其中已勾选的日常任务。

默认关闭每日定时执行。完成一次手动验证后，把 `daily_game.trigger.enabled` 改为 `true`。

## 使用

- 双击 `start.bat`：打开本地控制面板。
- 双击 `run_daily.bat`：直接执行每日任务。
- 碧蓝档案每日：`python main.py run blue_archive_daily`。
- 碧蓝航线每日：双击 `run_azur_lane_daily.bat`，或在控制面板勾选“碧蓝航线每日流程”。
- 碧蓝航线强制重跑：双击 `run_azur_lane_daily_force.bat`，可绕过“今日已成功执行”的防重复限制。
- 火影忍者每日：双击 `run_naruto_daily.bat`，强制重跑使用 `run_naruto_daily_force.bat`。
- 不思议迷宫每日：双击 `run_gumballs_daily.bat`，强制重跑使用 `run_gumballs_daily_force.bat`。
- 终末地每日：双击 `run_endfield_daily.bat`，强制重跑使用 `run_endfield_daily_force.bat`。
- 程序自检：`python main.py run self_test`
- 查看历史：`python main.py status`

控制面板仅监听 `127.0.0.1`，不会向局域网或互联网开放。

启动控制面板时会出现 Windows 管理员权限确认。BAAS 外层启动器自身以管理员权限运行，因此 GameFlow 也必须以相同权限启动，才能识别和点击 BAAS Pro 主窗口。

控制面板会以后台进程运行，不再保留容易被误关的命令窗口。浏览器页面关闭不会终止正在执行的任务；重新打开 `http://127.0.0.1:8765/` 即可继续查看。若电脑关机或 GameFlow 进程被强制结束，下一次启动会把遗留的 `running` 记录标记为 `interrupted`，避免显示错误状态。

## 每日流程编排

控制面板支持同时管理明日方舟和碧蓝档案：

1. 勾选本次需要运行的一个或两个任务。
2. 使用上下箭头调整启动顺序。
3. “最多同时执行”选择 `1` 时按顺序串行，选择 `2` 时两个游戏并行。
4. 点击“开始每日流程”。调度器会按照列表顺序启动任务，任意时刻不会超过所设并发数。

排序和并发选择保存在当前浏览器中。也可以使用每个任务右侧的“单独运行”按钮。点击“停止全部”会通知所有正在运行的任务停止，并继续执行各自标记为 `run_always` 的截图和模拟器清理步骤。

## 添加其他脚本

在 `steps` 中加入命令执行器：

```json
{
  "id": "my_script",
  "runner": "command",
  "command": "D:\\GameTools\\script.exe",
  "args": ["--daily"],
  "timeout": 900,
  "retry": 1
}
```

支持的执行器：

- `ldplayer`：启动、重启或关闭指定雷电实例。
- `adb`：等待设备、启动/停止应用、执行 Android 命令、保存截图。
- `maa`：运行 maa-cli 自定义任务。
- `maa_gui`：启动 MAA 桌面版，并等待 `AllTasksCompleted` 日志标记。
- `baas_gui`：启动 BAAS Pro 的 `baas1` 配置，并等待“任务全部执行成功”日志标记。
- `alas_gui`：启动 AzurLaneAutoScript，让其自行启动模拟器和任务；检测本次调度开始后出现 `No task pending`，并等待20秒无新日志才结束本轮。
- `naruto_shadow`：在雷电内启动“影分身”，依次点击“启动功能”、浮窗和“启动”，并观察结束弹窗或主页停留状态。
- `naruto_reward_verify`：进入火影“奖励”页，确认每日活跃度达到100且四个日常宝箱均已领取。
- `gumballs_gui`：启动不思议迷宫 MFAAvalonia 脚本，延迟点击“开始任务”，并按日志连续执行两轮。
- `maaend_gui`：启动 MaaEnd 的“全套日常”，确认四项主要任务完成后进入截图和清理阶段。
- `window_screenshot`：在关闭 PC 游戏前保存指定 Windows 游戏窗口截图。

## 终末地环境

- 脚本入口：`G:\maazmd\MaaEnd.exe`；游戏入口：`E:\Hypergryph Launcher\games\Endfield Game\Endfield.exe`。
- 使用 MaaEnd 已配置的“全套日常”，程序启动后优先使用 MaaEnd 自带的自动运行；45秒内没有开始日志时，备用点击底部“开始任务”按钮。
- MaaEnd 的四项主要任务全部完成并切换到收尾阶段后才算成功，不会把程序打开或任务提交误判成完成。
- 原 MaaEnd 收尾任务不再直接关闭 `Endfield.exe`。GameFlow 会先保存 `logs/endfield_final.png`，随后关闭终末地和 MaaEnd，因此该截图也会作为独立图片附件加入每日邮件。
- 最长等待时间为3小时；定时执行默认关闭，可先在控制面板单独运行测试。

## 不思议迷宫环境

- 雷电实例：`不思议迷宫`，实例编号 `2`，ADB 地址 `emulator-5558`。
- 脚本入口：`D:\bushiyi\MFAAvalonia.exe`。
- 模拟器和脚本启动后等待120秒，再按主窗口右上角的固定相对位置点击“开始任务”。
- 每轮必须先在本次新增日志中出现“用户操作：启动任务”，随后出现“任务已全部完成！”，才算该轮成功；历史日志不会参与判定。
- 第一轮结束后等待3秒并再次点击“开始任务”。第二轮确认完成后关闭脚本，然后保存 `logs/gumballs_final.png` 并关闭雷电实例2。
- 最长总等待时间为4小时；点击后180秒仍没有启动日志会直接报错。失败或手动停止时也会执行脚本和模拟器清理。

## 火影忍者环境

- 雷电实例：`fire`，实例编号 `0`，ADB 地址 `emulator-5554`。
- 影分身包名：`com.yy.yfs`；火影忍者包名：`com.tencent.KiHan`。
- 正常结束时会识别影分身的大型白色结束弹窗并点击“确定”。
- 影分身启动时若弹出竖屏“功能说明”，程序会先点击其中的“确定”再操作浮窗；竖屏白色弹窗不会再被当成游戏结束。
- 启动后每隔60秒截图，直到首次识别到火影大厅。
- 阶段1确认大厅后开始90分钟计时。计时结束后不再进入奖励页检查，直接保存最终截图并关闭雷电实例0。
- 奖励核验截图：`logs/naruto_activity_check.png`；最终截图：`logs/naruto_final.png`。无论成功或失败都会关闭雷电实例0。

## 碧蓝航线环境

- 脚本入口：`E:\AzurLaneAutoScript\Alas.exe`。
- 模拟器由 ALAS 自行启动，GameFlow 不配置或控制模拟器地址。
- 模拟器为MuMu 12主实例 `0`（名称“碧蓝航线”，ADB `127.0.0.1:16384`）。ALAS完成后，GameFlow调用 `D:\Program Files\Netease\MuMu Player 12\shell\MuMuManager.exe control --vmindex 0 shutdown` 关闭该实例。
- ALAS 官方自动运行保持关闭（`Run: null`）。GameFlow 启动 ALAS 并等待20秒后，自动点击界面顶部的“启动”按钮；优先按控件名称点击，备用窗口相对位置为43%、10.5%。单独打开 ALAS 时不会自动执行任务。
- 完成依据：本次日志先出现 `Scheduler: Start task`，之后出现 `No task pending`，并保持20秒没有新任务日志。
- 最长等待时间：2小时。

## 碧蓝档案环境

- 雷电实例：`碧蓝档案`，实例编号 `3`。
- ADB 地址：`emulator-5560`。
- BAAS 启动入口：直接使用 `E:\baas\baas-pro\baas.exe`，绕过已经无法稳定拉起窗口的旧外层 `Baas_Windows_V4.exe`。
- BAAS 配置：`baas1`，已加入 `configs/app.yaml` 的 `auto_start`。
- BAAS 启动前会清理同程序遗留的旧进程，保证 `auto_start` 在唯一主实例中触发。
- 如果启动后45秒 `baas1` 日志没有新增，程序会自动操作 BAAS Pro GUI；窗口尚未出现时每10秒重试，最长等待120秒。按照实际界面先点击左侧 `baas1`（窗口相对位置4%、15%），再点击顶部绿色“启动”（37.4%、10.8%）。
- BAAS 报告整轮结束后，程序只检查本次新增日志里的最后运行任务。只有“工作任务”已经开始并执行完成、且之后没有其他任务开始，才允许结束；否则保持 BAAS 和模拟器运行，每隔10分钟重新检查。
- 最终截图：`logs/blue_archive_final.png`。

碧蓝档案只有同时满足以下条件才会标记成功：本次 BAAS 日志出现“任务全部执行成功”，并且最后完成的任务为“工作任务”。不再点击游戏内任务页，也不再根据灰色按钮判断。

BAAS 的成功标记还必须位于本次“模拟器连接成功/开始执行”之后。检测到成功后继续观察 20 秒；期间只要日志继续追加，就取消该成功候选并继续等待，避免 BAAS 尚在执行时被提前关闭。

碧蓝档案定时执行默认关闭。完整手动验证成功后，可将 `blue_archive_daily.trigger.enabled` 改为 `true`。
- `command`：运行任意命令行脚本或程序。
- `delay`：可中断等待。

`run_always: true` 表示即使前面的步骤失败也必须执行，适合截图和关闭模拟器。`continue_on_error: true` 表示该步骤失败不阻断后续步骤。

## 数据位置

- `logs/gameflow.log`：完整运行日志。
- `logs/final.png`：每日流程结束截图。
- `data/gameflow.db`：运行和步骤历史。

## 每日截图邮件

- 每个游戏都会在关闭模拟器前保存一张游戏内最终截图。碧蓝航线通过 MuMu 12 的 ADB 地址 `127.0.0.1:16384` 截图。
- 批量“开始每日流程”全部结束后，仅收集本批新生成的截图，转换为适合邮件的清晰JPEG，并作为多张独立图片附件直接发送到 `26142328@qq.com`，不再生成ZIP。
- QQ 邮箱发件凭据不会写进配置文件。当前使用 `26142328@qq.com` 自发自收，通过 `smtp.qq.com:465` SSL 发信；Windows 用户环境变量保存发件地址和SMTP授权码。
- 可直接双击 `configure_email.bat`，按提示输入 QQ 邮箱地址和SMTP授权码；授权码输入时不会显示在窗口中。
- 未配置凭据或发送失败时，原始最终截图仍保留在 `logs`，控制面板完成消息和日志会显示具体原因。

如果某一步找不到程序、超时或返回非零退出码，控制面板和日志会显示明确原因。
