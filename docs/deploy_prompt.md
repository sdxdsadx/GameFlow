# GameFlow 台式机部署 —— Hermes 提示词

> 用途：把下面整段（含 `=== 主提示词 ===` 到结尾）复制粘贴到**台式机上的 Hermes 桌面应用对话**中，指挥它自动完成 GameFlow 本地部署。
> 前提：台式机是 Windows、已装 Git、Python 3.8+（建议 3.10-3.12）、能访问 GitHub（必要时配置代理）。

---

## === 主提示词 ===

你是部署工程师。请在**当前这台 Windows 电脑**上，把开源游戏自动化编排器 **GameFlow** 完整部署到本地，使其可以正常运行 9 个游戏每日流程。全程用中文汇报，每完成一个大阶段向我确认一次。

## 背景（已核实的信息，直接采用，不要质疑）

- **源码仓库**（私有）：`https://github.com/sdxdsadx/GameFlow`，分支 `main`。这是剥离了第三方运行时后的轻量源码工程（~25MB），包含 `main.py`、`gameflow/` 包、`config/workflow.json`（9 个每日流程的编排配置）、`build.ps1`、`requirements.txt`、`docs/tools_upstream_index.md`（含全部运行时上游清单）。
- **部署目标目录**：`G:\GameFlow`（若不存在则创建；若用户指定其他路径则用用户的）。
- **9 个第三方自动化运行时**：GameFlow 源码**不含**它们，需按 `docs/tools_upstream_index.md` 从各自上游下载，放入 `G:\GameFlow\resources\tools\<目录名>\`。这是部署的核心工作量。
- **QQ 阅读工具**在私有仓库 `https://github.com/sdxdsadx/MaaQQReader`，且含 `assets/MaaCommonAssets` 子模块（MaaXYZ 公开仓库，OCR 模型），必须 `--recurse-submodules` 拉取。
- **运行时总览**（工具目录 → 上游仓库 → 入口 exe）：

| GameFlow 目录 | 游戏 | 上游仓库 | 入口 |
|---|---|---|---|
| `resources/tools/maa` | 明日方舟 | `MaaAssistantArknights/MaaAssistantArknights` | `MAA.exe` |
| `resources/tools/baas` | 碧蓝档案 | 无公开 GitHub 上游（作者经 bilibili/QQ群分发） | `baas.exe` |
| `resources/tools/alas` | 碧蓝航线 | `LmeSzinc/AzurLaneAutoScript` | `Alas.exe` |
| `resources/tools/naruto` | 火影忍者 | `duorua/narutomobile` | `MFAAvalonia.exe` |
| `resources/tools/gumballs` | 不思议迷宫 | `KhazixW2/MaaGumballs` | `MFAAvalonia.exe` |
| `resources/tools/endfield` | 终末地 | `MaaEnd/MaaEnd` | `MaaEnd.exe` |
| `resources/tools/zenless` | 绝区零 | `OneDragon-Anything/ZenlessZoneZero-OneDragon` | `.venv/Scripts/pythonw.exe`（完整解压含 venv） |
| `resources/tools/star_rail` | 星穹铁道 | `moesnow/March7thAssistant` | `March7th Launcher.exe` |
| `resources/tools/qq_reader` | QQ阅读 | `sdxdsadx/MaaQQReader`（私有） | `MaaQQReaderGUI.exe` |

## 阶段 1：GitHub 认证（必须最先做）

1. 运行 `gh auth status` 检查 GitHub CLI 是否已登录。
2. 若未登录：运行 `gh auth login --hostname github.com --git-protocol https --web`，把显示的一次性代码和 `https://github.com/login/device` 链接给我，**等我完成授权后再继续**（授权码约 15 分钟有效）。
3. 登录成功后，用 `gh auth status` 确认账号是 `sdxdsadx` 且 token scopes 包含 `repo`。
4. 同时确认 `git config --global credential.helper` 可用（Windows 推荐 Git Credential Manager），确保后续 push/pull 免交互。

## 阶段 2：克隆源码仓库

1. `git clone --recurse-submodules https://github.com/sdxdsadx/GameFlow.git G:\GameFlow`
   - 若提示子模块失败，先 `git clone https://github.com/sdxdsadx/GameFlow.git G:\GameFlow`，再 `cd G:\GameFlow && git submodule update --init --recursive`。
