# V2 模块与控制边界

## 单向依赖

```text
CLI / HTTP
    → Service：外层自动化、执行控制、状态记录
        → CatalogSync / AssignmentSync / Replay / LiveMonitor：具体业务流程
            → Auth / Canvas / Video / Media / Recognizer / Slides：独立能力
```

`bootstrap` 仅组装具体实现和配置。不将它或 Service 传给能力模块。数据库 Store 只被应用层调用；`types` 只有数据类型及异常。测试通过 AST 检查能力模块不能反向导入流程、数据库或服务。

## 独立调用约定

- `Auth.session(interactive=False)` 返回独立 Canvas Session；`Auth.video(course_id)` 返回带独立 Session 的课程域令牌。调用方负责关闭。鉴权模块不查询课程或视频。
- `Canvas(session_factory)` 提供课程与作业分页查询。`Video(credential)` 提供教学班映射、课次查询和媒体来源解析，不自动登录或保存结果。
- `MediaSource(location, headers, view)` 表示地址或本地文件。地址与请求头不参与 repr；业务只持久化课次 ID，使用前重新解析地址。
- `media.audio(source)` 产生 PCM `AudioChunk`；`media.sample_frames(source, folder)` 产生等间隔采样画面。二者对 Canvas 无任何依赖。
- `Recognizer.transcribe(AudioChunk)` 返回 `TranscriptSegment`。模型实例持有自己的锁，延迟加载 Qwen3-ASR；推理配置是构造参数。
- `slides.extract(frames_dir, output_dir, sample_every, thresholds)` 处理采样画面并返回页面元数据。V1 的感知哈希、灰度、边缘、墨迹和网格差异算法保留，阈值通过参数传入，没有会话、视频客户端、队列或课程状态。
- `assignments.export(session, assignment, folder)` 提取正文并下载附件；每个附件独立保存状态和 SHA-256，失败可重试，成功附件不重复下载。

## 三种容易混淆的执行概念

1. Python `asyncio.Task`：让一个协程持续运行，是运行语言提供的工具。
2. 数据库 execution：记录某个具体处理动作的进度、错误、重试时间及产物，用于重启恢复和防重。
3. V1 的 JobRegistry/Plugin：动态注册、继承任务类型、传递整个 Gateway 的扩展框架。V2 没有这一层。

V2 使用固定的同步、作业、回放 ASR、回放 Slides、直播流程；Web API 另外提供显式的本地 ASR / Slides 和回放试跑入口。Service 显式调用这些流程，没有动态函数导入、注册回调表、任务类继承或插件生命周期。

## 直播控制

每个正在监听的课次有一个父协程、一个音频生产协程和一个识别消费协程。父协程负责清理与停止，生产协程负责连接与重连，消费协程调用独立 ASR 并写入转写。队列上限默认 100 个音频块，超载丢最老块并写入缺口事件。

推理仲裁位于应用层，直播优先，回放在块之间让出。模型不认识优先级或课次。直播控制也不直接登录：它调用注入的 `resolve_sources(lecture)`，由组合层连接鉴权与视频客户端。

处理错误不打印可能含签名地址的异常字符串。HTTP/执行记录记录异常类型；会话失效进入 needs_login，无映射的课程标为 video_unavailable。网络/媒体暂时失败最多额外重试三次，间隔 5、15、60 秒。

## 持久化与恢复

SQLite WAL 和事务领取保证同一动作只有一个执行者。唯一键为动作类型、Canvas 课程 ID、课次 ID；三个处理动作互不占用同一状态。服务 runtime 文件锁避免多进程同时恢复同一数据库。

回放音频逐块保存实际起止时间，不将不足一块的结尾补成固定长度。重试从最后完整 JSONL 记录继续，丢弃损坏尾行。Slides 保留旧成功结果，生成完新的图片与清单后再切换结果目录。手动取消保持终态，服务退出恢复待处理；CLI 的短片段测试使用 samples 目录，不污染完整处理记录。

