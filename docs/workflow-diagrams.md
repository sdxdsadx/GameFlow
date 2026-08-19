# GameFlow 当前流程图与规划图

本文档按当前代码和配置绘制，不是理想化设计稿。基线来源为：

- `config/workflow.json`：8 个游戏工作流、步骤、超时、重试和互斥配置。
- `gameflow/engine.py`：单任务执行、批量调度、并行、互斥、取消和“明日更新”状态。
- `gameflow/runners.py`：模拟器、ADB、GUI 点击、日志判定、黑屏恢复和游戏专用逻辑。
- `gameflow/web.py`：网页状态、排序、参与开关、单任务执行和批量执行。
- `gameflow/store.py`：SQLite 运行历史、凌晨 04:00 游戏日和持久化标志。
- `gameflow/diagnostics.py`、`gameflow/mailer.py`：错误现场、最终截图和邮件证据。

## 1. 当前系统边界

| 项目 | 当前实现 |
|---|---|
| 游戏流程 | 明日方舟、碧蓝档案、碧蓝航线、火影忍者、不思议迷宫、终末地、绝区零、星穹铁道 |
| 执行入口 | 网页控制台、命令行、各游戏批处理文件、关闭状态的定时触发器 |
| 总并行上限 | 1～2 个工作流 |
| 专用互斥组 | 终末地、绝区零、星穹铁道同属 `pc_hoyoverse_daily`，组内最多运行 1 个 |
| 其他游戏 | 不设置互斥，可在总并行上限内彼此并行，也可与一个 PC 游戏并行 |
| 防重复周期 | 游戏日从凌晨 04:00 到次日凌晨 04:00 |
| 普通失败重试 | 全局最多尝试 2 次，即首轮失败后再运行一轮 |
| 最终证据 | 游戏截图、失败现场、运行历史；批量流程结束后发送独立图片邮件 |

## 2. 系统总览图

```mermaid
flowchart LR
    subgraph Entry["入口层"]
        Start["start.bat / launch_admin.ps1<br/>管理员方式启动"]
        CLI["main.py run / status"]
        Browser["本地网页控制台<br/>排序、参与、并行、取消"]
        Timer["Scheduler<br/>定时触发器"]
    end

    subgraph Control["控制层"]
        Main["main.py<br/>加载配置并装配服务"]
        Web["web.py<br/>REST 接口与实时状态"]
        Manager["WorkflowManager<br/>队列、并行、互斥、批量汇总"]
        Engine["Engine × 每个工作流<br/>步骤、重试、取消、收尾"]
    end

    subgraph Execute["执行层"]
        Registry["RUNNERS 注册表"]
        Base["基础 Runner<br/>command / delay / ADB / 雷电 / MuMu"]
        Game["游戏 Runner<br/>MAA / BAAS / ALAS / 影分身<br/>MFA / MaaEnd / 日志型 GUI"]
        Focus["桌面 GUI 点击锁<br/>置前并串行完成点击"]
    end

    subgraph External["外部程序与游戏"]
        LD["雷电模拟器<br/>实例 0 / 1 / 2 / 3"]
        MuMu["MuMu 12<br/>实例 0"]
        PC["PC 游戏<br/>终末地 / 绝区零 / 星穹铁道"]
        Scripts["脚本 GUI<br/>MAA / BAAS / ALAS / MFA<br/>MaaEnd / OneDragon / March7th"]
    end

    subgraph Evidence["状态与证据"]
        Config["workflow.json<br/>流程配置"]
        Prefs["ui_preferences.json<br/>顺序、参与、并行"]
        DB["gameflow.db<br/>运行与步骤历史、明日更新标志"]
        Logs["gameflow.log 与脚本日志"]
        Shots["最终截图与 daily_evidence"]
        Diag["error_diagnostics<br/>失败时的设备、进程、窗口、日志"]
        Mail["QQ 邮箱<br/>独立图片附件"]
    end

    Start --> Main
    CLI --> Main
    Main --> Web
    Main --> Manager
    Main --> Timer
    Browser <-->|"REST 状态与控制"| Web
    Timer --> Manager
    Web --> Manager
    Config --> Main
    Prefs <--> Web
    Manager --> Engine
    Engine --> Registry
    Registry --> Base
    Registry --> Game
    Game --> Focus
    Base --> LD
    Base --> MuMu
    Game --> Scripts
    Game --> LD
    Game --> MuMu
    Game --> PC
    Engine --> DB
    Base --> Logs
    Game --> Logs
    Base --> Shots
    Game --> Shots
    Engine -.->|"失败时、重试前"| Diag
    Manager -->|"批量完成且未停止全部"| Mail
    Shots --> Mail
```

