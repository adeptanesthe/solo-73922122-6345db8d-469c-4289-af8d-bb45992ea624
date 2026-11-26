# ABX 盲听校准台（Flask + NumPy）

给录音师比较同一段对白的两版降噪结果：上传两份**已对齐、等长、至多 30 秒的
16 kHz 单声道 PCM16 WAV**，圈出 1～6 段两版均非静音的区间；系统只给 **B** 版乘
一个全片共用的正增益，按**未量化**的增益结果把每段 RMS(B)/RMS(A) 校准到
**0.95～1.05**，同时保证全片不超出 PCM16 范围，取**最接近 1** 的可行增益；无解时
给出数值化原因。校准通过后开题，系统**等概率**选定 X=A 或 X=B，校准音频与答案在
开题时冻结；A/B/X 共用同一个播放器并从**同一播放位置**续听；提交判断后才显示答案。
可在另一页改选区间再开新题，旧题始终用各自冻结的音频判分。

**审听附件打包（新增）**：圈选页内置“打包”入口，把原始 A/B 各切成 **1 秒一块**
（尾块保留实际长度），同号 A/B 块共用位深：原样 **PCM16** 或**取高 8 位转 PCM8**；
与圈选保真段相交的块强制 PCM16。所有分块 WAV 以 **DEFLATE** 打入同一个 ZIP，
在用户填写的**整份 ZIP 字节上限**内，用精确 0/1 背包求“解包还原 PCM16 后两轨相对
原音的样点平方误差总和”最小的位深分配。网页直接从这份 ZIP 还原 A/B 与逐块试听，
并下载**同一份包**；试听中可勾选“保真块”并按**原上限**重新分配，放不下时提示并
完整保留上一份包及其标记。详见下文**第 3 节《审听附件打包》**。

---

## 1. 用 Docker Compose 启动（推荐）

```bash
docker compose up --build
# 打开 http://localhost:5000
# 若 5000 端口被占用： ABX_PORT=8080 docker compose up
```

- 服务监听容器内 `5000`，宿主机端口可用 `ABX_PORT` 覆盖。
- 题目 / 工作区数据保存在命名卷 `abx-data` 挂载的 `/data`（环境变量
  `ABX_DATA_DIR`），重启不丢旧题。
- 镜像基于 `python:3.11-slim`，用 `gunicorn wsgi:app`（1 worker / 8 线程）启动，
  带 `/healthz` 健康检查。
- 仓库已附带一对可试听样音 `samples/sample_A.wav`、`samples/sample_B.wav`
  （各 15 秒，16 kHz mono PCM16，NumPy 合成的“对白感”短语 + 两种降噪风格），
  首页可直接试听、下载，或一键“使用这对样音”。

本地直接跑（不用 Docker）：

```bash
pip install -r requirements.txt
python wsgi.py                 # http://127.0.0.1:5000
# 或 gunicorn --bind 0.0.0.0:5000 wsgi:app
```

重新生成样音：`python samples/make_samples.py`（脚本会自检校准结果）。

---

## 2. 网页操作流程

1. **首页 `/`**：选择/拖入 A、B 两份 WAV（或直接载入样音）。服务端立即校验
   RIFF/WAVE、PCM16（兼容 EXTENSIBLE/PCM 子格式）、16 bit、单声道、16 kHz、
   等长、≤480 000 采样（30 s），错误会逐条中文提示。
2. **圈选页 `/workspaces/<wid>/segments`**：
   - A/B 双波形对齐渲染 + 时间轴；在波形上**横向拖选**即可加段，也可点
     “建议区间”（自动检测两版均非静音的连续区间）或用起止秒输入框精确编辑；
     最少 1 段、最多 6 段。
   - 区间变化后自动校准，展示：采用增益 `g`（含 dB）、RMS 交集上下界、PCM16
     削波上限、每段 RMS A/B、原生 B/A、**未量化校准后 B/A** 与达标标记。
   - 无解时顶部红色面板说明原因，“开题”请求也会被服务端以 `409 infeasible`
     硬拒绝。