可继续改进的点：直播实时环境验证、未来课表覆盖范围、长时运行性能数据。它们不要求 ASR 或 Slides 依赖调度器。


## 独立 WebUI

`webui/` 单独安装和构建，只通过 REST / SSE 访问 Python HTTP 适配层。页面按路由拆分；后端不托管前端、不依赖 Node。网页登录挑战、配置生效管理和进度投影分别位于 `browser_auth.py`、`configuration.py`、`progress.py`，均不反向注入底层处理模块。

本地文件、回放试跑和完整回放的 ASR 共享串行通道，Slides 同理；直播保持独立监听。调度器的启动和暂停由 Service 显式处理。新增入口仍使用直接分支调用，没有引入 Job 注册框架。

## 课堂检测与事件投影

- `qr.py` 是独立图像分析叶模块：输入图片目录和带时间的图片清单，输出二维码候选、解码结果、位置、时间及逐图错误；不导入 Slides、课程、数据库、HTTP 或认证模块。复用 Slides 环境的 OpenCV，无外部识别服务。
- `flows.slides_source` 在代表画面抽取后调用二维码模块，将 `qr.json` 与 `slides.json` 一起原子发布。单图错误标记 partial，整体错误标记 failed；二维码分析失败不丢弃 Slides。取消仍遵循 blocking 的线程清理约定。
- `classroom_events.py` 只读取已发布的回放二维码报告与直播关键词事件，将其投影为统一记录，不进行检测或网络操作。Web API 只负责调用投影；WebUI 每 10 秒刷新。
- 当前二维码覆盖范围为保存的代表画面，并非每一个视频帧；时间是对应画面的取样时间，不承诺精确开始 / 结束时间。多个画面或多个码分别记载，仅成功解码后生成确认记录；失败候选经过透视校正、放大与二值化重试，仍失败则不作为二维码事件。直播视频 QR 检测、外部通知和签到提交未接入。

## 直播自动签到

`attendance.py` 负责白名单二维码解析、移动课堂 OAuth、最新结果调度与签到响应判定；`live_attendance.py` 负责独立直播画面观察。二者不依赖 ASR 或回放处理队列。`media.live_frames` 输出 2 Hz JPEG；`QRDetector.detect_bytes` 复用现有二维码解码。视频优先请求 HTTP-FLV（官方 playType=2），连接失败切换 HLS（playType=3，最新分片）。

每个画面源仅保留最新帧。新帧到达立即作废旧解码候选；解码期间若出现更新帧，旧结果丢弃。OAuth 预热独立进行，派发前完成认证后重新选码；本地接收至发送超过 2 秒的不发送。这个预算不包括上游摄像机、编码器及直播服务器延迟，无法保证二维码在学校端仍有效。

按课程和签到记录去重，换码不重复提交已经确认的签到。过期 / 拒绝的结果仅遇到新 token 才重试；请求中断或结果不确定停止重发；服务重启把未确认请求标为需人工核对。先提交扫码请求，不采集或上传定位。成功响应中的定位提示只附加提醒，不覆盖成功状态；学校明确拒绝或返回额外验证状态时保留需人工核对状态。仅移动课堂签到白名单 URL 可以触发，课程编号必须与正在观察的直播一致。原始二维码 token 与 OAuth token 不写入签到状态；证据图片作为私有课程产物保存。

首次识别到每次签到事件后等待 3 秒再提交；同一次签到的轮换码不重置等待时间。等待结束后重新选择最新画面，仍要求本地帧龄不超过 2 秒；二维码消失、过期或自动签到关闭时不提交。

配置及结果通过独立 `/api/attendance` GET / PATCH 投影到「点名与二维码」页面。模块开关、课程范围持久化在 control/attendance；全局暂停同时暂停观察，关闭后不再发出新请求，已发出的网络请求可能完成。回放和历史 QR JSON 不进入执行输入。移动课堂 token 仅保留于进程内存，沿用已配置的 jAccount 自动认证。