## 3. 每日队列、并行和互斥规划图

网页保存任务顺序、每个任务是否参加每日流程以及最大并行数。开始每日流程后，调度器按顺序建立 `pending` 队列，但队首若与当前任务互斥，会继续寻找后面的兼容任务。

```mermaid
flowchart TD
    A["读取网页保存的任务顺序与参与开关"] --> B["过滤 self_test、重复项和 SKIPPED 项"]
    B --> C["限制最大并行数为 1～2"]
    C --> D["建立 pending 队列"]
    D --> E{"还有空闲执行槽？"}
    E -->|"否"| W["等待任一 active 任务结束或被取消"]
    W --> E
    E -->|"是"| F["从 pending 头部向后寻找<br/>第一个不与 active 冲突的候选"]
    F --> G{"找到兼容候选？"}
    G -->|"否"| W
    G -->|"是"| H["启动该工作流的独立 Engine"]
    H --> I["加入 active 并持续刷新 GUI 状态"]
    I --> J{"任务结束？"}
    J -->|"否"| I
    J -->|"是"| K["记录 success / failed / skipped<br/>needs_update / cancelled"]
    K --> L{"pending 或 active 仍有任务？"}
    L -->|"是"| E
    L -->|"否"| M["汇总本批状态与截图"]
    M --> N["发送独立图片邮件"]
```

```mermaid
flowchart LR
    Scheduler["总调度器<br/>全局最多同时 2 个"] --> SlotA["执行槽 A"]
    Scheduler --> SlotB["执行槽 B"]

    subgraph Free["无互斥组，可彼此并行"]
        FreeGate["无组内互斥检查"]
        AK["明日方舟"]
        BA["碧蓝档案"]
        AL["碧蓝航线"]
        Naruto["火影忍者"]
        Gumballs["不思议迷宫"]
        FreeGate --> AK
        FreeGate --> BA
        FreeGate --> AL
        FreeGate --> Naruto
        FreeGate --> Gumballs
    end

    subgraph Exclusive["互斥门：pc_hoyoverse_daily"]
        Gate{"组内是否已有任务运行？"}
        Endfield["终末地"]
        ZZZ["绝区零"]
        HSR["星穹铁道"]
        Gate --> Endfield
        Gate --> ZZZ
        Gate --> HSR
    end

    SlotA --> FreeGate
    SlotB --> FreeGate
    SlotA --> Gate
    SlotB --> Gate
```

并行关系如下：

| 组合 | 是否允许 | 原因 |
|---|---:|---|
| 两个模拟器类任务 | 是 | 不在互斥组内，且未超过总并行上限 |
| 一个模拟器类任务 + 一个 PC 游戏任务 | 是 | PC 互斥只约束组内成员 |
| 终末地 + 绝区零 | 否 | 同属 `pc_hoyoverse_daily` |
| 终末地 + 星穹铁道 | 否 | 同属 `pc_hoyoverse_daily` |
| 绝区零 + 星穹铁道 | 否 | 同属 `pc_hoyoverse_daily` |
| 任意三个任务同时运行 | 否 | 总并行上限为 2 |

即使两个工作流并行，桌面脚本的“窗口置前 + 点击”也会通过全局点击锁依次完成，避免两个 GUI 同时争夺鼠标焦点。

## 4. 通用工作流生命周期