2. 克隆后检查：`G:\GameFlow\main.py`、`G:\GameFlow\config\workflow.json`、`G:\GameFlow\gameflow\` 存在；`G:\GameFlow\resources\runtime\adb.exe` 存在（源码自带，6MB）。
3. 确认 `G:\GameFlow\resources\tools\` 为空（正常，待下载）。
4. 用 `git log --oneline -1` 汇报当前版本。

## 阶段 3：安装 Python 依赖

1. 检查 Python：`python --version`（要求 3.8+，建议 3.10-3.12；若 <3.8 用 `py -3.10` 或 uv 托管）。
2. 建虚拟环境并装依赖：
   ```powershell
   cd G:\GameFlow
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   （`requirements.txt`：numpy、opencv-python、Pillow、psutil、pyautogui、pywin32、PyInstaller。若网络慢或失败，配置 pip 镜像：`pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`。）
3. 验证：`python -c "import cv2, numpy, PIL, psutil, pyautogui, win32api; print('deps OK')"`。

## 阶段 4：下载 9 个第三方运行时（核心工作，逐个来）

> 通用方法：对每个工具，从上游 GitHub 仓库的 **Releases 页**下载最新 Windows 版 zip（注意甄别：有的项目最新是源码包，要找标注 Windows/win64 的预编译包），解压到 `G:\GameFlow\resources\tools\<目录名>\`，并确认入口 exe 存在。

对每个工具执行（建议用 `gh release download` 或直接 `Invoke-WebRequest` 下载 zip 后用 `Expand-Archive` 解压）：

1. **maa**（明日方舟）：`gh release download -R MaaAssistantArknights/MaaAssistantArknights -p "*win64*.zip" -D <tmp>`，解压后把内容放入 `resources/tools/maa/`，确认 `MAA.exe` 存在。
2. **baas**（碧蓝档案）：**无公开 GitHub Release**。策略：检查源电脑/网盘是否有 `baas.exe` 打包版（v5.x）；若没有，告诉用户"baas 需从原电脑 `G:\GameFlow_portable_final_*\resources\tools\baas\` 拷贝整个目录"，先跳过它继续其他工具。
3. **alas**（碧蓝航线）：`LmeSzinc/AzurLaneAutoScript` Releases → Windows 包，解压到 `resources/tools/alas/`，确认 `Alas.exe`。
4. **naruto**（火影）：`duorua/narutomobile` Releases → 解压到 `resources/tools/naruto/`，确认 `MFAAvalonia.exe`。
5. **gumballs**（不思议迷宫）：`KhazixW2/MaaGumballs` Releases → 解压到 `resources/tools/gumballs/`，确认 `MFAAvalonia.exe`。
6. **endfield**（终末地）：`MaaEnd/MaaEnd` Releases → 解压到 `resources/tools/endfield/`，确认 `MaaEnd.exe`。
7. **zenless**（绝区零）：`OneDragon-Anything/ZenlessZoneZero-OneDragon` Releases → **必须完整解压整个包**（含 `.venv`、`.install`），放入 `resources/tools/zenless/`，确认 `.venv/Scripts/pythonw.exe` 存在（其启动方式特殊，不是单一 exe）。
8. **star_rail**（星穹铁道）：`moesnow/March7thAssistant` Releases → 解压到 `resources/tools/star_rail/`，确认 `March7th Launcher.exe`。
9. **qq_reader**（QQ阅读）：私有仓库：
   ```powershell
   git clone --recurse-submodules https://github.com/sdxdsadx/MaaQQReader.git G:\GameFlow\resources\tools\qq_reader
   ```
   确认 `MaaQQReaderGUI.exe` 或 `gui/maa_qq_reader_gui.py` 存在。若子模块失败，先 clone 再 `git -C ... submodule update --init --recursive`（拉取 MaaCommonAssets OCR 模型）。

> 每个工具完成后记录：目录、入口 exe 是否存在、版本号（如有）。**baas 缺失不算失败**，最后统一汇报。

## 阶段 5：预检 GameFlow 源码可运行

```powershell
cd G:\GameFlow
.\.venv\Scripts\python.exe -c "from gameflow.config import load_config; from gameflow.runners import RUNNERS; from gameflow.settings import validate_port; c=load_config('config/workflow.json'); print('workflows:', list(c['workflows'].keys())); print('runners:', len(RUNNERS))"
```
预期输出 9 个工作流 id（daily_game、blue_archive_daily、azur_lane_daily、naruto_daily、gumballs_daily、endfield_daily、zenless_daily、star_rail_daily、qq_reader_trial）。

## 阶段 6：启动 Web 控制面板并验证

1. 启动：`.\main.py web --no-browser`（后台运行，监听 `127.0.0.1:8765`；若端口被占，从 `config/workflow.json` 的 `server.port` 改端口）。
2. 验证接口：
   - `GET http://127.0.0.1:8765/api/status` → 返回 state + workflows
   - `GET http://127.0.0.1:8765/api/host` → 返回 13 个工具路径项（含 maa_gui、baas_gui、...、qq_reader_gui）和当前模拟器端口
   - `GET http://127.0.0.1:8765/` → 页面含「主机配置」标签
