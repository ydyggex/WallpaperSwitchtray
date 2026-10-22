# 随机壁纸切换器 v7.1 修正报告

> 依据 2026-10-20 实机截图（基本设置 / 时段与文件夹 / 软件外观 / 关于 四页）与用户反馈整理。
> 覆盖对象：`wallpaper_switcher_v7.py`（单文件）、`wallpaper_switcher_v7.spec`。
> 优先级：P0（稳定性/必修）> P1（功能缺陷）> P2（体验优化）。

---

## 一、用户反馈问题与根因

### A1.【P1】软件背景只保留 `default` 和 `glass`，删除其余三种

**用户感知**：solid / gradient / image 三种"均无效或卡住"。

**根因（代码已核实）**：

| 模式 | 具体问题 |
|---|---|
| `gradient` | `_draw_gradient` 用 60 个 `create_rectangle` 逐条绘制，且绑定在 `<Configure>`（`_on_bg_resize`）上——**窗口拖边缩放时每个像素变化都全量重绘**，造成明显卡顿甚至假死 |
| `image` | Tk 原生 `tk.PhotoImage` 只支持 PNG/GIF；文件选择框却允许选 JPG/BMP/WebP，选了必然失败（静默空白）。且 `_draw_image` 的等比缩放分支被 `if False` 硬编码关闭，大图只露中间一块 |
| `solid` | 需要用户手填 `#RRGGBB`，无取色预设，基本不可用 |

**修正方案**：
1. 外观页"软件背景"下拉只保留 `('default', 'glass')`；
2. 删除"值"输入行、`浏览…` 按钮、`_pick_bg_value`、`_draw_gradient`、`_draw_image` 及相关分支（`_apply_app_background` / `_draw_background` 收敛为两种模式）；
3. **配置迁移**：`_normalize_config` 中若 `app_bg.mode ∈ {solid, gradient, image}` → 自动回落 `default`，不报错；
4. 提示文案同步更新。

**验收**：外观页仅两项可选；旧配置含已删模式时启动正常且自动回落；glass 效果不受影响。

---

### A2.【P2】强调色改为预设色板，去掉自由填色

**用户感知**："很鸡肋，大部分人颜色感不好"。

**现状**（`wallpaper_switcher_v7.py:1122-1124、1237-1245`）：自由输入 `#HEX` + 系统 `colorchooser` 对话框，无任何引导。

**修正方案**：
1. 替换为 **6 个预设色票按钮**（可辨识度高、深浅色底都协调）：

   | 名称 | 色值 |
   |---|---|
   | 蓝（默认） | `#3A7AFE` |
   | 紫 | `#8B5CF6` |
   | 青 | `#06B6D4` |
   | 绿 | `#10B981` |
   | 橙 | `#F59E0B` |
   | 红 | `#EF4444` |

2. 当前选中色票加高亮描边；点击立即生效并随"保存设置"持久化；
3. 删除 hex 输入框与 `colorchooser`（`_pick_accent` 整体移除）；
4. **配置兼容**：旧 `config.json` 中不在色板内的 accent → 回落 `#3A7AFE`；
5. Accent 按钮色、进度/焦点色、勾选焦点色统一取自色票。

**验收**：点色票换色即时可见；重启后保持；旧配置无害迁移。

---

### A3.【P1】"编辑所选时段"区域周围一片白，与整体不符

**根因（已定位到行）**：`wallpaper_switcher_v7.py:1093`

```python
form = ttk.LabelFrame(card, text='编辑所选时段', padding=10)
```

这是**全项目唯一一处未指定 `style='Card.TLabelframe'` 的 LabelFrame**。它走了未配置的默认 `TLabelframe` 样式——clam 主题默认浅灰白底，因此在深色 / glass 主题下出现刺眼的白色块（截图 2 中"编辑所选时段"标题框及周边）。其余所有卡片都通过 `_card()`（1011-1014）正确使用了 Card 样式，所以只有这一处发白。

**修正方案**：补上 `style='Card.TLabelframe'`；同时全局 `grep LabelFrame(` 复查，确认无第二处。

**验收**：深色 / glass / 浅色三种主题下，该区域与相邻卡片颜色完全一致。

---

### A4.【P0】稳定性加固（逐条列出代码中真实的不稳定点）

以下为本次代码审查补充的**具体不稳定位置**，按风险排序：