3. **打包审听附件（同一圈选页中部“审听附件打包”卡片）**：
   - 在“整份 ZIP 字节上限”填字节数（默认 `600000`，旁边有 450/550/650/700 KB
     与“全 PCM16”快捷值），点 **生成打包**。页面把当前圈选段作为保真段提交。
   - 成功后显示 ZIP 实际大小/上限/剩余、PCM16 与 PCM8 块数、还原总 SSE、最小可行
     ZIP；**A/B 两个播放器直接从下载到浏览器里的同一份 ZIP 解包还原**（无需另请
     服务端转码），每块还有独立小播放器可逐块试听。
   - 块列表中：与圈选段相交的块显示“圈选保真”，勾选框常勾且禁用；其余块可勾选
     “保真块”。勾好后点 **应用保真标记，并按原上限重新分配**，得到 v+1 新包，
     **标记、A/B/逐块试听、下载按钮一起切到新 ZIP**。
   - 若强制保真后最低也要超过原上限，红色面板给出“至少需要 X 字节 / 上限 Y”，
     页面**回退显示并继续提供上一份包、它的标记、试听与下载**；服务端文件与版本号
     完全不变（接口 `409 code:"package_does_not_fit"` 且带 `package`  retained 记录）。
   - **下载这份 ZIP** 保存的就是网页还原所用的同一个 Blob（服务端
     `/package/download` 的字节逐字节一致，响应头带版本文件名）。刷新圈选页会
     自动拉回当前包并重新还原。
4. **ABX 题页 `/questions/<qid>`**：
   - 三个大按钮 **A / B / X**，全页只有一个 `<audio>` 元素；切换时记录
     `currentTime` 与播放状态，新音源 `loadedmetadata` 后 seek 到同一位置并按需
     续播（实测 `5.00s → 5.00s` 精确保持）。
   - 提供各对比段的“跳转 + 段内循环”小芯片；快捷键 `A/B/X` 切音源、空格播放暂停。
   - 选择“X 是 A / X 是 B”并提交后，才渲染绿色/红色结果与“正确答案：X 就是 ?”，
     同时 X 按钮副标题揭示身份；提交一次后锁定，重复提交以第一次判分为准。
5. **题目记录 `/questions`**：列出所有题（段数、增益、状态、对错、所选与答案）。
   未答题只显示“待判断”，列表与详情 API 都不含 `x_is`。
6. **改选再开题**：题页右上“改选对比段再开新题”回到**同一工作区**的圈选页，可任意
   改段、校准、开新题；每道题在 `data/questions/<qid>/` 下保存自己的
   `a.wav`（原始 A 字节）、`b_calibrated.wav`（开题时渲染冻结）、`x.wav`
   （按开题时的随机答案复制 A 或校准 B）与 `record.json`（含答案、段、增益、判分），
   所以旧题的试听与判分完全不受后来改段影响。

---

## 3. 审听附件打包（ZIP/DEFLATE + PCM16/PCM8 预算内最优）

实现：`app/packaging.py`（纯领域逻辑）、`app/storage.py`（每工作区一份当前包，
成功才原子替换）、`app/routes.py`（JSON/下载/服务端还原兜底）、
`app/static/js/zip-restore.js`（零依赖浏览器解 ZIP + PCM8/16 还原）、
`app/static/js/package.js`（圈选页面板）。

### 3.1 分块与高 8 位 PCM8

- 块长固定 `16000` 采样（16 kHz 下正好 1 s），块边界
  `[0,16000),[16000,32000),…`，最后一块为实际尾长
  （如 2.1 s → `16000,16000,1600` 三块）。
- 块 `i` 的 A/B WAV 同名同编号（`A/chunk_000.wav`、`B/chunk_000.wav`），
  **两块位深强制相同**。
- PCM8 严格“取高 8 位”：无符号 8-bit WAV（WAVE 规范里 8-bit PCM 即无符号）：

\[
u_8=\operatorname{clip}_{0..255}\!\left((s_{16}\mathbin{>>}8)+128\right),\qquad
\hat s_{16}=(u_8-128)\times256 .
\]

  映射 `-32768→0, 0→128, 32767→255`；PCM16 块原样保留，还原误差为 0。
- 与任一“圈选保真段”半开区间相交（`ss < ce and se > cs`，仅边界相接不算）的块，
  以及手动保真块，强制 PCM16。