3. 汇报面板可访问地址。

## 阶段 7：配置本机路径（通过 GUI 或 CLI）

1. 告诉用户打开 `http://127.0.0.1:8765/#hostConfig`，在「主机配置」面板里填写：
   - **模拟器启动程序**：雷电 `ldconsole.exe`（`C:\Program Files\LDPlayer\LDPlayer9\ldconsole.exe` 之类）或 MuMu `MuMuManager.exe`
   - **各游戏自动化程序路径**：已下载的 `MAA.exe`/`baas.exe`/`Alas.exe`/`MFAAvalonia.exe`/`MaaEnd.exe`/`OneDragon-Launcher.exe`/`March7th Launcher.exe`/`MaaQQReaderGUI.exe`
   - **游戏路径**（终末地 `Endfield.exe` 等）
   - **模拟器端口**（1–65535，按各流程实际 ADB 端口填）
2. 也可用 CLI 等价命令：
   ```powershell
   .\.venv\Scripts\python.exe .\main.py configure-host --ldplayer "..." --mumu "..." --endfield "..."
   ```
3. 填写后点「保存」，确认提示"主机配置已保存"。

## 阶段 8：冒烟测试（可选但推荐）

1. 若有模拟器/游戏可测：点面板「开始每日作战」跑一个流程，观察日志。
2. 若无环境：至少跑 `self_test`：`.\main.py run self_test`，预期 success。
3. 汇报测试结果。

## 完成标准（全部满足才算部署完成）

- [ ] `G:\GameFlow` 源码 clone 成功，`git log` 正常
- [ ] `.venv` 依赖安装成功，import 验证通过
- [ ] 8/9 个运行时就位（baas 缺失需向用户说明获取方式）；每个入口 exe 存在
- [ ] qq_reader 私有仓库 + MaaCommonAssets 子模块拉取成功
- [ ] Web 面板 `127.0.0.1:8765` 可访问，`/api/host` 返回 13 项
- [ ] 主机配置已保存（模拟器/游戏/端口）
- [ ] 冒烟测试通过或明确说明未测原因

## 部署后说明（交付给用户）

1. 更新方式：以后在台式机 `G:\GameFlow` 下执行 `git pull`（GameFlow 源码/配置更新）；运行时如需更新，重新下载对应工具包替换。
2. baas 获取：从原电脑拷贝 `resources/tools/baas` 整个目录即可。
3. GameFlow 只做编排；模拟器、游戏本体、账号需在台式机上单独安装（雷电/MuMu/Endfield 等）。
4. 若流程日志报"XX工具不存在"，多半是运行时没放对目录，检查 `resources/tools/<name>/`。

开始执行吧。**先做阶段 1（GitHub 认证），完成后告诉我授权码，我授权后你继续。**

---

## === 提示词结束 ===

## 使用说明（给用户）

1. 把上面 `=== 主提示词 ===` 到 `=== 提示词结束 ===` 之间的全部内容，复制粘贴到台式机 Hermes 的对话框，发送。
2. Hermes 会先要你授权 GitHub（阶段 1），你在台式机浏览器打开 `https://github.com/login/device` 输入一次性代码即可（用你的 sdxdsadx 账号）。
3. 全程 Hermes 每完成一个大阶段会汇报；baas 无公开上游时会提示你从原电脑拷贝。
4. 若台式机没有 Hermes：也可把提示词交给任何 AI 编码助手（Claude/Codex 等），或手动按阶段执行。

## 依赖文件

- 本提示词内嵌了全部上游仓库信息；更详细的版本/备注见 `G:\project_I\docs\tools_upstream_index.md`（原电脑侧），可一并参考。
