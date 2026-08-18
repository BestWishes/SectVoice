# SectVoice 第一版项目规格索引

本文件用于满足跨工具和跨会话都容易找到的`PROJECT_SPEC.md`入口。完整且具有最高优先级的产品规格见[PRODUCT_SPEC.md](PRODUCT_SPEC.md)，架构见[ARCHITECTURE.md](ARCHITECTURE.md)。

第一版不可变结论：

- 只有一个Windows Reader；Basic和Standard是两个可独立安装、卸载、恢复和升级的Engine/Model Package。
- 声音创建与日常朗读分离。永久VoiceId可同时持有两档引擎私有Payload；朗读新文字不重新分析参考录音。
- Reader按SpeechUnit建立原文位置映射；AudioChunk只有SpeechUnitId，不含TextStart/TextEnd。
- EnginePayload内部保持引擎私有，通用协议不包含任何MOSS、GPT-SoVITS或其它引擎专用字段。
- 连续朗读保留完整SpeechUnit作为Reader定位边界，但VoiceCore按预计可听时长动态组合GenerationWindow；连续短句可合并、极长句可独占，禁止固定句数。
- 整个GenerationWindow完成一次推理、一次全局响度和内部帧映射后才进入Reader播放队列；引擎PCM流不能直接造成半句开播或句中等待。
- 双击先停声卡、失效旧Session、清空旧队列，再从最终双击位置附近继续；旧Chunk不能回插。
- Basic和Standard都必须经真实声音、真实声卡、真实取消、崩溃恢复和30分钟连续朗读验证，不用Mock完成最终验收。

当前实现与验收状态见[DEVELOPMENT_PROGRESS.md](DEVELOPMENT_PROGRESS.md)和[VALIDATION_REPORT.md](VALIDATION_REPORT.md)。
