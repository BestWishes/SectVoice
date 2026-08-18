# GPT-SoVITS Standard adapter

This package is the accepted Standard engine pinned to the upstream commit in
`package-manifest.json`. It has passed real Windows RTX 2060 SUPER benchmarks,
persisted prompt-cache switching, cancellation, voice similarity, Chinese
listening, and 30-minute continuous generation.

The upstream code stays pinned to the commit in `package-manifest.json`. The
packager applies a Windows fallback from `jieba_fast` to regular `jieba`; this
avoids requiring a global C++ compiler for an optional segmentation speedup.
No engine prompt-cache tensor name is added to SectVoice's common protocols.

The private adapter also paces the existing autoregressive token loop and
streaming-chunk boundaries on Windows. It does not change text, seeds, sampling,
chunk boundaries, vocoder parameters or PCM; Reader explicitly disables pacing
when its audible buffer falls to the low watermark.