| # | 位置 | 问题 | 修正方案 |
|---|---|---|---|
| S1 | `on_theme_change`（886-889） | 系统"深/浅色"切换 → 除了刷 UI，还会 `save_settings()`：**每次都重跑 PowerShell 重建开机自启 .lnk、无条件重写 config**；若当时时段格式不合法还会弹出"时间格式错误"模态框。用户只是切了下系统主题，软件却连环动作 | 主题变更只调 `_apply_theme()` + `_apply_app_background()`，**不落盘、不校验、不重建自启**；`set_startup()` 仅在自启勾选状态实际变化时执行；时段校验仅归属"保存设置" |
| S2 | `save_config()` | 写死字段字典，**不含 `last_used`**——切图后只要点一次"保存设置"，防重启重复的记录就被覆盖丢失 | `save_config` 先读旧文件合并 `last_used` 再写回；或拆分为独立 `state.json` |
| S3 | Acrylic（Win11 已知平台问题） | `ACCENT_ENABLE_ACRYLICBLURBEHIND` 在**拖动/缩放窗口时会出现黑闪与迟滞**（未公开 API 的通病） | 监听 `WM_ENTERSIZEMOVE`：拖动期间临时切换 `ACCENT_ENABLE_BLURBEHIND`（无黑闪），`WM_EXITSIZEMOVE` 后恢复 Acrylic |
| S4 | `DW_*` COM vtable | `IDesktopWallpaper` 槽位号（`DW_SET_WALLPAPER=4` 等）为硬编码经验值，不同 Windows 版本布局若变化会调用错位。异常已有保护但失败会静默 | 失败时必须落日志（含 HRESULT）；连续失败自动降级 SPI 路径并在状态栏提示 |
| S5 | 托盘 `_add_icon`（541、558-559） | `TaskbarCreated` 已正确处理（explorer 重启后重建图标）✅；但 `_add_icon` 若失败**无重试**，任务栏重建瞬间调用可能失败，之后永远无图标 | `_add_icon` 失败时 `after(3000)` 延迟重试一次，仍失败记日志 |
| S6 | `WM_DPICHANGED` 处理 | 只重算 `tk scaling`（影响控件默认尺寸），但**标题(18)、卡片标题(11) 等硬编码字号不随 DPI 缩放**，跨屏拖动后观感不一致 | 字号改为按 DPI 比例计算：`font_size * dpi/96`，在 `_apply_theme` 内统一生成 |
| S7 | tick 线程退出竞态 | 窗口销毁后 worker 线程仍可能 `status_var.set(...)` → 抛 `TclError`（虽被捕获但日志噪音大） | `on_close` 置 `running=False` 后 join 线程（超时兜底）；所有 `after` 循环在销毁后不再重入 |
| S8 | `-transparentcolor` 复位 | 非 glass 分支传空串 `''`，部分 Tk 8.6 版本会抛 `ValueError`（目前被 except 吞掉，但复位可能实际未生效，残留透明键） | 复位改为重设为窗口背景色本身（如 `pal['bg']`），确保键被清除 |
| S9 | 扫描防重入 | 扫描已有 `_scan_busy` + try/finally 保护 ✅；但扫描进行中"立即切换"按钮仍可连点，事件堆积 | 扫描/切换期间按钮临时 `state='disabled'`，完成后恢复 |

**已核查无需改动**：单实例互斥、`TaskbarCreated` 重建、`_scan_busy` 自愈、日志滚动——逻辑在位。

---

## 二、修改优先级汇总

| 优先级 | 编号 | 内容 | 改动量 |
|---|---|---|---|
| P0 | A4-S1 | 主题切换不再触发保存/PowerShell/弹窗 | 小 |
| P0 | A4-S2 | `last_used` 持久化防覆盖 | 小 |
| P0 | A4-S3~S9 | 其余稳定性加固（S4/S5/S7 为必改，其余建议一并） | 中 |
| P1 | A3 | "编辑所选时段"补 Card 样式（一行） | 极小 |
| P1 | A1 | 软件背景收敛为 default/glass + 配置迁移 | 中 |
| P2 | A2 | 强调色预设色板 | 小 |

## 三、回归验证清单（修完后需在实机确认）

1. 深色 / 浅色 / 跟随系统 三种主题下遍历四个标签页，无发白、无错色块；
2. 在 Windows 设置里切换深浅色 → 软件只换皮肤，**无 PowerShell 闪窗、无弹窗、config.mtime 不变**；
3. 软件背景 default ↔ glass 来回切换 5 次：透明正常建立与清除，无残留透明键；
4. glass 模式下拖动窗口：无黑闪（S3 生效）；
5. 时段编辑器：增/删/上移/下移/改文件夹/保存，功能正常且配色统一；
6. 点色票换色 → 保存 → 重启，颜色保持；
7. 任务管理器重启 explorer → 托盘图标自动恢复；
8. 旧 v7.1 配置（含 solid/glass/自定义 accent）启动后自动迁移，无报错；
9. 重新打包 `pyinstaller wallpaper_switcher_v7.spec` 后启动无 `ModuleNotFoundError` / 缺 DLL。

## 四、不做的事

- 不引入第三方依赖（保持零依赖、单文件）；
- 不恢复 solid/gradient/image 背景与自由取色（明确移除，不是延后）；
- 不改打包形态（维持 onedir）。
