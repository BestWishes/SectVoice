# SectVoice 架构设计

## 1. 依赖方向

```text
ReaderApp -> VoiceCore -> IVoiceEngine -> Engine Package
```

VoiceCore禁止调用Reader UI。引擎包禁止直接操作Reader文档、字符位置、Qt控件或SQLite业务表。

## 2. 模块

### ReaderApp

- Document、SpeechUnit映射和编辑保存。
- ReaderUI、ReaderController、高亮和自动滚动。
- ThemeManager统一管理QPalette、全局样式和主题持久化；业务窗口不维护私有配色，主题不得进入VoiceCore。
- Branding在QApplication创建时统一设置Windows AppUserModelID和应用图标，发行配置复用同一多尺寸ICO。
- Windows音频输出适配器。
- `SpeechUnitId -> [start_char,end_char)` 映射。

### VoiceCore

- VoiceLibrary、VoiceProfile、VoicePackage。
- VoiceCompiler编排和Payload状态。
- VoiceRuntime、VoiceSession、EngineRegistry。
- StreamingAudioBuffer和按秒水位控制。
- VoiceCache、ModelPackageManager、能力与健康检查。

### Engine Package

- 独立运行环境、模型、Compiler和Runtime适配器。
- 私有Payload结构。
- Warmup、Unload、Compile、Start、Cancel和Health。
- 引擎故障不得带崩Reader。

## 3. 通用协议

### AudioChunk

通用字段：

- session_id
- generation_id
- sequence
- speech_unit_id
- pcm_format（sample_rate/channels/sample_format）
- duration_seconds
- payload bytes或受控共享内存引用
- is_final
- timing_info（可选，仅SpeechUnit内部相对时间）

禁止字段：Reader原文绝对字符位置、TextStart、TextEnd、Qt对象、具体引擎Prompt字段。

### EnginePayloadRef

通用层只保存：

- voice_id
- tier
- engine_id
- engine_version
- payload_format_version
- status
- opaque_path
- sha256
- created_at/updated_at/last_error

Payload目录内文件由具体引擎解释。

### EngineCapabilities

- streaming_audio
- cancellable_generation
- native_speed
- emotion
- style
- language set
- preferred_pcm_format
- min/max reference duration
- CPU/GPU要求

## 4. Session与取消

- 每次播放/跳转/切声音/切Tier建立新SessionId并递增GenerationId。
- Reader先停止AudioSink，再失效Session，再清空Buffer。
- 所有Chunk入队前同时校验SessionId、GenerationId和SpeechUnitId。
- 取消优先使用引擎合作式取消；只有崩溃、超时或不可恢复卡死才重启进程。
- 旧Session结果可以作为内容缓存提交，但永不进入新播放队列。

## 5. 缓冲

- Buffer记录已提交、已消费和剩余PCM帧，统一换算为秒。
- Reader的定位边界是稳定的完整`SpeechUnit`，VoiceCore的计算边界是按预计可听时长动态建立的`GenerationWindow`；不能再按固定句数或逐句单独生成。
- 首窗目标约8秒，后续窗目标8～15秒；一个长句可独占窗口，连续短句可进入同一窗口。正常目标水位约6秒，高水位约18秒，播放和后续窗口推理并行，不生成整篇WAV。
- 引擎可以流式返回PCM，但Runtime必须收齐整个窗口，完成一次全窗响度、边界映射和缓存后，才按SpeechUnitId切成传输微块入队。引擎内部流不能造成半句开播。
- 标点和段落停顿按窗口内部边界重建；只消耗确认的低能量静音，不裁有声帧，再追加Reader配置的精确停顿。
- 默认语速1.0不做自动逐句伸缩。Basic的用户变速对整个窗口只运行一次Rubber Band；Standard使用经实测校准的原生速度。音量只在播放端生效。
- Standard可在私有引擎的自回归token和既有流式块边界之间做可取消的计算让步，以降低功耗和风扇突然爬升；让步不得改动文本、随机种子、采样、窗口、Vocoder或PCM。可听缓冲达到低水位时必须自动恢复全速，优先保证连续播放。
- Basic包可声明独立工作进程RSS保护线。只有完整窗口已经入缓冲、没有活跃推理时才释放扩张的ONNX进程；已缓冲PCM照常播放，下一个冷窗口按需重载。
- 后台预生成失败不会清空已经完成的连续音频；AudioOutput先排空有效队列，抵达失败边界后再进入Error。工作进程同时把结构化上下文和traceback持久化到本地日志。

## 6. 模型包

安装根目录下的包目录：

```text
<InstallRoot>\runtime\engines\<tier>\<engine-id>\<version>
<InstallRoot>\models\<tier>\<engine-id>\<version>
```

模型安装采用下载临时目录、哈希校验、真实smoke生成、原子激活。卸载不删除VoiceProfile和原始参考音频。

## 7. 历史实现迁移原则

保留文档、分块、位置映射、Session准入、媒体处理、ASR、缓存安全和UI框架；不恢复已淘汰的单一引擎请求、整篇WAV协议、逐句孤立缓冲或把声音档案绑定到单一引擎的实现。

## 8. v12 连续朗读重构（已验收架构）

### 8.1 Reader单元与生成窗口分离