```mermaid
flowchart TD
    A["收到单任务或批量启动请求"] --> B{"工作流存在且当前空闲？"}
    B -->|"否"| X["拒绝启动；批量中记为 skipped"]
    B -->|"是"| C{"once_per_day 且今日已成功？"}
    C -->|"是，且非 force"| X
    C -->|"否或 force"| D["runs 表写入 running"]
    D --> E{"还有配置步骤？"}
    E -->|"是"| F["按顺序取下一步骤"]
    F --> G{"此前已有失败，且本步非 run_always？"}
    G -->|"是"| E
    G -->|"否"| H{"收到取消，且本步非 run_always？"}
    H -->|"是"| E
    H -->|"否"| I["调用对应 Runner"]
    I --> J{"Runner 成功？"}
    J -->|"是"| K["step_runs 记录成功"]
    K --> E

    J -->|"否"| L["先保存错误现场与 step_runs"]
    L --> M{"状态为 skipped 或 needs_update？"}
    M -->|"是"| MU["更新记 needs_update<br/>维护记 skipped<br/>设置次日额外等待标志"]
    MU --> R["立即结束本轮主任务<br/>不做普通重试"]
    M -->|"否"| N{"请求黑屏或模拟器恢复？"}
    N -->|"是"| O["重启对应模拟器并等待 Android 就绪"]
    N -->|"否"| P{"本步骤总尝试次数小于 2？"}
    O --> P
    P -->|"是"| I
    P -->|"否"| R
    R --> T{"continue_on_error？"}
    T -->|"是"| E
    T -->|"否"| Q["保存失败状态<br/>后续只运行 run_always 步骤"]
    Q --> E

    E -->|"否"| U{"最终是否收到取消事件？"}
    U -->|"是"| V["最终状态 cancelled"]
    U -->|"否"| W{"是否保存过失败状态？"}
    W -->|"否"| S["最终状态 success"]
    W -->|"是"| Y["最终状态 failed / skipped / needs_update"]
    X --> Z["结束本次启动请求"]
```

更新或维护会在当前 Runner 内立即形成明确状态并保存现场，不会继续尝试启动点击。用户取消后，Engine 会改用独立的清理上下文执行 `run_always` 步骤，因此取消事件不会再次打断最终截图和模拟器、脚本清理。普通失败在本步骤局部尝试耗尽后，仍由全局机制完整重跑一次工作流。

### 今日状态模型

```mermaid
stateDiagram-v2
    state "今日未完成" as Pending
    state "排队中" as Queued
    state "执行中" as Running
    state "正常结束" as Success
    state "异常结束" as Failed
    state "已跳过" as Skipped
    state "需要更新" as NeedsUpdate
    state "已取消" as Cancelled
    state "上次进程意外退出" as Interrupted

    [*] --> Pending
    Pending --> Queued: 加入每日队列
    Pending --> Running: 单独运行
    Queued --> Running: 获得执行槽
    Queued --> Cancelled: 取消等待任务
    Running --> Success
    Running --> Failed
    Running --> Skipped
    Running --> NeedsUpdate
    Running --> Cancelled: 用户取消
    Running --> Interrupted: GameFlow 异常退出后下次启动修复记录
    Success --> Running: force 强制重跑
```

凌晨 04:00 切换到新的游戏日，GUI 的当日运行状态和上一批徽标重新从 `pending` 开始计算；任务顺序、最大并行数和“每日启动/SKIPPED”参与偏好继续保留。

## 5. 八个游戏的当前流程

### 5.1 总流程对照表

八个流程都配置了各自的精确更新和维护标志：更新统一返回 `needs_update`，维护统一返回 `skipped`，两者都会立即结束本轮主任务、留存证据并安排下一个游戏日启动时额外等待10分钟。所有启动点击、启动确认和启动后日志停滞也都有明确上限。

