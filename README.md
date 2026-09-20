# Flower

🌷 **Flower = flow + er** —— 分流下载，合流落盘。

> *Feed it a URL. It asks the server how big the file is and whether it does ranges, splits that one
> stream into several, and writes them into a single file in place. A stream that dies costs its own
> stretch — never the download.*

极简的一页下载器：只给一个链接，它自己去问清总长度和支不支持分段，把这一条流拆成几条同时取，
按证据增减连接数，断了从盘上**真到的位置**接着取。基于 PySide6 + PySide6-Fluent-Widgets，Python 3.13。

## 技术栈

| 类别 | 选型 |
|---|---|
| 语言 | Python 3.13 |
| 界面 | PySide6 6.10.2 + PySide6-Fluent-Widgets 1.11.3（单页 FluentWindow，无侧边栏） |
| 网络 | 标准库 `urllib`，显式 `ProxyHandler`，不引第三方下载库 |
| 工具链 | ruff 0.13.0（format 只查不改）+ pytest 8.3.5 |

## 它守的几条规矩

- **只给链接就自动分流**。先发 `HEAD`；对 `HEAD` 不老实的站点（GitHub 的资产跳转链那种）退一步发
  `Range: bytes=0-0`——一个 `206` 同时回答了「总长多少」和「支不支持分段」。服务器不认分段就降级成单流，
  并在界面上明说，不装作分了。
- **断了一条只补那一段**。重试的起点不是请求起点，而是区间并集的**前沿**（盘上真到哪儿）。
  这是「单流断流不整份重来」的全部秘密。
- **终名下永远不出现半截文件**。长度对得上**且**每个字节都有窗口认领，才把 `.part` 改成真名；
  任何失败路径删 part，暂停留 part（能接着下）。
- **代理是本机显式给的**。默认 `127.0.0.1:7897`，一个开关；代理连不上就明确报错，**绝不静默改直连**——
  静默换路会让人以为加速生效了。代码里还要把 `urllib` 的 `proxy_bypass` 按掉，否则环境里一个 `NO_PROXY`
  就能把用户开着的代理悄悄绕过去。
- **流数自适应**。开始 2 条；出现「一块的身体比它的区间先结束」（停滞）就减一条，且本次任务不再往上加；
  加完等 5 秒，聚合速率没涨 5% 就不再加。上限是用户选的 1–16 条。界面上显示的是**活的**连接数
  （跑满时 `4 条流`，降过就是 `2/4 条流`）。

## 界面

一页，四行，一个会改名的按钮：

```
┌ Flower ─────────────────────────────────────────────┐
│ 链接    [ https://…                               ]  │
│ 保存到  [ D:\Downloads        ]  [ 浏览 ]  [ 打开 ]  │
│ 代理    [ 127.0.0.1 ] : [ 7897 ]  ( 关 )             │
│ 流数    [ 4 ▾ ]  1–16 条连接一起取同一份文件         │
│             [ 开始下载 ]  [ 取消 ]                   │
│ ▓▓▓▓▓▓▓░░░░░░░░  6.25 MB / 16.00 MB · 2.98 MB/s      │
│ 4 条流 · 约剩 3 秒                                   │
└──────────────────────────────────────────────────────┘
```

按钮就是状态机：开始下载 → 暂停 → 继续；旁边的取消 = 删掉 part（暂停留着，能续）。
速度是 5 秒滑动窗口差分，剩余时间和进度条同一个数据源，不会各说各话。

## 怎么跑

```bash
python -m venv --without-pip --system-site-packages .venv        # 用你自己的 Python 3.13
.venv/Scripts/python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ \
    "PySide6-Fluent-Widgets==1.11.3"
run.bat                                                          # 或 .venv\Scripts\pythonw.exe app.py
```

**venv 要隔离**：全局 `site-packages` 里那份 `qfluentwidgets` 是 PyQt5 版，直接往全局装 PySide6 版会覆盖它、
弄坏依赖它的项目。装完核对一遍——`qfluentwidgets.__file__` 在自己的 venv 里，且 `qfluentwidgets/common/config.py`
里是 `from PySide6` 而不是 `from PyQt5`。

设置存在 `%APPDATA%\Flower\settings.json`（上次的保存目录、代理地址与开关、流数），默认保存到 `~/Downloads`。

## 门禁与测试

```bash
pwsh -File scripts/check.ps1     # ruff check --fix → ruff format --check → ruff check → pytest
```

20 例测试里的假服务器能提供四种形状：正常 `206`、**忽略 Range 回 200 全量**、**身体写一半就断**、
一律 `416`。据此钉住的都是合同级的性质：并集覆盖率、续传的起点不是 0、`416` 只重探一次、
停滞会让连接数下来（且不再爬回去）、健康链路会让它上去、暂停留 part 而取消删 part。

## 实机验证（2026-09-19）

- 90 MB 文件（HuggingFace 镜像，发布方给了 LFS sha256）：**1 流 75.9s / 1.30 MB/s、2 流 35.7s / 2.71 MB/s、
  4 流 107.1s / 1.87 MB/s**，三组 sha256 与发布方公布的值逐字节相同、无 part 残留。
- 同样是 90 MB，4 流那组**线上跑了 200.5 MB 才落地 90 MB**：镜像中途停滞、窗口从断点重试，
  多开连接反而更容易被停滞打到——这条数字是「窗口多不一定快」的实测依据，也是自适应要治的病。
- 7 MB 小文件与 `curl -o` 的结果 sha256 逐字节一致。
- 界面：真实平台截图 7 个状态（下载中 / 已暂停 / 续传 / 完成 / 单流降级 / 已取消 / 失败）。
  续传断言是数值的：暂停在 6.55 MB，继续后从 **11.78 MB** 起，不是从 0。
- 942 MB 那份当天没跑完：镜像在 burst 与长时间停滞之间反复，均值 ~40 KB/s —— 是链路的事，不是工具的事。
- 自适应本身目前由单测（停滞→减流、健康→涨流）与界面验收覆盖；**这条链路上干净的 A/B 数字还欠着**，
  等链路好的时候补。

## 目录结构

```
Flower/
├── app.py                 入口：任务栏身份（AUMID）、窗口、单页
├── flower/
│   ├── config.py          记住的东西：保存目录、代理、流数
│   ├── net.py             一条出网的路：显式 ProxyHandler + 按掉 proxy_bypass
│   ├── probe.py           问清三件事：总长、支不支持 Range、文件叫什么
│   ├── landing.py         part 文件、区间并集、双条件改名
│   ├── engine.py          分块队列 + 取块线程 + 自适应 + 断点续传
│   └── gui/
│       ├── page.py        一页界面（四行 + 一个会改名的按钮）
│       └── worker.py      跑在网络线程里的那半：探测 → 下载 → 回报
├── tools/
│   ├── make_icon.py       逐档渲染 🌷 图标（16/24/32/48/64/128/256）
│   └── build_exe.py       PyInstaller 打包 + 自检（在 .venv-build 里构建）
├── tests/                 假服务器 + 20 例合同测试
├── scripts/check.ps1      门禁
└── run.bat                双击启动（pythonw，不弹黑框）
```

## 血缘

分流与落地这套算法的出处是作者自己的 [Fungi](https://github.com/CN-Fungi/Fungi)：那边把「一条流拆成 N 条、
断了只补那一段」用在局域网传文件上（落地铁律与窗口记账两节）。Flower 与它**算法同源、代码不共享**——
那边那 ~200 行带着日志与配置依赖，搬过来不如照着重写一遍干净。

## License

MIT，见 [LICENSE](LICENSE)。