- `SpeechUnit`仍是Reader原文映射、角色分配、双击定位和高亮的最小稳定单元；不得为了模型推理修改、合并或丢失它的原文范围。
- `GenerationWindow`是VoiceCore临时建立的推理与后处理单元，不写回Reader原文，也不进入通用AudioChunk协议。
- 窗口不按固定句数建立，而按预计可听总时长动态累加：多个极短SpeechUnit持续加入，直到接近目标时长；单个极长SpeechUnit可以独占一个窗口。
- 首窗默认目标约10秒（允许约6～15秒），让多组连续短句能保持在同一个声学上下文；后续窗目标约12秒（允许约8～15秒）。同时遵守引擎的字符、token、内存和单请求硬上限。目标值由实测调节，不能退化为“固定三句”。
- 预计时长使用当前声音/档次的近期有效语速EMA；没有历史时使用保守中文基线，并计入标点和段落停顿。
- 角色、VoiceId、Tier、EngineId、Payload版本、参考语言或会真实影响合成的设置变化时必须断窗，避免一个引擎请求混入不兼容声音。
- 若单个SpeechUnit超过引擎硬上限，只能在该SpeechUnit内部按自然标点私有拆分；Reader侧仍只有原SpeechUnitId，AudioChunk不出现Reader字符范围。

### 8.2 整窗生成与一次后处理

- 一个GenerationWindow优先只向引擎提交一次连续文本，让模型在同一上下文中决定相邻短句的声线、节奏和语气，避免每句重新起音造成音色、距离和速度漂移。
- 窗口PCM全部完成后只进行一次全局LUFS校准；建立内部帧范围后，允许对客观近/远漂移做不超过±2.5dB的整段常量增益校准。它不做第二次逐句LUFS、压缩、句首塑形、淡入淡出或保护静音，并保持每个范围内部动态不变。
- 默认语速1.0不做自动逐句时间伸缩。用户主动选择非1.0语速且引擎没有可靠原生控制时，只对整个窗口做一次保持音高和共振峰的高质量时间伸缩。
- Reader配置的标点/段落停顿由窗口内部边界重建，不能叠加模型随机生成的长空白。边界算法只裁取已确认的安静间隔，不删除有声字词。
- 句首句尾保护只应用于真正的播放会话/窗口外边界；窗口内部SpeechUnit边界不得强行插入每句50ms数字静音或逐句淡入淡出。
- 声明`window_boundary_timing=asr`的Basic和Standard包，在冷生成的多单元窗口中使用独立Faster-Whisper常驻工作进程取得词级时间戳，再将ASR字符与Reader原文进行容错对齐。同音字可作为时间锚点，但Reader原文始终是内容权威；对齐不完整先换生成路径自动重试，仍不确定则按完整SpeechUnit递归拆窗。拆到单个完整SpeechUnit后跳过ASR拒绝、按原文直接生成缓存和播放。ASR是边界与诊断工具，不能成为文字内容的播放许可；单个完整SpeechUnit不存在内部句界，不为了核对而阻塞播放。

### 8.3 窗口内部定位与播放

- VoiceCore为每个完成窗口建立 `SpeechUnitId -> [start_frame,end_frame)` 的内部PCM映射；该映射只描述合成结果，不包含Reader的TextStart/TextEnd。
- Runtime按内部映射输出仍带原SpeechUnitId的AudioChunk，使Reader高亮、上一句/下一句和自动滚动继续以SpeechUnit为单位。
- TimingInfo如存在，只能记录SpeechUnit内部的相对时间；窗口边界映射不能提升成Reader绝对字符位置。
- 双击先停AudioSink、失效旧Session/Generation、清空旧Buffer，再从命中SpeechUnit建立新首窗。旧窗口即使随后生成完成也只能进入内容缓存，绝不能进入新会话队列。
- 快速连续跳转必须只允许最终Session的窗口映射、音频和高亮通过准入门。

### 8.4 窗口缓存

- 缓存对象改为完整GenerationWindow的基础速度PCM和内部单元帧范围，不再把独立逐句后处理结果作为连续朗读的首选缓存。
- 运行时必须先规划目标GenerationWindow，再查窗口缓存；命中的缓存片段不得短于计划窗口，避免多个较短缓存重新制造多个声学起点。跳入一个更大缓存窗口中部时可以只复用目标后缀。
- 缓存身份必须包含有序的完整SpeechUnit文本列表及边界、VoiceId、Tier、Engine/Payload版本、参考语言、真实合成设置、PCM格式和后处理版本；简单字符串拼接不能作为边界身份。
- 缓存元数据保存相对单元序号/内容指纹和PCM帧范围，不保存文档字符位置，也不依赖某个文档临时SpeechUnitId，才能安全跨文档复用。
- 用户音量仍是播放参数；用户语速若为统一后处理，可复用基础速度窗口缓存并整窗变速。

### 8.5 v12禁止回退项

- 禁止固定生成二句、三句或任意固定句数。
- 禁止窗口内逐句独立归一化、逐句自动字速修正、逐句50ms保护静音和逐句淡入淡出。
- 禁止让模型内部小请求边界变成Reader高亮边界。
- 禁止以修复节奏为由删除、改写、重排Reader原文，或把Reader字符位置写进AudioChunk。
- 禁止只修改组窗算法却保留旧逐句生成、逐句缓存和逐句播放拼接链路。

### 8.6 完成验收

- 单元测试覆盖连续极短句、单个极长句、长短混合、引擎硬上限、角色/声音断窗和预计时长收敛。
- PCM测试覆盖窗口内部边界回映、整窗响度、整窗变速、配置停顿、无重复/漏读以及无每句强制边缘处理。
- Basic和Standard使用真实VoicePayload复测用户现场四句、历史问题句、快速连续双击、多角色和连续长篇；两档都必须严格按SpeechUnit顺序恰好一次且无旧会话回插。
- 最终重新完成30分钟中文小说连续朗读、资源峰值、下溢、错误和缓存热启动记录，才能把本节从“实施基线”更新为“已验收架构”。

本节已于2026-08-11满足上述门槛：自动化101 passed；Basic与Standard最终全新文本30分钟分别1800.01秒/188单元和1800.06秒/154单元，均0下溢、0错误。完整证据路径见`VALIDATION_REPORT.md`。