| 工作流 | 启动链 | 核心完成判定 | 异常与恢复 | 最终证据和清理 |
|---|---|---|---|---|
| 明日方舟 | 雷电 1 → ADB `emulator-5556` → MAA | MAA 本轮日志出现 `AllTasksCompleted` | 黑屏5分钟重启模拟器；启动最多点6次；启动后日志静默上限10分钟 | ADB 截图 `final.png` → 关闭雷电 1 |
| 碧蓝档案 | 雷电 3 → ADB `emulator-5560` → 管理员 BAAS | 本轮工作证据成立，且 BAAS 队列稳定为空10秒 | 忙碌或队列未知时禁止盲点；黑屏、日志停滞、ATX卡死和重复探测均有限恢复 | 工作任务奖励核验 → `blue_archive_final.png` → 关闭雷电 3 |
| 碧蓝航线 | MuMu 0 / ADB `127.0.0.1:16384` → ALAS | 本轮先出现 `Scheduler: Start task`，再出现 `No task pending`，并静默20秒 | 启动最多点5次；启动后日志静默上限10分钟；ADB/模拟器错误有限恢复 | 重连 ALAS ADB → `azur_lane_final.png` → 关闭 MuMu 0 |
| 火影忍者 | 雷电 0 → ADB `emulator-5554` → 影分身 | 确认浮窗进入终止态，识别大厅后计时5400秒 | 横竖屏、继续弹窗和浮窗操作均限制点击次数；黑屏有限恢复 | `naruto_final.png` → 关闭雷电 0 |
| 不思议迷宫 | 雷电 2 → ADB `emulator-5558` → MFAAvalonia | 每轮先有“用户操作：启动任务”，再有“任务已全部完成！”，共两轮 | 首次延迟2分钟；每轮启动最多3次；业务日志静默10分钟后最多重启脚本1次 | `gumballs_final.png` → 关闭雷电 2 |
| 终末地 | MaaEnd → 置前 `Endfield.exe` | 收尾切到 `Dummy Controller`，并确认最终任务提交日志 | 启动最多点10次；框架日志停滞、信用菜单和每日操作均有限恢复 | Runner 保存 `endfield_final.png` → 关闭游戏和 MaaEnd |
| 绝区零 | OneDragon → 点击“启动一条龙” → 置前游戏 | 日志出现“一条龙执行成功、全部结束” | 启动最多点20次；启动后日志静默上限10分钟；状态停滞有限恢复 | 活跃度/奖励标志时保存 `zenless_final.png` → 关闭游戏 |
| 星穹铁道 | March7th `main -e` → 公告检查 → 点击“完整运行” | 仅“每日实训已完成”可完成；“每日实训未完成”具有否决权 | 启动最多20次；新版本公告直接按 `needs_update` 结束，不点击“好的”继续更新 | 完成或奖励证据时保存 `star_rail_final.png` → 关闭游戏 |

### 5.2 模拟器类游戏

```mermaid
flowchart LR
    subgraph AK["明日方舟"]
        AK1["雷电 1"] --> AK2["ADB 就绪并稳定 60 秒"]
        AK2 --> AK3["置前 MAA 并启动任务"]
        AK3 --> AK4["AllTasksCompleted"]
        AK4 --> AK5["最终截图"]
        AK5 --> AK6["关闭雷电 1"]
    end

    subgraph BA["碧蓝档案"]
        BA1["雷电 3"] --> BA2["ADB 就绪"]
        BA2 --> BA3["管理员方式启动 BAAS"]
        BA3 --> BA4["本轮工作证据 + 队列稳定为空"]
        BA4 --> BA5["进入工作任务页核验奖励"]
        BA5 --> BA6["最终截图"]
        BA6 --> BA7["关闭雷电 3"]
    end

    subgraph AL["碧蓝航线"]
        AL1["启动并确认 MuMu 0"] --> AL2["启动 ALAS 并点击启动"]
        AL2 --> AL3["Start task → No task pending"]
        AL3 --> AL4["重连 ADB 并截图"]
        AL4 --> AL5["关闭 MuMu 0"]
    end

    subgraph N["火影忍者"]
        N1["雷电 0"] --> N2["启动影分身"]
        N2 --> N3["继续 / 启动功能"]
        N3 --> N4["识别并点击悬浮窗"]
        N4 --> N5["点击衍生启动并确认终止态"]
        N5 --> N6["识别大厅后等待 90 分钟"]
        N6 --> N7["最终截图并关闭雷电 0"]
    end

    subgraph G["不思议迷宫"]
        G1["雷电 2 + MFA"] --> G2["等待 2 分钟"]
        G2 --> G3["第 1 轮：启动 → 完成"]
        G3 --> G4["第 2 轮：启动 → 完成"]
        G4 --> G5["最终截图并关闭雷电 2"]
    end
```