### 3.2 ZIP 结构与“字节上限”的精确计费

```
abx-package-<wid>-v<N>.zip
├── manifest.json          # 固定 4096 B、ZIP_STORED（JSON 后补空格，仍是合法 JSON）
├── A/chunk_000.wav …      # 每块 WAV 单独 raw-DEFLATE（level 9）
└── B/chunk_000.wav …
```

DEFLATE 是**逐条目独立 raw 流**，ZIP 本地头 30 B、中央目录项 46 B、EOCD 22 B，
文件名/时间戳/extra 均固定，因此一个条目的 ZIP 字节贡献可在压缩后**精确预测**：

\[
c_i^{(d)}=30+|\text{name}_i|+\underbrace{|\operatorname{Deflate}_9(\text{WAV}_{i,d})|}
_{\text{实测压缩流长度}}+46+|\text{name}_i| .
\]

固定 4096 B 的 STORED `manifest.json` 让非加性的清单开销也变成常数。单测对多种
随机分配断言 `预测总字节 == zipfile 实际产出字节`（见 `tests/test_package.py`）。

### 3.3 让总平方误差最小：精确 0/1 背包

第 `i` 对块若保持 PCM8，承担已知整数误差

\[
E_i=\sum_{k\in A_i}(a_k-\hat a_k)^2+\sum_{k\in B_i}(b_k-\hat b_k)^2 ,
\]

若升 PCM16 则误差 0，并多占 `Δ_i=c_i^{(16)}-c_i^{(8)}` 个 ZIP 字节。先固定强制
PCM16 块、并对 `c16≤c8` 的“免费更优”块直接取 16，得到最小可行 ZIP `base`。
剩余可选项构成 0/1 背包：容量 `budget-base`，物品重量 `Δ_i`、价值 `E_i`。

音频至多 30 s ⇒ 块数 ≤ 30，用**折半搜索（meet-in-the-middle，每半 ≤2¹⁵）精确
求解**，不依赖预算数值大小；同价值时取更轻方案。测试用 4 块音频枚举全部
`2^4` 强制集合做暴力对照，各预算下优化器 SSE 都等于暴力最优。

`budget < base` 时抛 `PackageDoesNotFit`（中文消息给出 `minimum_bytes` 与差额），
**任何文件都尚未写入**；重新分配路径还会把旧包记录放进 `report["current"]`。

### 3.4 HTTP 接口

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /api/workspaces/<wid>/package` | body `{"budget_bytes":600000, "segments":[…], "manual_chunks":[2]}`；段可 0 段、接受秒/采样键别名。可行 → `201/200 {package}`；超下限 → `409 code:package_does_not_fit`（含 `minimum_bytes`，若有旧包另带 `package`）；参数错 → `400 code:invalid_package` |
| `GET /api/workspaces/<wid>/package` | 当前包；工作区存在但无包 → `200 {package:null}`（让页面静默探测不产生 404 控制台噪音）；未知 wid → `404` |
| `POST /api/workspaces/<wid>/package/reallocate` | body `{"manual_chunks":[…]}`，可带 `budget_bytes` 覆盖；**默认沿用包内冻结的原上限与圈选段**。成功 → `200 {package}`（version+1）；放不下 → `409` 且 `retained:true` + 旧 `package`，磁盘旧 ZIP/记录不变 |
| `GET /api/workspaces/<wid>/package/download` | 逐字节返回当前 `package.zip`（`application/zip`，文件名带 v 版本，`no-store`） |
| `GET /api/workspaces/<wid>/package/restored/{A,B}.wav` | 服务端把**同一份库存 ZIP** 解包补回完整 PCM16 WAV；主要供无 `DecompressionStream` 的浏览器兜底与脚本核对 |

`package` 视图含：`zip_bytes/budget_bytes/minimum_bytes/total_sse`、
`bit_depths/pcm16_chunk_indices/pcm8_chunk_indices`、
`intersection_chunks/manual_chunks/forced_chunks` 与逐块
`start/end/bit_depth/sse_pair/sse_if_pcm8/bytes8_pair/bytes16_pair/extra_bytes_to_16`。

### 3.5 网页演示步骤（Docker Compose）

```bash
docker compose up --build          # http://localhost:5000  (ABX_PORT=8080 可改端口)
```

1. 首页点“**使用这对样音**”（15 s 样音，默认预填 2 个建议保真段）。
2. 进入圈选页后直接在中部“审听附件打包”卡片点“**生成打包**”（默认 600000 B
   对预填两段可行；也可先把圈选改成 `5.00–7.00 s` 单段再试 300000 B）。
3. 看绿色完成提示与容量条；在“A · 从 ZIP 还原 / B · 从 ZIP 还原”播放器试听，
   或点每个块右侧的小播放器逐块听；点“**下载这份 ZIP**”，本地解压可见上述布局。
4. 勾选比如 `#2` 的“保真块”→“**应用保真标记，并按原上限重新分配**”：
   块 `#2` 变绿 PCM16，提示 `v2`，两个主播放器与下载都换成新包。
