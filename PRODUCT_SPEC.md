# SectVoice Reader 产品总规格

## 1. 当前产品

当前产品是一款完整、独立、可长期使用的 Windows 本地文本朗读器。用户可以导入或粘贴中文小说/文章、选择自己创建的声音、连续朗读、暂停继续、切换声音，并双击任意文字位置立即停止旧声音后从附近继续。

Reader 必须像普通阅读软件，而不是输入文字后等待完整 WAV 的 AI 配音工具。

第一版只有一个 Reader 程序，正式支持两档可选本地语音包：

- Basic：CPU 优先、小、轻、快，中文足够自然，人物声音明显可区分。
- Standard：允许 GPU 和更大资源，人物相似度、中文自然度及表现力明显优于 Basic，但仍须长期实时连续朗读。
- Advanced：只预留枚举和显示位置，第一版不安装、不实现。

当前不开发 Unity、NPC、3D 音频、VoiceScheduler、DLC 或任何游戏代码。只保证 VoiceCore 不依赖 ReaderApp。

## 2. 核心体验

- 粘贴文本、导入 UTF-8 TXT、编辑、自动保存、恢复文档和上次位置。
- 大文本滚动、字体和行距设置、当前 SpeechUnit 高亮及自动跟随。
- 提供可即时切换并持久保存的界面主题；主题只改变显示，不得改变文本、语音生成、缓存身份或播放状态。
- Windows 程序窗口、任务栏、可执行文件、安装器和快捷方式使用同一应用标识与图标。
- 播放、暂停、继续、停止、上一/下一 SpeechUnit、语速、音量。
- 双击任意字符后立刻停止音频、清空旧缓冲、失效旧 Session，并从该位置附近建立新 Session。
- 快速连续双击时只有最后一次有效，旧音频和旧高亮不得重新出现。
- 连续朗读不能因逐句生成产生可听见停顿。
- 缓冲按可播放音频秒数管理，不按句子数量管理，也不提前生成整篇。
- Reader的SpeechUnit与VoiceCore的GenerationWindow必须分离；窗口按预计时长组合多个兼容单元，一个长句可以独占窗口，不能使用固定二句或三句策略。

## 3. Voice Library

- 每个声音有永久稳定的 VoiceId；名称变化不改变 VoiceId。
- 声音身份层保存原始音频、规范化参考片段、转写、语言、默认设置和长期元数据。
- 每个 Tier 对应一个引擎私有 EnginePayload。Payload 只通过不透明引用、版本、校验和及状态进入通用协议。
- 同一 VoiceId 可以拥有 Basic 和 Standard Payload；不得创建“基础老掌柜”和“中级老掌柜”两个身份。
- 缺少当前 Tier Payload 时明确提示并允许立即或批量编译，禁止静默重新分析全部声音。
- 支持新建、重命名、删除、试听、重新编译、状态查看、可靠备份及 `.voicepkg` 导入/导出。
- Reader Core 提供两个来源、许可证、VoiceId和哈希均固定的体验声音。首次启动只导入一次；已有同VoiceId档案不覆盖，用户删除后不得在后续启动中强制恢复。

## 4. VoicePackage

声音包是可迁移文件资产，不把大型内容塞进 SQLite Blob。概念结构：

```text
VOICE_xxx.voicepkg
├─ manifest.json
├─ source/
├─ reference/
├─ transcript/
├─ previews/
└─ payloads/<tier>/<engine-id>/<payload-version>/
```

所有文件具有哈希。EnginePayload 内部字段保持引擎私有；通用层只记录引擎ID、引擎版本、Payload格式版本、状态、路径、哈希和创建时间。

## 5. 引擎与模型包

- Basic 和 Standard 是两个可独立安装、更新、卸载和启用的 Engine/Model Package。
- Reader安装包不强制捆绑两套模型。
- 用户界面显示“基础/中级/高级”，普通使用不暴露技术模型名。
- 高级信息页显示实际引擎、版本、模型大小、能力、本机Benchmark和资源数据。
- 同档切换 VoiceProfile 只切 Payload，不重载基础模型。
- 跨档切换可以加载另一引擎，但必须明确显示进度，并从当前位置重建 Session。
- 每个引擎报告 Capabilities；不支持的情绪或风格设置必须在UI中禁用并说明。

## 6. 声音创建

- 自动转写后必须由用户修订；程序根据最终参考转写记录参考语种，不得把英文参考静默标成中文。
- 非中文参考用于Basic中文朗读时必须显示跨语种质量提示。提示不能冒充自动修复；情绪、录音距离和语言差异明显的参考应建议换用同人物平稳中文片段或Standard。
- 支持 WAV、MP3、FLAC、M4A。
- 波形、试听、选段、裁剪、质量检查、格式统一、轻度处理、ASR、人工修订和测试朗读。
- VoiceCompiler 是独立生命周期，可以较慢或使用更多资源；VoiceRuntime 才受严格日常资源与延迟约束。
- 创建一个 VoiceProfile 后，可为当前已安装的一个或多个 Tier 顺序编译 Payload。
- 某个 Tier 编译失败不破坏 VoiceProfile 或其它成功 Payload。

## 7. 连续音频与定位协议

- VoiceRuntime 输出 AudioChunk；完整 WAV 仅是缓存或导出格式，不是唯一播放协议。
- AudioChunk 包含 SessionId、GenerationId、Sequence、SpeechUnitId、PCM格式、时长、数据、IsFinal和可选 TimingInfo。
- AudioChunk 不得包含 Reader 原文 TextStart/TextEnd。
- TimingInfo 只能表达 SpeechUnit 内部相对项目与时间，不携带文档绝对字符位置。
- Reader持有 `SpeechUnitId -> 文档字符范围` 的映射，并负责高亮。
- 缓冲低水位约0.5～1秒、目标约1～2秒、高水位约2～3秒；最终参数由Benchmark和长期试听调整。

## 8. 缓存

缓存属于 VoiceCore，可跨文档复用。缓存键至少覆盖 Text、VoiceId、Tier、EngineId、EngineVersion、PayloadVersion、语言、风格、情绪、发音、合成格式和后处理版本。

音量属于播放参数，不进入合成缓存键。语速若由播放时保持音高伸缩实现，可复用基础速度缓存；若由引擎原生韵律控制，则进入缓存键。

## 9. 第一版验收

第一版必须真实完成：

1. Windows正常启动，一个Reader程序管理两档引擎。
2. Basic和Standard可独立安装、启用、更新/卸载。
3. 同一个VoiceProfile具有Basic和Standard Payload。
4. 两档都能使用该声音朗读未生成过的新中文文本。
5. 大篇中文小说连续朗读至少30分钟，无明显计算停顿。
6. 当前SpeechUnit高亮和自动滚动正确。
7. 暂停、继续、停止、上一/下一、语速和音量真实生效。
8. 双击任意位置立即停止旧音频并快速重新出声。
9. 快速连续双击只有最终位置有效。
10. 同档切人物不重载模型；跨档切换行为清楚可靠。
11. 声音创建、永久VoiceId、Payload编译、导入/导出和恢复真实可用。
12. 缓存、错误处理、崩溃恢复和长时间资源稳定。
13. 自动化测试全部通过，最终验收不使用Mock声音。