### 5.3 PC 游戏互斥组

```mermaid
flowchart TD
    Gate{"pc_hoyoverse_daily<br/>当前是否已有成员运行？"}
    Gate -->|"有"| Wait["保持排队，等待执行槽和互斥锁"]
    Wait --> Gate
    Gate -->|"没有"| Pick{"本轮候选"}

    Pick --> E1["终末地：启动 MaaEnd"]
    E1 --> E2["置前 Endfield"]
    E2 --> E3["收尾标志 + 最终提交"]
    E3 --> E4["截图并关闭游戏、脚本"]

    Pick --> Z1["绝区零：启动 OneDragon"]
    Z1 --> Z2["置前并点击启动一条龙"]
    Z2 --> Z3["跟踪日志状态与定点恢复"]
    Z3 --> Z4["完成标志截图并关闭游戏"]

    Pick --> H1["星穹铁道：启动 March7th"]
    H1 --> H2{"是否出现新版本公告？"}
    H2 -->|"是"| H5["直接 needs_update<br/>留证据并进入收尾"]
    H2 -->|"否"| H3["点击完整运行并跟踪日志"]
    H3 --> H4{"每日实训是否明确完成<br/>且没有“未完成”否决？"}
    H4 -->|"是"| H6["奖励证据截图并关闭游戏"]
    H4 -->|"否"| H3
    H5 --> H7["最终截图并关闭游戏、脚本"]
```

## 6. 碧蓝档案详细状态图

这是当前判断条件最多的流程。BAAS 的“任务全部执行成功”只是候选证据；主步骤必须确认本轮确实工作过并且队列稳定为空，随后再核验游戏内工作任务奖励。

```mermaid
flowchart TD
    A["启动雷电 3 并确认 Android 就绪"] --> B["清理旧 BAAS 进程"]
    B --> C["管理员方式启动 BAAS Pro"]
    C --> D{"存在“明日更新”标志？"}
    D -->|"是"| E["脚本启动后等待10分钟<br/>Runner确认消费后才清除标志"]
    D -->|"否"| F["读取本轮新日志与 baas1 队列"]
    E --> F

    F --> G{"队列状态？"}
    G -->|"明确忙碌"| H["持续监控，禁止备用坐标点击<br/>避免把“启动”误点成“停止”"]
    G -->|"未知"| H2["继续读取日志和队列<br/>禁止盲目坐标点击"]
    H --> I["解析本轮任务、日志和队列变化"]
    H2 --> I
    G -->|"明确空闲且需要启动"| K["置前 BAAS → 选 baas1 → 点击启动<br/>受启动次数和超时上限约束"]
    K --> I

    I --> L{"黑屏超过 5 分钟？"}
    L -->|"是"| M["保存证据 → 重启雷电 3 → 重跑 BAAS 步骤"]
    L -->|"否"| N{"ATX/任务卡死或重复探测？"}
    N -->|"是，可恢复次数内"| O["等待或定点恢复后继续监控"]
    O --> I
    N -->|"是，超过上限且识别到更新画面"| P["needs_update<br/>写入“明日更新”标志"]
    N -->|"否"| Q{"是否已有本轮工作证据<br/>且队列稳定为空10秒？"}

    Q -->|"否"| I
    Q -->|"是"| T["BAAS 主步骤成功"]

    T --> U["按固定坐标进入“工作任务”页"]
    U --> V{"每日进度条是否满格？"}
    V -->|"否"| W["验证失败并保存当前奖励页"]
    V -->|"是"| X{"“领取”和“一键领取”是否都为灰色？"}
    X -->|"否"| Y["尝试领取仍可领取的奖励并重新截图"]
    Y --> X
    X -->|"是"| Z["奖励验证成功并保存最终证据图"]
    Z --> AA["关闭雷电 3"]

    P --> AB["执行 run_always 最终截图和关闭"]
    W --> AB
    M --> C
```