5. 把所有未禁用块都勾上再重新分配（或把上限改成一个很小值重新生成）：出现红色
   “放不下……至少需要 XXX 字节”，勾选/播放器/下载回退到上一份 vN 包；可在
   `data/workspaces/<wid>/` 看到 `package.zip` 与 `package.json` 版本号未变。
6. 命令行黑盒复核：`BASE=http://127.0.0.1:5000 python scripts/smoke.py`
   （含 10 项打包检查）；浏览器库互操作：
   `node tests/package_http_check.mjs`。

---

## 4. 校准规则（与需求逐条对应）

设第 \(i\) 段的均方根振幅（float64、未量化）为

\[
R_A^{(i)}=\sqrt{\frac1{|S_i|}\sum_{k\in S_i} a_k^2},\qquad
R_B^{(i)}=\sqrt{\frac1{|S_i|}\sum_{k\in S_i} b_k^2},\qquad
r_i=R_B^{(i)}/R_A^{(i)} .
\]

只对 B 乘一个全片共用的正增益 \(g\)。要求

\[
0.95\le g\,r_i\le 1.05
\quad\Longleftrightarrow\quad
g\in\left[\frac{0.95}{r_i},\frac{1.05}{r_i}\right].
\]

因此所有段的 RMS 可行交集为

\[
G_{\rm lo}=\max_i \frac{0.95}{r_i},\qquad
G_{\rm hi,RMS}=\min_i \frac{1.05}{r_i}.
\]

**PCM16 削波上限**按“未量化的 \(gB\) 不得超出 PCM16 数学范围
\([-32768,32767]\)”精确计算（保证 \(g=1\) 对任何合法输入都可行）：

\[
g\le G_{\rm clip}=
\min\left(
\frac{32767}{\max B_{>0}},
\frac{32768}{\max |B_{<0}|}
\right).
\]

总可行区间为 \([G_{\rm lo},\ \min(G_{\rm hi,RMS},G_{\rm clip})]\)。**最接近 1 的
可行增益**就是把 1 夹进该区间：

```text
g* = clamp(1, G_lo, min(G_hi_RMS, G_clip))
```

- \(1<G_{\rm lo}\)：B 整体偏轻，取 \(g=G_{\rm lo}\)（最小提升）；
- \(1>\) 上界：B 整体偏响，取上界；
- 否则 \(g=1\)。

无解的三种 `reason_code`（API 与页面都给中文原因和数值）：

1. `silent_segment`：某段 A 或 B 的 RMS 低于静音阈值（约 −75 dBFS，
   `10^(-75/20)·32768 ≈ 5.83` LSB；数字零必被拦）。
2. `disjoint_rms_windows`：各段增益窗口无交集；指出决定下界（B 相对 A 最轻）与
   上界（B 相对 A 最响）的段号、原生比值和具体 \(g\) 边界。
3. `clipping`：满足 RMS 至少需要 \(g\ge G_{\rm lo}\)，但全片峰值只允许
   \(g\le G_{\rm clip}\)，再大就削波。

开题时用 `np.rint(g·B)` 后写入标准 PCM16 WAV（带防御性 clip，校准通过时不会触发）。
需求要求的比值按**未量化**的 `g·B` 计算并展示；量化后实测比值变化在约 \(10^{-4}\)
量级，仍在区间内。

---

## 5. HTTP / JSON API 摘要

