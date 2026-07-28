from __future__ import annotations

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .engine import WorkflowManager


class UiPreferences:
    """Persist web-panel task participation independently from workflow config."""

    def __init__(self, path: Path, workflow_ids: list[str]):
        self.path = path
        self.workflow_ids = [item for item in workflow_ids if item != "self_test"]
        self._lock = threading.Lock()
        self._value = self._load()

    def _defaults(self) -> dict[str, Any]:
        return {
            "version": 1,
            "order": list(self.workflow_ids),
            "max_parallel": 1,
            "workflows": {item: {"enabled": True} for item in self.workflow_ids},
        }

    def _normalise(self, value: Any) -> dict[str, Any]:
        defaults = self._defaults()
        if not isinstance(value, dict):
            return defaults
        saved_order = value.get("order", [])
        if not isinstance(saved_order, list):
            saved_order = []
        order = [item for item in saved_order
                 if item in self.workflow_ids and saved_order.count(item) == 1]
        order.extend(item for item in self.workflow_ids if item not in order)
        raw_workflows = value.get("workflows", {})
        if not isinstance(raw_workflows, dict):
            raw_workflows = {}
        workflows = {}
        for item in self.workflow_ids:
            raw_item = raw_workflows.get(item, {})
            enabled = raw_item.get("enabled", True) if isinstance(raw_item, dict) else True
            workflows[item] = {"enabled": bool(enabled)}
        try:
            max_parallel = max(1, min(int(value.get("max_parallel", 1)), 2))
        except (TypeError, ValueError):
            max_parallel = 1
        return {"version": 1, "order": order, "max_parallel": max_parallel,
                "workflows": workflows}

    def _load(self) -> dict[str, Any]:
        try:
            return self._normalise(json.loads(self.path.read_text(encoding="utf-8")))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return self._defaults()

    def get(self) -> dict[str, Any]:
        with self._lock:
            # Return a detached JSON-compatible value so request threads cannot mutate it.
            return json.loads(json.dumps(self._value, ensure_ascii=False))

    def update(self, value: Any) -> dict[str, Any]:
        with self._lock:
            self._value = self._normalise(value)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(self._value, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
            os.replace(temporary, self.path)
            return json.loads(json.dumps(self._value, ensure_ascii=False))


PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>GameFlow 次元作战终端</title>
<style>
:root{--bg:#08091b;--panel:rgba(18,20,52,.78);--panel2:rgba(10,12,34,.88);--line:rgba(142,151,255,.22);--text:#f2f4ff;--muted:#9ba3c7;--pink:#ff66b7;--cyan:#55dcff;--violet:#8d7cff;--green:#61f2b1;--red:#ff6680;--yellow:#ffd76a;--shadow:0 18px 55px rgba(0,0,0,.38);--radius:20px}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{min-height:100vh;margin:0;color:var(--text);font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;background:radial-gradient(circle at 8% 8%,rgba(92,71,220,.3),transparent 28%),radial-gradient(circle at 90% 20%,rgba(255,72,170,.18),transparent 26%),linear-gradient(145deg,#070817 0%,#10143a 52%,#070817 100%);overflow-x:hidden}
body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.55;background-image:radial-gradient(circle,#fff 0 1px,transparent 1.4px),radial-gradient(circle,#7bdcff 0 1px,transparent 1.5px);background-size:63px 63px,97px 97px;background-position:0 0,30px 18px;animation:stars 35s linear infinite}
body:after{content:"";position:fixed;inset:0;pointer-events:none;background:repeating-linear-gradient(0deg,transparent 0 3px,rgba(129,150,255,.018) 4px);mix-blend-mode:screen}
body.day{--bg:#f1f4ff;--panel:rgba(255,255,255,.82);--panel2:rgba(247,249,255,.92);--line:rgba(91,103,190,.2);--text:#292b4c;--muted:#6f769d;--shadow:0 18px 55px rgba(79,75,145,.18);background:radial-gradient(circle at 8% 8%,rgba(123,113,255,.2),transparent 30%),radial-gradient(circle at 90% 20%,rgba(255,118,186,.17),transparent 28%),linear-gradient(145deg,#f5f6ff,#e7ecff)}
@keyframes stars{to{background-position:126px 63px,224px 115px}}@keyframes pulse{50%{box-shadow:0 0 0 9px rgba(86,221,255,0)}}@keyframes float{50%{transform:translateY(-7px) rotate(1deg)}}@keyframes sweep{to{transform:translateX(220%)}}@keyframes enter{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.shell{width:min(1260px,calc(100% - 32px));margin:24px auto 50px;position:relative}.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}.brand{display:flex;align-items:center;gap:14px}.brand-mark{width:48px;height:48px;display:grid;place-items:center;border-radius:15px;background:linear-gradient(135deg,var(--violet),var(--pink));font-size:25px;box-shadow:0 0 30px rgba(152,101,255,.5);transform:rotate(-4deg)}.brand h1{font-size:24px;letter-spacing:1px;margin:0}.brand small{display:block;color:var(--muted);font-size:11px;letter-spacing:3px;margin-top:3px}.top-actions{display:flex;align-items:center;gap:10px}.clock{font:600 13px Consolas,monospace;color:var(--cyan);padding:9px 13px;border:1px solid var(--line);border-radius:99px;background:var(--panel)}
button,select{font:inherit}button{position:relative;border:0;color:#fff;border-radius:12px;padding:10px 15px;cursor:pointer;background:linear-gradient(135deg,#7568ec,#a35cff);box-shadow:0 7px 20px rgba(107,80,222,.25);transition:.2s transform,.2s filter,.2s opacity}button:hover:not(:disabled){transform:translateY(-2px);filter:brightness(1.12)}button:active:not(:disabled){transform:translateY(0)}button:disabled{opacity:.38;cursor:not-allowed;box-shadow:none}.icon-btn{width:40px;height:40px;padding:0;border:1px solid var(--line);background:var(--panel);box-shadow:none;color:var(--text)}.secondary{background:rgba(119,126,190,.16);border:1px solid var(--line);box-shadow:none;color:var(--text)}.danger{background:linear-gradient(135deg,#d94174,#ff616f)}.primary{padding:12px 21px;background:linear-gradient(135deg,#6758eb,#d955ca);overflow:hidden}.primary:after{content:"";position:absolute;inset:-20px;width:35%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.5),transparent);transform:translateX(-220%) skewX(-18deg);animation:sweep 3.5s infinite}.quick-start{min-width:168px;white-space:nowrap}
.hero{position:relative;display:grid;grid-template-columns:1fr 280px;min-height:205px;overflow:hidden;border:1px solid var(--line);border-radius:26px;padding:28px 30px;background:linear-gradient(118deg,rgba(91,74,213,.25),var(--panel) 45%,rgba(255,79,174,.16));box-shadow:var(--shadow);isolation:isolate}.hero:before{content:"GAMEFLOW // 07";position:absolute;right:15px;top:12px;color:rgba(255,255,255,.045);font:bold 58px/1 Consolas;letter-spacing:-5px;z-index:-1}.eyebrow{color:var(--cyan);letter-spacing:3px;font:bold 11px Consolas;margin-bottom:10px}.hero h2{font-size:30px;margin:0 0 9px}.hero p{color:var(--muted);margin:0;max-width:620px;line-height:1.7}.hero-status{display:flex;align-items:center;gap:10px;margin-top:24px}.status-light{width:11px;height:11px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 rgba(97,242,177,.45);animation:pulse 1.8s infinite}.status-light.busy{background:var(--pink)}#headline{font-weight:700}.hero-message{color:var(--muted);font-size:13px;margin-top:5px;min-height:20px}
.companion-wrap{position:relative;display:flex;align-items:center;justify-content:center}.speech{position:absolute;right:178px;top:15px;width:125px;padding:9px 11px;background:rgba(255,255,255,.95);color:#4c4269;border-radius:13px 13px 2px 13px;font-size:12px;box-shadow:0 8px 22px rgba(0,0,0,.16);z-index:3}.gacha-card{width:158px;height:174px;padding:0;border:1px solid rgba(255,215,106,.55);border-radius:25px;background:radial-gradient(circle at 50% 38%,rgba(255,230,151,.35),transparent 38%),linear-gradient(145deg,rgba(92,73,195,.65),rgba(39,31,103,.82));box-shadow:0 0 35px rgba(255,198,75,.22),inset 0 0 24px rgba(255,255,255,.08);overflow:hidden;animation:float 4s ease-in-out infinite}.gacha-card:hover{transform:translateY(-5px) scale(1.025);filter:brightness(1.08)}.gacha-card:before{content:"";position:absolute;inset:7px;border:1px solid rgba(255,235,174,.3);border-radius:19px;pointer-events:none}.gacha-avatar{position:absolute;width:146px;height:146px;left:6px;top:3px;object-fit:contain;filter:drop-shadow(0 8px 12px rgba(0,0,0,.35));transition:.35s opacity,.35s transform}.gacha-avatar.anime-avatar{width:142px;height:146px;left:8px;object-fit:cover;object-position:center 18%;border-radius:19px 19px 10px 10px}.gacha-card:hover .gacha-avatar{transform:scale(1.04)}.gacha-info{position:absolute;left:8px;right:8px;bottom:8px;padding:20px 7px 7px;border-radius:0 0 15px 15px;background:linear-gradient(transparent,rgba(11,9,40,.95) 42%);text-align:center}.gacha-stars{color:#ffdf73;font-size:10px;letter-spacing:1px;text-shadow:0 0 9px #ffb84f}.gacha-name{display:block;color:#fff;font-weight:700;font-size:14px;margin-top:1px}.gacha-hint,.gacha-pool{position:absolute;top:8px;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:3px 6px;border-radius:99px;background:rgba(8,7,31,.72);color:#ffe89a;font:9px "Microsoft YaHei",sans-serif;letter-spacing:.5px;z-index:2}.gacha-hint{right:8px}.gacha-pool{left:8px;color:#80eaff;font-family:Consolas,monospace}.summon-flash{animation:summon .55s ease}@keyframes summon{0%{opacity:.2;transform:scale(.72) rotate(-5deg)}55%{filter:brightness(1.8) drop-shadow(0 0 24px #fff)}100%{opacity:1;transform:none}}
.layout{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(330px,.75fr);gap:18px;margin-top:18px}.stack{display:flex;flex-direction:column;gap:18px}.card{position:relative;border:1px solid var(--line);border-radius:var(--radius);padding:20px;background:var(--panel);box-shadow:var(--shadow);backdrop-filter:blur(18px);animation:enter .45s ease both}.card-title{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:15px}.card-title h3{font-size:17px;margin:0}.section-code{font:10px Consolas;color:var(--muted);letter-spacing:2px}.muted{color:var(--muted)}.small{font-size:12px}
.progress-shell{margin-top:17px}.progress-meta{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-bottom:7px}.progress-track{height:8px;border-radius:99px;background:rgba(100,108,177,.18);overflow:hidden}.progress-fill{height:100%;width:0;border-radius:inherit;background:linear-gradient(90deg,var(--cyan),var(--violet),var(--pink));box-shadow:0 0 18px var(--violet);transition:width .5s ease}
#tasks{display:flex;flex-direction:column;gap:9px}.task{--accent:#8d7cff;display:grid;grid-template-columns:20px 28px 48px minmax(0,1fr) auto auto;align-items:center;gap:10px;padding:12px;border:1px solid rgba(141,124,255,.13);border-radius:16px;background:rgba(8,10,31,.35);transition:.2s border-color,.2s transform,.2s background,.2s opacity;cursor:grab}.day .task{background:rgba(255,255,255,.48)}.task:hover{transform:translateX(3px);border-color:color-mix(in srgb,var(--accent) 55%,transparent);background:color-mix(in srgb,var(--accent) 8%,transparent)}.task.is-running{border-color:var(--accent);box-shadow:inset 3px 0 var(--accent),0 0 24px color-mix(in srgb,var(--accent) 18%,transparent)}.task.is-disabled{opacity:.62}.task.is-disabled .task-icon{filter:grayscale(.8)}.task.dragging{opacity:.32;transform:scale(.985)}.task.drop-before{box-shadow:0 -3px 0 var(--cyan),0 -8px 22px rgba(85,220,255,.22)}.task.drop-after{box-shadow:0 3px 0 var(--pink),0 8px 22px rgba(255,102,183,.2)}.drag-handle{color:rgba(170,180,235,.58);font:bold 17px/1 monospace;letter-spacing:-4px;cursor:grab;user-select:none;text-align:center}.drag-handle:active,.task:active{cursor:grabbing}.select-box{appearance:none;width:20px;height:20px;border-radius:7px;border:2px solid rgba(160,169,229,.45);display:grid;place-items:center;cursor:pointer}.select-box:checked{background:linear-gradient(135deg,var(--violet),var(--pink));border-color:transparent}.select-box:checked:after{content:"✓";font:bold 13px sans-serif;color:white}.task-icon{width:45px;height:45px;display:grid;place-items:center;border-radius:14px;color:#fff;font-size:20px;background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 75%,#fff),var(--accent));box-shadow:0 7px 20px color-mix(in srgb,var(--accent) 24%,transparent)}.task-name{font-weight:700}.task-sub{display:block;color:var(--muted);font-size:12px;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.pill{display:inline-flex;align-items:center;gap:5px;margin-left:7px;padding:3px 8px;border-radius:99px;background:rgba(140,148,201,.14);color:var(--muted);font-size:10px}.pill:before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor}.pill.running{color:var(--cyan);background:rgba(85,220,255,.12)}.pill.success{color:var(--green)}.pill.failed,.pill.interrupted{color:var(--red)}.status-retry{border:0;box-shadow:none;cursor:pointer}.status-retry:hover:not(:disabled){transform:none;filter:brightness(1.25)}.participation{margin-left:6px;padding:3px 8px;border:1px solid currentColor;border-radius:99px;background:transparent;box-shadow:none;font-size:10px}.participation.enabled{color:var(--green)}.participation.skipped{color:var(--yellow)}.participation:hover:not(:disabled){transform:none;background:rgba(255,255,255,.08)}.task-actions,.single{display:flex;gap:4px}.task-actions button{width:30px;height:30px;padding:0;border-radius:9px}.single button{white-space:nowrap;font-size:12px;padding:8px 10px}.single .cancel-one{background:linear-gradient(135deg,#c93e67,#ff616f)}
.toolbar{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-top:16px;padding-top:15px;border-top:1px solid var(--line)}.parallel-control{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px;margin-right:auto}select{color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:9px 28px 9px 10px;outline:none}
.batch-grid{display:grid;grid-template-columns:1fr;gap:9px}.batch-item{position:relative;min-height:82px;padding:13px 14px 12px 45px;border-radius:15px;background:var(--panel2);border:1px solid rgba(142,151,255,.12)}.batch-item:before{position:absolute;left:14px;top:15px;font-size:18px}.batch-item.active:before{content:"✦";color:var(--pink)}.batch-item.queue:before{content:"⌛";color:var(--yellow)}.batch-item.done:before{content:"✓";color:var(--green)}.batch-item b{font-size:12px}.batch-item p{white-space:pre-line;line-height:1.55;margin:6px 0 0}
.log-card{grid-column:1/-1}.log-head,.log-tools{display:flex;align-items:center;justify-content:space-between;gap:12px}.log-tools{justify-content:flex-end}.switch{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}.log-view{height:350px;overflow:auto;padding:15px 17px;white-space:pre-wrap;word-break:break-all;border:1px solid rgba(85,220,255,.15);border-radius:15px;background:#050711;color:#7df9bf;font:12px/1.65 Consolas,"Microsoft YaHei",monospace;box-shadow:inset 0 0 38px rgba(0,0,0,.5);scrollbar-color:#6655bc transparent}.log-view:before{content:"●  LIVE LINK";display:block;color:#ff6cae;font-size:10px;letter-spacing:2px;margin-bottom:8px}.log-empty{color:var(--muted)}
.history{display:flex;flex-direction:column;gap:8px;max-height:340px;overflow:auto}.run{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 12px;border:1px solid rgba(142,151,255,.11);border-radius:13px;background:var(--panel2)}.run-main{min-width:0}.run-main b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px}.run-state{padding:4px 8px;border-radius:99px;font-size:10px;background:rgba(140,148,201,.12)}.success{color:var(--green)}.failed,.interrupted{color:var(--red)}.skipped{color:var(--yellow)}.empty{color:var(--muted);padding:20px;text-align:center;border:1px dashed var(--line);border-radius:13px}.toast{position:fixed;right:24px;bottom:24px;z-index:20;max-width:min(390px,calc(100% - 48px));padding:14px 18px;border:1px solid rgba(85,220,255,.35);border-radius:14px;background:rgba(15,18,48,.95);color:#fff;box-shadow:0 15px 45px rgba(0,0,0,.4);transform:translateY(90px);opacity:0;transition:.3s}.toast.show{transform:none;opacity:1}.toast.bad{border-color:rgba(255,102,128,.55)}
.pill.needs_update,.run-state.needs_update{color:var(--yellow)}.pill.cancelled,.run-state.cancelled{color:var(--red)}
.readiness-bar{position:relative;display:flex;align-items:center;gap:7px;max-width:520px;height:22px;margin-top:7px;padding:0 9px 0 24px;overflow:hidden;border:1px solid rgba(140,148,201,.16);border-radius:8px;background:rgba(91,98,159,.11);color:var(--muted);font-size:10px}.readiness-bar:before{content:"";position:absolute;left:9px;width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 10px currentColor}.readiness-bar:after{content:"";position:absolute;left:0;bottom:0;width:100%;height:2px;background:currentColor;opacity:.55}.readiness-bar .readiness-label{font-weight:700;letter-spacing:.4px}.readiness-bar .readiness-detail{margin-left:auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.78}.readiness-bar.success{color:var(--green);background:rgba(97,242,177,.08)}.readiness-bar.running{color:var(--cyan);background:rgba(85,220,255,.08)}.readiness-bar.queued,.readiness-bar.skipped,.readiness-bar.needs_update{color:var(--yellow);background:rgba(255,204,102,.08)}.readiness-bar.failed,.readiness-bar.interrupted,.readiness-bar.cancelled{color:var(--red);background:rgba(255,102,128,.08)}
@media(max-width:900px){.hero{grid-template-columns:1fr 210px}.speech{display:none}.layout{grid-template-columns:1fr}.log-card{grid-column:auto}}@media(max-width:650px){.shell{width:min(100% - 18px,1260px);margin-top:12px}.clock{display:none}.brand-mark{width:40px;height:40px}.brand small{display:none}.top-actions{gap:6px}.quick-start{min-width:0;padding:10px 12px;font-size:12px}.hero{display:block;padding:22px}.hero h2{font-size:24px}.companion-wrap{display:none}.task{grid-template-columns:18px 25px 42px 1fr}.task-actions,.single{grid-column:4}.task-actions{justify-content:flex-start}.card{padding:15px}.toolbar{align-items:stretch}.parallel-control{width:100%}.danger{flex:1}.log-head{align-items:flex-start;flex-direction:column}.log-tools{width:100%;justify-content:space-between}.log-view{height:280px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important}}
</style></head><body><div class="shell">
<header class="topbar"><div class="brand"><div class="brand-mark">✦</div><div><h1>GameFlow</h1><small>DIMENSIONAL OPERATIONS</small></div></div><div class="top-actions"><div id="clock" class="clock">SYNC --:--:--</div><button id="startDaily" class="primary quick-start" onclick="runDaily()">✦ 开始每日作战</button><button class="icon-btn" onclick="toggleTheme()" title="切换昼夜主题">☾</button></div></header>
<section class="hero"><div><div class="eyebrow">COMMAND CENTER / ONLINE</div><h2>请输入文本</h2><p>请输入文本</p><div class="hero-status"><span id="statusLight" class="status-light"></span><span id="headline">正在连接指挥系统……</span></div><div id="message" class="hero-message">正在读取任务状态</div><div class="progress-shell"><div class="progress-meta"><span>今日进度</span><span id="progressText">0 / 0</span></div><div class="progress-track"><div id="progressFill" class="progress-fill"></div></div></div></div>
<div class="companion-wrap"><div id="operatorSpeech" class="speech">正在连接祈愿池……</div><button class="gacha-card" onclick="drawCharacter()" title="点击重新召唤女性角色"><span id="operatorPoolSize" class="gacha-pool">POOL 50</span><span id="operatorSeries" class="gacha-hint">RANDOM PICK</span><img id="operatorAvatar" class="gacha-avatar" alt="随机女性游戏或番剧角色头像"><span class="gacha-info"><span id="gachaStars" class="gacha-stars">★★★★★</span><span id="operatorName" class="gacha-name">召唤中</span></span></button></div></section>
<main class="layout"><div class="stack"><section class="card"><div class="card-title"><div><span class="section-code">SQUAD FORMATION</span><h3>每日任务编队</h3></div><span class="muted small">按住任务卡片直接拖拽 · ↑↓ 也可微调</span></div><div id="tasks"></div><div class="toolbar"><label class="parallel-control">同步出击上限 <select id="parallel"><option value="1">1 队 · 顺序执行</option><option value="2">2 队 · 并行执行</option></select></label><button class="danger" onclick="stopAll()">终止行动</button></div></section></div>
<aside class="stack"><section class="card"><div class="card-title"><div><span class="section-code">MISSION TELEMETRY</span><h3>实时战况</h3></div></div><div class="batch-grid"><div class="batch-item active"><b>正在行动</b><p id="active" class="small muted">无</p></div><div class="batch-item queue"><b>待命队列</b><p id="queued" class="small muted">无</p></div><div class="batch-item done"><b>行动报告</b><p id="completed" class="small muted">无</p></div></div></section><section class="card"><div class="card-title"><div><span class="section-code">ARCHIVE</span><h3>最近行动记录</h3></div></div><div id="history" class="history"></div></section></aside>
<section class="card log-card"><div class="card-title log-head"><div><span class="section-code">NEURAL LINK / STREAM</span><h3>实时运行日志</h3></div><div class="log-tools"><label class="switch"><input id="autoScroll" type="checkbox" checked>跟随最新信号</label><button class="secondary" onclick="clearLogView()">清空终端</button></div></div><div id="logView" class="log-view"><span class="log-empty">等待任务日志……</span></div></section></main></div><div id="toast" class="toast"></div>
<script>
const gameMeta={daily_game:{icon:'♜',color:'#7e8cff'},blue_archive_daily:{icon:'✦',color:'#55cfff'},azur_lane_daily:{icon:'⚓',color:'#4f91ff'},naruto_daily:{icon:'忍',color:'#ff765f'},gumballs_daily:{icon:'◈',color:'#bd77ff'},endfield_daily:{icon:'⬡',color:'#65e2b6'},zenless_daily:{icon:'Z',color:'#f3d85c'},star_rail_daily:{icon:'轨',color:'#8e7dff'}};
const baseCharacterPool=[
 {name:'胡桃',file:'Hutao',series:'原神',quote:'不如由我来送你一程？今天也要全勤！'},
 {name:'神里绫华',file:'Ayaka',series:'原神',quote:'若知是梦，何须醒来。今日行动请交给我。'},
 {name:'纳西妲',file:'Nahida',series:'原神',quote:'知识与你分享，任务也一起完成吧。'},
 {name:'芙宁娜',file:'Furina',series:'原神',quote:'聚光灯已经就位，精彩的行动要开始了！'},
 {name:'妮露',file:'Nilou',series:'原神',quote:'让今天的任务像舞步一样顺利吧。'},
 {name:'夜兰',file:'Yelan',series:'原神',quote:'情报已确认，接下来只需等待结果。'},
 {name:'刻晴',file:'Keqing',series:'原神',quote:'时间宝贵，现在就开始今日行动。'},
 {name:'莫娜',file:'Mona',series:'原神',quote:'命运的轨迹显示，今天会顺利完成。'},
 {name:'雷电将军',file:'Shougun',series:'原神',quote:'既是今日之事，便不应留待明日。'},
 {name:'八重神子',file:'Yae',series:'原神',quote:'呵，这点日常也值得烦恼？交给自动化便是。'},
 {name:'甘雨',file:'Ganyu',series:'原神',quote:'今日的工作清单已经整理好了，请逐项确认。'},
 {name:'可莉',file:'Klee',series:'原神',quote:'可莉今天不去炸鱼，先把每日任务做完！'},
 {name:'芭芭拉',file:'Barbara',series:'原神',quote:'芭芭拉会为你加油的，今天也要打起精神哦！'},
 {name:'琴',file:'Qin',series:'原神',quote:'今日的事务不能积压，我们按计划完成。'},
 {name:'丽莎',file:'Lisa',series:'原神',quote:'小可爱，偷懒之前，先把今天的事情处理完吧。'},
 {name:'安柏',file:'Ambor',series:'原神',quote:'侦察骑士准备完毕，一起出发吧！'},
 {name:'菲谢尔',file:'Fischl',series:'原神',quote:'断罪之皇女已降下谕旨——今日委托，一项不留！'},
 {name:'砂糖',file:'Sucrose',series:'原神',quote:'参数记录完成……接下来观察自动流程的结果。'},
 {name:'诺艾尔',file:'Noel',series:'原神',quote:'交给诺艾尔吧，我会把每一项都妥善完成。'},
 {name:'迪奥娜',file:'Diona',series:'原神',quote:'我、我才不是特意来帮忙的！快点把日常做完啦！'},
 {name:'罗莎莉亚',file:'Rosaria',series:'原神',quote:'只要按时完成工作就行，没必要多浪费时间。'},
 {name:'优菈',file:'Eula',series:'原神',quote:'把任务拖到明天？这个仇，我可记下了。'},
 {name:'珊瑚宫心海',file:'Kokomi',series:'原神',quote:'能源和时间都要合理分配，按既定方案执行吧。'},
 {name:'宵宫',file:'Yoimiya',series:'原神',quote:'日常也要像烟花一样，干脆又漂亮地收尾！'},
 {name:'早柚',file:'Sayu',series:'原神',quote:'任务自动完成的话……我就能多睡一会儿了。'},
 {name:'柯莱',file:'Collei',series:'原神',quote:'我会认真帮忙的……今天也一起加油吧。'},
 {name:'迪希雅',file:'Dehya',series:'原神',quote:'放心交给我，拿了委托就一定办到底。'},
 {name:'坎蒂丝',file:'Candace',series:'原神',quote:'一切都已安排妥当，安心完成今天的行程吧。'},
 {name:'琳妮特',file:'Linette',series:'原神',quote:'指令已收到。接下来进入自动执行。'},
 {name:'娜维娅',file:'Navia',series:'原神',quote:'打起精神来！刺玫会的行动可不能拖拖拉拉。'},
 {name:'克洛琳德',file:'Clorinde',series:'原神',quote:'目标明确。无需多言，开始执行。'},
 {name:'闲云',file:'Liuyun',series:'原神',quote:'此等机关之术，正宜代劳繁琐日课。'},
 {name:'玛薇卡',file:'Mavuika',series:'原神',quote:'既然决定出发，就全力赢下今天！'},
 {name:'雷姆',series:'Re:Zero',url:'https://s4.anilist.co/file/anilistcdn/character/large/b88575-Ayu8UPDA8NS6.png',quote:'雷姆会一直陪在你的身边。'},
 {name:'艾米莉亚',series:'Re:Zero',url:'https://s4.anilist.co/file/anilistcdn/character/large/b88572-IzTwXEHSobRs.jpg',quote:'今天也一起努力吧。'},
 {name:'亚丝娜',series:'刀剑神域',url:'https://s4.anilist.co/file/anilistcdn/character/large/b36828-j5ib0adAzGMx.png',quote:'只要行动起来，就一定能抵达终点。'},
 {name:'芙莉莲',series:'葬送的芙莉莲',url:'https://s4.anilist.co/file/anilistcdn/character/large/b176754-PCnpqIOkjhFk.png',quote:'十分钟而已，对精灵来说很短。'},
 {name:'菲伦',series:'葬送的芙莉莲',url:'https://s4.anilist.co/file/anilistcdn/character/large/b183965-uGFohBjlFoTp.png',quote:'请不要熬夜，任务交给自动化就好。'},
 {name:'喜多川海梦',series:'更衣人偶坠入爱河',url:'https://s4.anilist.co/file/anilistcdn/character/large/b133676-kV2czE3C8Qls.png',quote:'喜欢的东西就要全力以赴！'},
 {name:'后藤一里',series:'孤独摇滚',url:'https://s4.anilist.co/file/anilistcdn/character/large/b257562-Ru35NYPfsqhY.png',quote:'自、自动运行的话，我应该可以……'},
 {name:'锦木千束',series:'莉可丽丝',url:'https://s4.anilist.co/file/anilistcdn/character/large/b260329-ejhEdFfXhs53.jpg',quote:'今天也要开心地完成任务！'},
 {name:'牧濑红莉栖',series:'命运石之门',url:'https://s4.anilist.co/file/anilistcdn/character/large/b34470-Jw2LXZBL5R8i.png',quote:'这才不是什么魔法，是自动化。'},
 {name:'樱岛麻衣',series:'青春猪头少年',url:'https://s4.anilist.co/file/anilistcdn/character/large/b127222-Jh5hhP7vZ7s1.png',quote:'别发呆了，今天的任务还没结束。'},
 {name:'薇尔莉特',series:'紫罗兰永恒花园',url:'https://s4.anilist.co/file/anilistcdn/character/large/b90169-4wr1Zehnsac8.png',quote:'已理解指令，开始执行每日任务。'},
 {name:'惠惠',series:'为美好的世界献上祝福',url:'https://s4.anilist.co/file/anilistcdn/character/large/b89361-tq8PQQ4MmF0M.png',quote:'将每日任务一口气全部爆裂吧！'},
 {name:'赫萝',series:'狼与香辛料',url:'https://s4.anilist.co/file/anilistcdn/character/large/b7373-1BH0gELuZmHD.jpg',quote:'交给贤狼，当然不会有问题。'},
 {name:'蝴蝶忍',series:'鬼灭之刃',url:'https://s4.anilist.co/file/anilistcdn/character/large/b136070-MC9LLxJsHyHE.png',quote:'请安心等待任务完成。'},
 {name:'灶门祢豆子',series:'鬼灭之刃',url:'https://s4.anilist.co/file/anilistcdn/character/large/b127518-NRlq1CQ1v1ro.png',quote:'唔！今天也会顺利完成。'},
 {name:'星野爱',series:'我推的孩子',url:'https://s4.anilist.co/file/anilistcdn/character/large/b172759-cccVhJ2fQA92.png',quote:'今天也要闪闪发光地全勤！'},
 {name:'有马加奈',series:'我推的孩子',url:'https://s4.anilist.co/file/anilistcdn/character/large/b188783-77orwP7vNuNg.png',quote:'别小看我，日常任务当然能完成。'}
];
let characterPool=[...baseCharacterPool];
const statusNames={success:'正常结束',failed:'异常结束',needs_update:'需要更新',skipped:'已跳过',interrupted:'已中断',cancelled:'已取消',running:'作战中',queued:'排队中',pending:'未完成',idle:'就绪'};
let workflows=[],order=[],currentStates={},uiPreferences=null,clearAnchor='',latestLines=[],lastLogText='',toastTimer,currentCharacter=null,lastCharacter=-1,draggingId=null,isDragging=false,currentBusy=false,preferencesSave=Promise.resolve(),ignoredBatchStatuses=new Set(),cancellableTasks=new Set(),cancellingTasks=new Set();
const $=id=>document.getElementById(id);
async function api(url,opt){let r=await fetch(url,opt),d=await r.json();if(!r.ok)throw new Error(d.message||d.error||'请求失败');return d}
function preferencePayload(){let workflowPrefs={};workflows.forEach(w=>workflowPrefs[w.id]={enabled:uiPreferences?.workflows?.[w.id]?.enabled!==false});return{version:1,order:[...order],max_parallel:Number($('parallel').value||1),workflows:workflowPrefs}}
function queuePreferenceSave(showError=true){if(!uiPreferences)return Promise.resolve();let payload=preferencePayload();uiPreferences=payload;preferencesSave=preferencesSave.catch(()=>{}).then(()=>api('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})).then(d=>{uiPreferences=d.preferences;return d}).catch(e=>{if(showError)notify('保存任务开关失败：'+e.message,true);throw e});return preferencesSave}
function label(id){let w=workflows.find(x=>x.id===id);return w?w.name:id}
function prettyStatus(value){return statusNames[value]||value||'就绪'}
function readinessText(value){return({success:'今日已完成',running:'正在执行',queued:'等待执行',failed:'今日执行异常',needs_update:'脚本需要更新',skipped:'今日已跳过',cancelled:'今日已取消',interrupted:'今日已中断',pending:'今日未完成'})[value]||'今日未完成'}
function readinessTime(value){if(!value)return'';let date=new Date(value);return Number.isNaN(date.getTime())?'':date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false})}
function escapeAttr(value){return String(value||'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
function notify(text,bad=false){let el=$('toast');el.textContent=text;el.className='toast show'+(bad?' bad':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.className='toast',4200)}
function drawCharacter(){let index;if(characterPool.length>1){do{index=Math.floor(Math.random()*characterPool.length)}while(index===lastCharacter)}else index=0;lastCharacter=index;currentCharacter=characterPool[index];let img=$('operatorAvatar');img.classList.toggle('anime-avatar',!!currentCharacter.url);img.classList.remove('summon-flash');void img.offsetWidth;img.classList.add('summon-flash');img.onerror=()=>{img.onerror=null;img.classList.remove('anime-avatar');img.src='https://enka.network/ui/UI_AvatarIcon_Ayaka.png'};img.src=currentCharacter.url||('https://enka.network/ui/UI_AvatarIcon_'+currentCharacter.file+'.png');img.alt=currentCharacter.name+'女性角色头像';$('operatorName').textContent=currentCharacter.name;$('operatorSeries').textContent=currentCharacter.series;$('operatorPoolSize').textContent='POOL '+characterPool.length;$('gachaStars').textContent='★★★★★';$('operatorSpeech').textContent=currentCharacter.quote}
function workflowEnabled(id){return uiPreferences?.workflows?.[id]?.enabled!==false}
function setWorkflowEnabled(id,enabled){if(!uiPreferences?.workflows?.[id])return;uiPreferences.workflows[id].enabled=!!enabled;renderTasks(currentStates);queuePreferenceSave();notify(label(id)+(enabled?' 已加入每日流程':' 已设为 SKIPPED'))}
function toggleWorkflow(id){setWorkflowEnabled(id,!workflowEnabled(id))}
function clearDropMarks(){document.querySelectorAll('.task').forEach(x=>x.classList.remove('drop-before','drop-after'))}
function reorderTask(source,targetId,before){if(!source||source===targetId)return false;let next=order.filter(x=>x!==source),target=next.indexOf(targetId);if(target<0)return false;next.splice(target+(before?0:1),0,source);order=next;draggingId=null;isDragging=false;clearDropMarks();renderTasks(currentStates);queuePreferenceSave();notify('编队顺序已更新');return true}
function bindTaskDrag(row,id){let active=false,targetId=null,before=true;function movePointer(event){if(!active)return;let target=document.elementFromPoint(event.clientX,event.clientY)?.closest('.task');clearDropMarks();targetId=null;if(!target||target.dataset.id===id)return;let rect=target.getBoundingClientRect();before=event.clientY<rect.top+rect.height/2;targetId=target.dataset.id;target.classList.add(before?'drop-before':'drop-after')}function finish(commit){if(!active)return;document.removeEventListener('mousemove',movePointer);document.removeEventListener('mouseup',dropPointer);if(commit&&targetId)reorderTask(id,targetId,before);active=false;draggingId=null;isDragging=false;targetId=null;clearDropMarks();row.classList.remove('dragging')}function dropPointer(){finish(true)}row.addEventListener('mousedown',event=>{if(event.button!==0||event.target.closest('button,input,select,a'))return;active=true;draggingId=id;isDragging=true;row.classList.add('dragging');document.addEventListener('mousemove',movePointer);document.addEventListener('mouseup',dropPointer,{once:true});event.preventDefault()})}
function renderTasks(states){
 $('tasks').innerHTML='';
 order.forEach((id,i)=>{
  let w=workflows.find(x=>x.id===id);if(!w)return;
  let state=states[id]||{},enabled=workflowEnabled(id),cancellable=cancellableTasks.has(id),cancelling=cancellingTasks.has(id),meta=gameMeta[id]||{icon:'◇',color:'#8d7cff'};
  let raw=state.running?'running':(state.batch_status||state.last_status||'idle');
  let todayRaw=state.today_status||'pending';
  if(!enabled&&todayRaw!=='success'&&todayRaw!=='running')todayRaw='skipped';
  let readyAt=readinessTime(state.today_finished_at||state.today_started_at),readyDetail=readyAt?('更新于 '+readyAt):(todayRaw==='pending'?'等待今日首次执行':'状态实时同步');
  let statusTag=raw==='skipped'?`<button class="pill skipped status-retry" title="该任务本轮被跳过；点击强制执行" ${currentBusy?'disabled':''} onclick="runOne('${id}',true)">${prettyStatus(raw)} · 重跑</button>`:`<span class="pill ${raw}">${prettyStatus(raw)}</span>`;
  if(state.tomorrow_update)statusTag+=`<span class="pill needs_update" title="下个游戏日启动脚本后将先等待 10 分钟自动更新">明日更新</span>`;
  let row=document.createElement('div');row.className='task'+(state.running?' is-running':'')+(enabled?'':' is-disabled');row.dataset.id=id;row.style.setProperty('--accent',meta.color);
  row.innerHTML=`<span class="drag-handle" title="按住拖拽调整顺序">⋮⋮</span><input class="select-box" type="checkbox" data-id="${id}" ${enabled?'checked':''} onchange="setWorkflowEnabled('${id}',this.checked)" aria-label="${enabled?'停用':'启用'}${w.name}"><div class="task-icon">${meta.icon}</div><div><span class="task-name">${w.name}</span>${statusTag}<button class="participation ${enabled?'enabled':'skipped'}" onclick="toggleWorkflow('${id}')" title="点击切换该任务是否参加每日流程">${enabled?'每日启动':'SKIPPED'}</button><span class="task-sub">${state.step?'当前步骤：'+state.step:(state.batch_message||state.message||'等待指挥官下令')}</span><span class="readiness-bar ${todayRaw}" title="${escapeAttr(state.today_message||readinessText(todayRaw))}"><span class="readiness-label">${readinessText(todayRaw)}</span><span class="readiness-detail">${readyDetail}</span></span></div><div class="task-actions"><button class="secondary" title="向前调整" ${i===0?'disabled':''} onclick="move('${id}',-1)">↑</button><button class="secondary" title="向后调整" ${i===order.length-1?'disabled':''} onclick="move('${id}',1)">↓</button></div><div class="single"><button class="secondary" ${currentBusy?'disabled':''} onclick="runOne('${id}')">单队出击</button><button class="cancel-one" title="只取消这个任务，不影响其他任务" ${cancellable&&!cancelling?'':'disabled'} onclick="cancelOne('${id}')">${cancelling?'取消中':'取消'}</button></div>`;
  bindTaskDrag(row,id);$('tasks').appendChild(row)
 })
}
function move(id,delta){let i=order.indexOf(id),j=i+delta;if(j<0||j>=order.length)return;[order[i],order[j]]=[order[j],order[i]];renderTasks(currentStates);queuePreferenceSave()}
function renderHistory(runs){$('history').innerHTML=runs.length?runs.slice(0,12).map(x=>`<div class="run"><div class="run-main"><b>#${x.id} · ${label(x.workflow)}</b><span class="small muted">${x.started_at||'时间未知'}</span></div><span class="run-state ${x.status}">${prettyStatus(x.status)}</span></div>`).join(''):'<div class="empty">暂无行动记录</div>'}
async function refresh(){
 try{
  let d=await api('/api/status');workflows=d.workflows.filter(x=>x.id!=='self_test');
  if(!uiPreferences){let savedState=await api('/api/preferences');uiPreferences=savedState.preferences;order=[...uiPreferences.order];$('parallel').value=String(uiPreferences.max_parallel||1)}
  let busy=d.state.running,batch=d.state.batch,done=batch.completed||[];currentBusy=busy;currentStates=d.state.workflows;
  cancellableTasks=new Set([...(batch.queue||[]),...(batch.active||[]),...Object.entries(currentStates).filter(([,state])=>state.running).map(([id])=>id)]);
  [...cancellingTasks].forEach(id=>{if(!cancellableTasks.has(id))cancellingTasks.delete(id)});
  if(batch.running)[...(batch.queue||[]),...(batch.active||[])].forEach(id=>ignoredBatchStatuses.delete(id));
  done.forEach(item=>{if(currentStates[item.workflow]&&!ignoredBatchStatuses.has(item.workflow))currentStates[item.workflow]={...currentStates[item.workflow],batch_status:item.status,batch_message:item.message}});
  if(!isDragging)renderTasks(currentStates);
  $('headline').textContent=busy?'作战任务执行中':'系统待命中';$('message').textContent=batch.message||d.state.message;$('statusLight').classList.toggle('busy',busy);
  if(currentCharacter)$('operatorSpeech').textContent=busy?'「'+currentCharacter.name+'」正在关注本次行动！':currentCharacter.quote;
  $('active').textContent=(batch.active||d.state.active).map(label).join('、')||'当前无队伍出击';$('queued').textContent=batch.queue.map(label).join('、')||'队列为空';$('completed').textContent=done.map(x=>label(x.workflow)+' · '+prettyStatus(x.status)).join('\n')||'等待行动数据';
  let enabledIds=workflows.filter(w=>workflowEnabled(w.id)).map(w=>w.id),total=enabledIds.length,finished=enabledIds.filter(id=>currentStates[id]?.today_completed||currentStates[id]?.today_status==='success').length;
  $('progressText').textContent=`${finished} / ${total}`;$('progressFill').style.width=(total?Math.min(100,finished/total*100):0)+'%';$('startDaily').disabled=busy;renderHistory(d.runs||[])
 }catch(e){$('headline').textContent='指挥链路异常';$('message').textContent=e.message;$('statusLight').classList.add('busy')}
}
async function runDaily(){let selected=order.filter(workflowEnabled);if(!selected.length){notify('所有任务均为 SKIPPED，请先点击标签启用任务',true);return}try{ignoredBatchStatuses.clear();await queuePreferenceSave(false);let d=await api('/api/run-daily',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workflows:selected,max_parallel:Number($('parallel').value)})});notify('✦ '+d.message);refresh()}catch(e){notify(e.message,true)}}
async function runOne(id,force=false){try{let d=await api('/api/run?workflow='+encodeURIComponent(id)+(force?'&force=true':''),{method:'POST'});if(force)ignoredBatchStatuses.add(id);notify('✦ '+d.message);refresh()}catch(e){notify(e.message,true)}}
async function cancelOne(id){if(!cancellableTasks.has(id)||cancellingTasks.has(id))return;cancellingTasks.add(id);renderTasks(currentStates);try{let d=await api('/api/cancel?workflow='+encodeURIComponent(id),{method:'POST'});notify(d.message);refresh()}catch(e){cancellingTasks.delete(id);renderTasks(currentStates);notify(e.message,true)}}
async function stopAll(){try{let d=await api('/api/stop',{method:'POST'});notify(d.message);refresh()}catch(e){notify(e.message,true)}}
async function refreshLogs(){try{let d=await api('/api/logs?lines=300');latestLines=d.lines||[];let start=0;if(clearAnchor){let i=latestLines.lastIndexOf(clearAnchor);start=i>=0?i+1:0}let visible=latestLines.slice(start),text=visible.length?visible.join('\n'):'暂无新日志';let view=$('logView'),nearBottom=view.scrollHeight-view.scrollTop-view.clientHeight<45;if(text!==lastLogText){view.textContent=text;if($('autoScroll').checked&&nearBottom)view.scrollTop=view.scrollHeight;lastLogText=text}}catch(e){$('logView').textContent='日志读取失败：'+e.message}}
function clearLogView(){clearAnchor=latestLines.length?latestLines[latestLines.length-1]:'';lastLogText='';$('logView').textContent='终端显示已清空，等待新信号……'}
function toggleTheme(){document.body.classList.toggle('day');localStorage.setItem('gameflow-day',document.body.classList.contains('day')?'1':'0')}
function updateClock(){let now=new Date();$('clock').textContent='SYNC '+now.toLocaleTimeString('zh-CN',{hour12:false})}
function saveBeforeExit(){if(!uiPreferences)return;let body=JSON.stringify(preferencePayload());navigator.sendBeacon('/api/preferences',new Blob([body],{type:'application/json'}))}
if(localStorage.getItem('gameflow-day')==='1')document.body.classList.add('day');$('parallel').addEventListener('change',()=>queuePreferenceSave());window.addEventListener('pagehide',saveBeforeExit);drawCharacter();updateClock();refresh();refreshLogs();setInterval(updateClock,1000);setInterval(refresh,1500);setInterval(refreshLogs,1000);
</script></body></html>'''


def handler_for(manager: WorkflowManager):
    preferences = UiPreferences(manager.root / "data" / "ui_preferences.json",
                                list(manager.config["workflows"]))

    class Handler(BaseHTTPRequestHandler):
        def _json(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/status":
                workflows = [{"id": key, "name": value.get("display_name", key)}
                             for key, value in manager.config["workflows"].items()]
                self._json({"state": manager.state(), "workflows": workflows,
                            "runs": manager.store.recent(),
                            "preferences": preferences.get()})
                return
            if parsed.path == "/api/preferences":
                self._json({"preferences": preferences.get()})
                return
            if parsed.path == "/api/logs":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    count = max(20, min(int(query.get("lines", ["300"])[0]), 1000))
                except ValueError:
                    count = 300
                log_path = manager.root / "logs" / "gameflow.log"
                lines = []
                if log_path.exists():
                    with log_path.open("rb") as handle:
                        handle.seek(0, 2)
                        size = handle.tell()
                        handle.seek(max(0, size - 262144))
                        text = handle.read().decode("utf-8", errors="replace")
                    lines = text.splitlines()[-count:]
                self._json({"lines": lines})
                return
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/preferences":
                try:
                    value = preferences.update(self._body())
                    self._json({"ok": True, "preferences": value})
                except (ValueError, TypeError, json.JSONDecodeError, OSError) as exc:
                    self._json({"ok": False, "message": f"保存界面状态失败：{exc}"}, 400)
                return
            if parsed.path == "/api/run":
                query = urllib.parse.parse_qs(parsed.query)
                workflow = query.get("workflow", [""])[0]
                force = query.get("force", ["false"])[0].lower() == "true"
                ok, message = manager.start(workflow, "manual", force)
                self._json({"ok": ok, "message": message}, 200 if ok else 409)
                return
            if parsed.path == "/api/run-daily":
                try:
                    body = self._body()
                    ok, message = manager.start_daily(body.get("workflows", []),
                                                      body.get("max_parallel", 1),
                                                      bool(body.get("force", False)))
                    self._json({"ok": ok, "message": message}, 200 if ok else 409)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json({"ok": False, "message": f"请求格式错误：{exc}"}, 400)
                return
            if parsed.path == "/api/cancel":
                query = urllib.parse.parse_qs(parsed.query)
                workflow = query.get("workflow", [""])[0]
                ok, message = manager.cancel(workflow)
                self._json({"ok": ok, "message": message}, 200 if ok else 409)
                return
            if parsed.path == "/api/stop":
                ok, message = manager.stop()
                self._json({"ok": ok, "message": message}, 200 if ok else 409)
                return
            self._json({"error": "not found"}, 404)

        def log_message(self, fmt, *args):
            return
    return Handler


def serve(manager: WorkflowManager, host: str, port: int):
    ThreadingHTTPServer((host, port), handler_for(manager)).serve_forever()