碧蓝档案的三项成功条件：

1. 本轮出现新的任务开始、成功日志或有效队列变化，不能沿用历史完成记录。
2. `baas1` 队列稳定为空；队列忙碌或未知时均禁止备用坐标点击。
3. 游戏内每日任务进度满格，并且“领取”和“一键领取”按钮最终都变灰。

## 7. 黑屏、更新、维护和错误证据链

```mermaid
flowchart TD
    R["Runner 正在监控游戏、脚本和日志"] --> B{"模拟器画面连续黑屏 5 分钟？"}
    B -->|"是"| B1["保存黑屏证据"]
    B1 --> B2["请求 Engine 重启雷电或 MuMu"]
    B2 --> B3{"重启成功且仍有恢复次数？"}
    B3 -->|"是"| R
    B3 -->|"否"| F["步骤失败"]

    R --> U{"命中本游戏精确标志？"}
    U -->|"更新"| U1["状态 needs_update"]
    U -->|"服务器维护"| U2["状态 skipped"]
    U1 --> U3["立即停止本轮主任务"]
    U2 --> U3
    U3 --> U4["保存日志、截图、窗口与进程证据"]
    U4 --> U5["Store 写入 update_before_next_day=600 秒"]
    U5 --> U6["执行 run_always 最终截图与清理"]
    U6 --> U7["下一个游戏日启动对应 Runner"]
    U7 --> U8["Runner 实际等待10分钟并确认已消费"]
    U8 --> U9["Engine 才清除持久化标志"]

    R --> E{"日志报错、停滞、超时或验证失败？"}
    E -->|"是"| F
    F --> D["清理前保存 error_diagnostics"]
    D --> D1["report.json"]
    D --> D2["模拟器截图与前台 Activity"]
    D --> D3["脚本/游戏窗口与进程状态"]
    D --> D4["脚本日志尾部和 ALAS 错误文件"]
    D --> T{"第一次普通失败？"}
    T -->|"是"| T1["全局完整运行第2轮"]
    T1 --> R
    T -->|"否"| C["执行 run_always 截图与清理"]

    R --> S["正常完成"]
    S --> C
    R --> X{"用户取消？"}
    X -->|"是"| X1["切换到独立清理上下文"]
    X1 --> C
    C --> M{"整批自然结束？"}
    M -->|"是"| M1["复制本批新截图到 daily_evidence"]
    M1 --> M2["压缩画质但不打包<br/>以多张独立图片发送 QQ 邮件"]
    M -->|"停止全部"| M3["不发送本批邮件"]
```

八个流程的更新与维护都返回 `defer_update_next_day`。Engine 只负责把等待时间交给 Runner；只有 Runner 返回 `_startup_update_wait_consumed`，才会清除“明日更新”标志。若启动、取消或异常发生在等待真正完成之前，标志会保留到下一次运行。

## 8. 代码职责规划图

```mermaid
flowchart TB
    Config["配置层<br/>workflow.json"] --> Orchestration["编排层<br/>engine.py"]
    UI["交互层<br/>web.py"] --> Orchestration
    CLI["入口层<br/>main.py / daily.py"] --> Orchestration
    Orchestration --> Adapter["适配层<br/>runners.py"]
    Adapter --> Platform["平台层<br/>雷电 / MuMu / ADB / Win32"]
    Adapter --> Script["脚本层<br/>MAA / BAAS / ALAS / MFA / MaaEnd / OneDragon / March7th"]
    Orchestration --> State["状态层<br/>store.py / SQLite"]
    Adapter --> Observe["观测层<br/>日志、截图、窗口状态"]
    Orchestration --> Diagnose["诊断层<br/>diagnostics.py"]
    Observe --> Diagnose
    Orchestration --> Report["报告层<br/>mailer.py"]
    Observe --> Report
```

