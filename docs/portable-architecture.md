# 便携运行结构

```text
dist/
  ├─ GameFlow/                 # 可替换的只读发布目录
  │   ├─ GameFlow.exe
  │   ├─ config/workflow.json
  │   ├─ resources/runtime/adb.exe
  │   └─ resources/tools/<tool>/...
  ├─ GameFlow.state/           # 跨版本保留的可写状态
  │   ├─ data/runtime_settings.json
  │   ├─ data/host_settings.json
  │   └─ logs/
  └─ GameFlow.previous/        # 上一个可回退版本
```

源码运行时状态仍位于项目的 `data/` 与 `logs/`。正式 EXE 使用同级的 `dist/GameFlow.state`；`RuntimeSettings` 与 `HostSettings` 分别保存动态 ADB 端点和宿主机安装位置，这些位置只注入内存配置，不回写便携的 `config/workflow.json`。工作流中的 `${GAMEFLOW_ROOT}` 指向发布资源，`${GAMEFLOW_STATE}` 指向跨版本状态目录。

每个工作流只保存模拟器类型、实例编号和 `required_host_tools` 逻辑键。GameFlow 在流程启动前验证所需宿主机路径，再启动对应模拟器或 PC 游戏；验证失败时不会进入脚本阶段。日志连续五分钟无业务进展时，Runner 返回失败；全局失败策略会清理当前工具并完整重试一次。

桌面脚本清理由 GameFlow 启动的 PID 树或精确可执行文件路径负责，不按 `MFAAvalonia.exe`、`pythonw.exe` 等共享映像名全局结束。配置加载后还会验证 runner、action、必填字段和非互斥 ADB 端点冲突。

`/api/identity` 返回构建编号、运行根目录、可执行文件和 PID。管理员启动器仅复用根目录一致的服务；检测到旧 GameFlow 构建占用端口时先关闭该 PID，再启动当前产物。

正式构建只按 `config/resource_manifest.json` 的允许清单 staging 资源，排除测试、缓存、更新目录和历史日志。staging 先通过便携审计并运行 `GameFlow.exe status` 自检，再替换 `dist/GameFlow`；旧版内的 `data/logs` 首次迁移到 `GameFlow.state`，上一版本保留为 `dist/GameFlow.previous`。`build_info.json` 记录 Git 状态、Python 依赖列表和内置程序的 PE 版本/大小，便于离线定位发布内容。