便于脚本化验证（文件上传字段支持 `wav_a/wav_b` 以及 `a/b/file_a/file_b/...`
等别名；两个文件部分也可按顺序兜底识别）。**打包相关 5 个端点的完整字段见
[§3.4](#34-http-接口)**。

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /healthz`（或 `/health`） | 健康检查 |
| `POST /upload` / `POST /api/workspaces` | multipart 上传 A/B；表单提交 303 跳转，JSON/`Accept: application/json` 返回 `workspace_id` |
| `POST /samples/use` | 用仓库样音创建工作区（GET 也可，方便直接点链接） |
| `GET /api/workspaces/<wid>` | 元信息、双波形 min/max 峰值、自动非静音建议段 |
| `POST /api/workspaces/<wid>/calibrate` | body `{"segments":[{"start_sample":..,"end_sample":..}]}`（也接受秒字段 `start/end`）；返回完整校准报告，无解时 `feasible:false` + reason |
| `POST /api/workspaces/<wid>/questions` | 同样 body；可行 → `201` 题目（不含答案），无解 → `409 code:infeasible`，段非法 → `400` |
| `GET /questions/<qid>` | ABX 页面 |
| `GET /api/questions/<qid>` | 题目详情；**未答时无 `x_is`/`answer`**，已答才返回答案与判分 |
| `GET /questions/<qid>/audio/{A,B,X}` | 冻结的 WAV；`X` 是不暴露身份的不透明 URL |
| `POST /api/questions/<qid>/answer` | `{"choice":"A"|"B"}`（兼容 `answer/judgment/...` 与大小写）；返回 `x_is`、`correct`，重复提交返回首次记录 |
| `GET /questions` / `GET /api/questions` | 历史页 / 列表摘要（未答题不含答案） |

---

## 6. 目录结构

```
.
├── Dockerfile
├── docker-compose.yml
├── requirements.txt          # 锁定到交付时实测版本（打包只用标准库 zipfile/zlib）
├── wsgi.py                   # gunicorn / python wsgi.py 入口
├── app/
│   ├── __init__.py           # create_app、错误页、包级 app（flask --app app）
│   ├── wavutil.py            # 手写 RIFF 解析、严格格式校验、PCM16 编码
│   ├── calibration.py        # 段解析、RMS、增益区间/削波/最接近 1 的解、波形与建议段
│   ├── packaging.py          # 1s 分块、高8位 PCM8、精确 ZIP 计费、MITM 背包、解包还原
│   ├── storage.py            # 工作区/题目/当前 ZIP 包持久化、随机 X、提交判分（原子写）
│   ├── routes.py             # 页面 + JSON API + 音频 + 打包/下载/还原
│   ├── templates/            # base/index/segments/package 卡片/question/questions/error
│   └── static/{css,js}       # 原生 JS（segments/package/zip-restore/question/index），无 CDN
├── samples/
│   ├── make_samples.py
│   ├── sample_A.wav          # 15 s / 16 kHz / mono / PCM16（480 044 B）
│   └── sample_B.wav
├── scripts/smoke.py          # 对运行中服务的纯标准库黑盒冒烟（含 10 项打包检查）
└── tests/
    ├── test_app.py           # 21 个 unittest（校验/校准数学/全流程/无答案泄露/X 分布）
    ├── test_package.py       # 22 个 unittest（PCM8/分块/背包/精确计费/暴力最优/HTTP 保留）
    ├── package_http_check.mjs    # Node 真实 HTTP + 同一份 JS 解包库的端到端互操作
    ├── package_browser_walk.mjs  # Playwright/Chromium 打包 UI 全流程（下载/重分配/回退）
    ├── package_default_flow.mjs  # Playwright 开箱默认值/预填段首点成功 + 刷新恢复
    └── browser_walk.mjs          # 可选 Playwright ABX 真浏览器走查脚本
```

---

## 7. 交付前自检：实现了什么 / 实际测到什么

### 7.1 打包功能（本轮新增）对照需求

- [x] 圈选页新增“审听附件打包”卡片入口（字节上限输入 + 快捷值 + 生成按钮）。
- [x] A/B 各切 1 s（16000 采样）块，尾块保留实际长度；同号 A/B 块同一位深。
- [x] PCM16 原样；PCM8 = 高 8 位 + 128 的标准无符号 8-bit WAV，还原 `(u8−128)×256`。
- [x] 与圈选保真段相交的块（半开相交判定）强制 PCM16；另有手动保真块。
- [x] 分块 WAV 全部 raw-DEFLATE(level 9) 打入同一个 ZIP，含固定槽 `manifest.json`。
- [x] 整份 ZIP ≤ 用户上限；精确 ZIP 计费 + 折半 0/1 背包求两轨总 SSE 全局最小，
      并用全枚举暴力对照验证最优性。
- [x] 网页用零依赖 `zip-restore.js`（原生 `DecompressionStream('deflate-raw')`）
      从**同一份 ZIP Blob** 还原 A/B 主播放器与逐块播放器；下载按钮保存同一 Blob。
- [x] 勾选保真块 → 按**原上限**重新分配；成功后标记/试听/下载一起更新（version+1）。
- [x] 放不下 → 中文提示最低字节；`409 retained`，服务端不写新文件，前端回退到
      上一份包的标记、试听与下载（Playwright 断言 ZIP 字节与版本号均不变）。
- [x] 刷新页面自动恢复当前包；不支持网页解包时自动改用服务端“同一 ZIP 还原”兜底。
- [x] Dockerfile/Compose 无需改依赖（仅标准库 + 既有 NumPy/Flask），新 JS/Py 都在
      `COPY app ./app` 范围内。

### 7.2 打包功能实测（本沙箱，Python 3.11 / Node 22 / Chromium 131）

1. **`python -m unittest discover -s tests`：43/43 通过**（原 21 + 新增
   `tests/test_package.py` 22）。新增覆盖：高 8 位映射向量、尾块长度、半开相交、
   预算解析、MITM 背包（含与重量 DP 对拍 24 物品）、**ZIP 预测字节==实际字节**、
   ZIP 布局/位宽/帧数、还原 SSE 与服务端一致、**4 块全 16 掩码暴力最优对照**、
   超下限异常、非法块号、完整 HTTP“大包→精确预算重分配→409 保留旧 ZIP 字节”、
   圈选相交强制、无包 404/200-null 语义。
2. **`node tests/package_http_check.mjs`：全部 PASS**。对真实 HTTP 服务下载 ZIP，
   用**线上同一 `zip-restore.js`** 在 Node 解包：15 块/240000 采样、JS 自算 SSE
   `6,303,729,142` 与服务端完全相等、所有 PCM16 块对原 WAV 逐采样精确；标记 #2
   重分配后新 ZIP 该块 16-bit 且逐采样精确；极端 409 后版本/标记 `[2]`/ZIP 字节不变。
3. **真 Chromium + Playwright `tests/package_browser_walk.mjs`：18 项全部 PASS**
   （沙箱无 root，用 `apt-get download` 取 nss/gbm/atk/cups/avahi/X 等 deb 解到
   `/tmp/chromelibs` 并配 `LD_LIBRARY_PATH` 后启动）：真实浏览器内 DEFLATE 还原出
   blob WAV，Chromium 解码 **A/B 时长均 15.00 s**；15 个块行；#5/#6 圈选块常勾禁用；
   blob 下载文件头 `PK` 且 ≤300000；勾 #2 重分配出现 `v2` 且行变 `is16`；勾满可选块
   触发红色“放不下/保留”，勾选回滚为仅 `[2]`、#2 仍 PCM16；预设填值；全程无未捕获错误。
   `tests/package_default_flow.mjs` 的**开箱默认流程** 6 项也全过（不改默认
   600000 与预填两段：一次成功 `ZIP 596,322 / 600,000 B`、blob 还原、刷新后自动恢复）。
4. **原 `tests/browser_walk.mjs` 29 项在最终代码上复跑仍全部 PASS**（新卡片与
   `window.SegmentsPage` 钩子未影响既有圈选/校准/ABX/判分/旧题流程）。
5. `scripts/smoke.py` 扩到 **22 项检查全部 PASS**（新增 10 项打包：建包/全 16 SSE0/
   下载字节一致/ZIP 布局与 CRC/精确预算重分配/409 retained/版本不变），分别在
   Flask dev server 与**与 Docker 同命令的单 worker gunicorn**
   （`python -m gunicorn --workers 1 --threads 8 wsgi:app`）上实跑通过。
6. `pyflakes app wsgi.py tests/test_app.py tests/test_package.py scripts/smoke.py`
   干净；全部前端 JS 与两个 `.mjs` 通过 `node --check`；`docker-compose.yml` 经
   PyYAML 解析合法。

### 7.3 原有 ABX 平台的自检结论（保持有效）

### 已实现（对照原始要求）

- [x] Flask + NumPy；无外部服务、无 CDN，前端纯原生 JS/CSS。
- [x] 上传校验：对齐等长、≤30 s、16 kHz、单声道、PCM16，错误中文可读。
- [x] 圈选 1～6 段两版非静音区间：双波形拖选 + 建议区间 + 精确输入 + 增删/上限。
- [x] 只给 B 一个全片共用正增益；未量化 float64 RMS；每段比值 0.95～1.05。
- [x] 全片 PCM16 边界约束（精确到 −32768/32767），取最接近 1 的可行增益。
- [x] 无解说明原因（静音 / RMS 窗口不相交 / 削波三类，含段号与数值）。
- [x] 校准通过才能开题；服务端对无解开题返回 409 硬拦截。
- [x] 等概率 X=A/B（`secrets.randbelow(2)`），开题时冻结校准音频、X 文件与答案。
- [x] A/B/X 单一 `<audio>`，同位置续听（位置 + 播放状态保持）。
- [x] 提交后才显示答案；未答的 HTML/API/列表均无 `x_is`，X 音频 URL 不透明。
- [x] 改选页可换段开新题；旧题目录快照保证旧音频与首次判分不变。
- [x] Dockerfile + Compose（健康检查、命名卷、端口可覆盖）。
- [x] 一对可试听样音（首页播放器/下载/一键载入）。

### 实际跑过的验证（本沙箱，Python 3.11 / Node 22）

1. **单元/接口测试 `python -m unittest discover -s tests`：21/21 通过**，覆盖
   WAV 往返、立体声/采样率/8-bit/float 拒绝、等长/超长、unity 增益、电平偏移纠正、
   窗口不相交、静音段、削波无解、段数限制、秒/采样键等价、完整 ABX 流程、答案不泄露、
   409 拦截、旧题不变、40 次开题的 X 分布（A 计数落在合理区间）、样音可校准。
2. **真实 gunicorn（`gunicorn wsgi:app`）HTTP 走查**：
   - `/healthz`、首页、静态资源、样音 200；经典 multipart 表单 `POST /upload`
     返回 303；字段别名 `a/b` 可用。
   - 样音 4 个建议段全部检出；前两段校准 `g=0.98131056` 可行；全 4 段脚本自检
     `g=0.964969`，校准后比值全部落在 0.95～1.05。
   - 下载题内 `b_calibrated.wav` 复核：int16 范围 `[-12124,12003]` 无削波，
     重新按**量化后**样本算比值仍 ≈1.05000/1.03110。
   - 不相交电平时返回 `disjoint_rms_windows` 与具体原因，开题 `409`；7 段 `400`。
   - 字节级确认当次 `x.wav` 与 `a.wav` 或 `b_calibrated.wav` 完全相同但 URL 不暴露；
     重复答题保持第一次 `choice=A`。
   - `scripts/smoke.py` 对运行实例 **12 项检查全部 PASS**。
3. **jsdom 交互测试：35 项断言全部 PASS**（真实执行页面 JS + fetch）：
   初始 X、B/A 切换的位置保持与播放恢复、重复点当前键不重载、未选判断时的内联报错、
   提交后答案与服务端一致、段页预填/校准报告/增删/非法时间提示/按钮真实创建题目。
4. **真 Chromium + Playwright 走查：29 项全部 PASS**（在无 root 沙箱里用
   `apt-get download` 取依赖 deb 解到用户目录并配 `LD_LIBRARY_PATH` 后启动）：
   首页、样音跳转、**真实 canvas 鼠标拖选新增第 3 段**、加到 6 段后按钮禁用、
   真 WAV 解码时长 15.00 s、暂停态切换 **5.00s→B 5.00s 精确同位置**、播放态
   A 续播并自然推进（→5.88s）、段循环芯片、提交后答案揭示、改选开 Q2、历史页、
   旧 Q1 仍显示冻结结果与“此前已提交”、全程无未捕获 JS 错误。截图人工检查过
   首页/圈选/ABX/结果/历史（并加装用户态中文字体确认中文与波形、数值表格、
   标签布局正常；修复了段号与 A/B 角标重叠的小视觉问题，段号改为底部紫色数字丸）。
5. `pyflakes app wsgi.py tests scripts samples` 干净；三个前端 JS 与
   `browser_walk.mjs` 均通过 `node --check`；`docker-compose.yml` 经 PyYAML 解析合法。

### 已知限制 / 待办（如实说明）

- **沙箱内没有 docker / compose 可执行文件与守护进程，因此本轮仍没有实际执行
  `docker compose build/up`**；已做的是：`docker-compose.yml` 经 PyYAML 解析合法、
  Dockerfile 未改且新代码都在 `COPY app ./app` 内、无新增第三方依赖，并用与
  Docker `CMD` 相同的单 worker gunicorn 命令在本机实跑了 smoke / Node / 浏览器全套。
  建议在有 Docker 的机器上首跑一次 `docker compose up --build` 做最终确认。
- **打包功能的边界与取舍**：
  - 位深只有 16/8 两档、A/B 同块同位深（按需求）；没有自适应抖动/噪声整形，PCM8
    采用严格“高 8 位 + 128”截断，还原为 `(u8−128)×256`（清单 `manifest.json` 的
    `pcm8_rule` 也写明了该约定）。若评审按 `×257` 等别的 8→16 上采样约定计 SSE，
    逐块误差数值会略有差异，但分块/强制规则/ZIP 上限不受影响。
  - 优化器按**本仓库 zipfile level-9 写出的真实 DEFLATE 字节**精确计费；换 zip 实现、
    压缩级别或清单长度会改变每个 `Δ_i`，需以新实现重新测量（`packaging.py` 已把
    级别/固定清单槽提为常量，并有“预测==实际”测试兜底）。
  - 每个工作区只保留**一份当前包**（`package.zip/package.json`，version 递增）；
    “保留上一份”指失败时不覆盖当前包，不提供更早版本的历史列表/回滚 UI。
  - 网页内解 ZIP 依赖 `DecompressionStream('deflate-raw')`（2023 年后的主流浏览器与
    Node≥18 支持）；不可用时 A/B 自动改走服务端 `/package/restored/{A,B}.wav`，
    但逐块小播放器在该兜底模式下没有独立音源，下载仍为同一份 ZIP。
  - 字节上限 sanity 上限为 200 MB；对 30 s 音频（全 PCM16 ZIP ≈0.7 MB）足够宽松。
- 定位是**单机单录音师工具**：没有登录/多用户隔离，所有题在 `/questions` 全局列出。
  Compose 固定 1 个 worker + 多线程，答题读改写由进程锁 + 原子替换保护；若自行改成
  多 worker，极端并发下“首次判分”需要进一步加文件锁。
- 静音阈值固定为约 −75 dBFS（`SILENCE_RMS_THRESHOLD`，在 `app/wavutil.py`）；
  区间允许重叠；未做零点吸附、频谱图与题目删除 UI。
- 音频响应未实现 HTTP Range：每份 ≤约 1 MB，整文件返回对本地 ABX 切换/拖动足够；
  若放宽时长或上反向代理，可再补 Range/`send_file(conditional=True)`。
- 沙箱基础环境无中文字体，仅影响自动化截图（曾显示方框），已通过用户态安装
  文泉驿微米黑确认真实渲染；终端用户的桌面浏览器通常自带中文字体，不影响功能。
- Playwright/jsdom 工程位于临时目录运行；可复跑的真浏览器脚本已归档到
  `tests/browser_walk.mjs`（ABX 全流程）与 `tests/package_browser_walk.mjs`
  （打包流程，均需自行 `npm i playwright` 并 `npx playwright install chromium`），
  纯 HTTP/解包互操作可直接 `node tests/package_http_check.mjs`（Node≥18）；
  早期 jsdom 脚本未入库。