| 文件 | 应负责 | 不应承载 |
|---|---|---|
| `workflow.json` | 路径、坐标、标志、阈值、超时、步骤顺序 | 通用调度算法 |
| `engine.py` | 生命周期、重试、互斥、取消、状态汇总 | 某个游戏页面的点击细节 |
| `runners.py` | 平台和游戏适配、日志/画面判定 | 网页布局与任务排序偏好 |
| `web.py` | 展示、用户操作、偏好保存、API | 游戏完成判定 |
| `store.py` | 历史、游戏日、持久化标志 | 外部程序控制 |
| `diagnostics.py` | 失败瞬间的统一证据采集 | 改变游戏流程结果 |
| `mailer.py` | 收集本批新截图并发送 | 决定工作流是否成功 |

## 9. 已实现的可靠性基线与后续规划

```mermaid
flowchart LR
    Now["当前基线<br/>8 个游戏 + 统一网页调度"] --> Done["已实现可靠性闭环"]
    Done --> D1["8流程精确区分<br/>更新 needs_update / 维护 skipped"]
    Done --> D2["次日等待10分钟<br/>Runner确认消费后才清标志"]
    Done --> D3["启动点击、启动阶段<br/>运行后日志停滞均有上限"]
    Done --> D4["取消仍截图清理<br/>普通首轮失败全局再试1轮"]
    Done --> D5["BAAS 本轮证据 + 稳定空队列<br/>忙碌或未知时禁止盲点"]
    Done --> D6["星铁未完成否决奖励结束<br/>更新公告直接 needs_update"]

    Done --> Future["后续维护"]
    Future --> F1["坐标、ROI、模板和分辨率<br/>继续拆分为游戏配置资源"]
    Future --> F2["增加日志与截图样本的<br/>离线回放测试"]
    Future --> F3["网页直接打开错误现场<br/>并展示分阶段证据"]
```

### 已从当前代码确认的实现注意点

1. 八个工作流对应的 Runner 都会接收 `startup_update_wait`；等待未真正完成时，“明日更新”标志不会被提前清除。
2. 星穹铁道的新版本公告直接结束为 `needs_update`，不会点击“好的”继续更新；“每日实训未完成”优先于奖励截图标志。
3. `naruto_reward_verify` Runner 仍存在于代码中，但当前 `naruto_daily` 配置没有使用它；当前火影流程是识别大厅后等待5400秒，再截图并关闭模拟器。
4. 八个游戏的定时触发器目前全部关闭，当前主要入口仍是网页、命令行或批处理文件。
5. “每日启动/SKIPPED”是持久化参与偏好，不会在凌晨04:00自动改回启用；凌晨04:00刷新的是今日执行状态和批次结果。
6. `pc_hoyoverse_daily` 是互斥组内部名称，成员为终末地、绝区零和星穹铁道。

## 10. 修改流程时的核对清单

新增或修改游戏流程时，应同时核对：

- 启动：程序路径、权限、模拟器实例、ADB 地址、游戏窗口置前。
- 开始确认：不能只点击按钮，必须有日志、按钮状态或浮窗状态证明已启动。
- 心跳：明确日志多久不更新才算停滞，避免脚本正常等待时被提前结束。
- 完成：必须使用本轮新增日志或当前画面，不能把旧日志当作本轮完成。
- 奖励：重要奖励应有单独页面验证与最终截图，而不是只相信脚本完成标志。
- 更新与维护：区分 `needs_update`、维护 `skipped` 和普通 `failed`。
- 黑屏：超过 5 分钟保存证据，最多重启一次模拟器，再重跑当前脚本步骤。
- 收尾：截图和关闭步骤使用 `run_always`，取消和失败也要清理外部程序。
- 并行：确认是否需要加入互斥组，且桌面 GUI 点击必须经过置前和全局点击锁。
- 测试：至少覆盖正常日志、旧日志、停滞、黑屏、更新、取消和第二轮重试。
