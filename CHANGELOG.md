# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。

## [7.1] - 2026-10-20

当前版本。源码 `wallpaper_switcher_v7.py`（单文件、零第三方依赖），打包 `wallpaper_switcher_v7.spec`；预编译产物见 v7.1 Release：`WallpaperSwitcherTrayV7-win64.zip`（onedir）。

### 新增
- **多时段/多文件夹**：配置重构为 `schedules` 列表（名称/起止时间/文件夹/启停），UI 提供时段编辑器（增删/上下移/启停/换文件夹）；无匹配时段回退首个启用时段；禁用某段即该时段不切换；旧 day/night 配置自动迁移
- **递归扫描**："递归扫描子目录"开关（`rglob` + 深度上限）；扫描移入后台线程，网络盘/占位符目录不再冻结 UI
- **日志可观测**：`RotatingFileHandler` 滚动写入 `%APPDATA%/WallpaperSwitcher/app.log`，UI 与托盘均有"打开日志"；`tick`/`poll_events` 不再吞异常，改为 `logging.exception`
- **软件外观自定义**：`ttk.Notebook` 卡片式四页（基本设置/时段与文件夹/软件外观/关于）；窗口背景 **默认 / 毛玻璃（glass，透出壁纸）**；强调色 6 色预设色板（`#3A7AFE` 蓝 / `#8B5CF6` 紫 / `#06B6D4` 青 / `#10B981` 绿 / `#F59E0B` 橙 / `#EF4444` 红）
- **深浅主题实时跟随**：子类化 tk 顶层窗口监听 `WM_SETTINGCHANGE`（ImmersiveColorSet），系统切换深浅色即时刷新皮肤
- **多显示器 DPI 统一**：拦截 `WM_DPICHANGED`，按新 DPI 重算 `tk scaling`，字号按 `dpi/96` 比例统一生成
- **托盘增强**：图标跟随 UI 主题（`TrayIcon.set_dark`）而非时段；`_add_icon` 失败 3s 延迟重试
- **毛玻璃拖动防黑闪**：`WM_ENTERSIZEMOVE` 期间临时降级 `BLURBEHIND`，`WM_EXITSIZEMOVE` 恢复 Acrylic（规避 Win11 平台已知问题）
- **切换即时生效**：保存后 `next_switch=0`，立即应用新设置
- **防重启重复**：`last_used` 持久化到 config（与保存合并写回，修复过一次"保存即丢失"的覆盖问题），抽中上次用过的图会自动换一张
- **WebP 单图兼容**：先尝试 `IDesktopWallpaper.SetWallpaper`，失败回退 SPI 并提示改用幻灯片
- 单实例广播携带"是否后台"意图（`wParam`），已有实例据此保持隐藏或唤起前台
- COM 调用失败落日志（含 HRESULT），连续失败自动降级 SPI 并在状态栏提示
- 进程存活自愈：扫描锁 `try/finally` + 60s 超时强制复位；心跳检查 `last_ok_switch` 超时主动恢复；扫描/切换期间"立即切换"按钮临时禁用防事件堆积

### 修复
- **"编辑所选时段"白块**：该 `LabelFrame` 是全项目唯一未指定 `style='Card.TLabelframe'` 的一处，clam 默认浅灰白底导致深色/glass 主题下刺眼发白——已补样式并全局复查
- `-transparentcolor` 复位传空串在部分 Tk 8.6 版本无效（`ValueError` 被吞）——改为重设窗口背景色，确保透明键被清除
- 保存设置前增加时段校验 `_validate_schedules()`：时间格式非法即拦截提示，不再先弹文件夹选择框
- 版本标识统一为 `VERSION = '7.1'`（标题、config、spec 注释与源文件名对齐）

### 变更（含按用户反馈的移除）
- **移除**软件背景的 solid / gradient / image 三档：gradient 逐条矩形全量重绘导致拖窗卡顿假死、image 因 `tk.PhotoImage` 不支持 JPG/BMP/WebP 而静默空白、solid 需手填 hex 基本不可用；仅保留 `default` / `glass`，旧配置自动回落 `default`
- **移除**强调色自由 hex 输入与 `colorchooser`，改为预设色板；旧自定义色回落默认蓝
- 主题切换（含"跟随系统"触发）不再连带 `save_settings()`：不落盘、不校验、不重跑 PowerShell 重建自启
- 保持零第三方依赖、单文件、onedir 打包形态不变

### 实机回归验证清单
深色/浅色/glass 三主题遍历四标签页无发白；系统切换深浅色只换皮肤（无 PowerShell 闪窗/弹窗，config 不变）；default↔glass 反复切换无残留透明键；glass 拖窗无黑闪；时段编辑器全操作配色统一；换色保存重启后保持；重启 explorer 托盘自动恢复；旧配置自动迁移无报错；重新打包启动无缺模块。

## [6] - 2026-10-08

初版（源码 `wallpaper_switcher.py` + `wallpaper_switcher.spec`，已归档保留）。

### 新增
- 白天/夜间两时段壁纸，各绑一个文件夹，支持跨午夜（如夜间 19:00–07:00）
- 洗牌袋随机算法，一轮内不重复；间隔可调 1 分钟 ~ 7 天
- 幻灯片模式：`IDesktopWallpaper` COM（ctypes 手写 vtable）系统级淡入淡出；暂停/继续与"立即切换"（AdvanceSlideshow）联动
- 深色主题三档：跟随系统 / 浅色 / 深色，标题栏经 DWM 同步沉浸深色
- 纯 ctypes 系统托盘：手写图标与右键菜单，日/夜图标按当前时段自动切换，按 `SM_CXSMICON` 真实像素加载，`TaskbarCreated` 后自动重挂
- 开机自启：当前用户"启动"文件夹 `.lnk`（非注册表 Run，免管理员权限），exe 移动位置后自愈重写
- 单实例：`CreateMutexW` + `ERROR_ALREADY_EXISTS`；重复启动广播唤起已有窗口
- 原子写配置：临时文件 + `os.replace`，崩溃/断电不丢设置

### 修复（初版开发期）
- 调度器读取实时输入框导致半截时间使 1 秒心跳链静默停摆 —— 改为仅用已保存值，且异常后仍重新排程
- `CreateWindowExW` 参数个数错误导致托盘图标从未创建成功
- 图片文件夹位于网络盘/占位符目录时同步扫描冻结 UI —— 改为后台线程扫描
- 随机选择可能连续给出同一张壁纸 —— 洗牌袋一轮内不重复

### 变更
- 移除 `random`/`hashlib` 依赖（改用 `os.urandom`），构建不再携带约 5.6MB 的 OpenSSL
- 裁剪打包内 Tcl/Tk 的 locale/时区/多余编码数据（约 4.3MB → 0.9MB）
- 启动即声明 DPI 感知，高分屏界面不再被系统拉伸
