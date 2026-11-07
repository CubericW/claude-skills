# claude-skills

一组给 [Claude Code](https://docs.anthropic.com/en/docs/claude-code)（以及其他支持 `SKILL.md`
的 agent，如 Codex）用的 skill，都在 macOS 上日常使用中打磨过。

*A collection of agent skills for Claude Code / Codex, used day to day on macOS.*

| Skill | 做什么 | 依赖 |
|---|---|---|
| [video-digest](skills/video-digest) | 本地版 BibiGPT：B站 / YouTube / 抖音 / 小红书 / 播客 / 本地文件 → 字幕或 Whisper 转写 + 关键帧 OCR → 带时间戳的结构化总结 | `uv`、`yt-dlp`、`ffmpeg` |
| [handwriting-svg](skills/handwriting-svg) | 手写照片 / 扫描件 → 去背景、去横线的透明矢量 SVG（整页 / 逐行 / 逐字），可按文字重新拼接 | `uv`、`potrace`（Homebrew）|
| [info-gap-filler](skills/info-gap-filler) | 跨国内外社交媒体采集经验与口碑 → 可靠性评估 → 交叉验证 → 带来源的调研报告 | [mediacrawler-mcp](https://github.com/CubericW/mediacrawler-mcp)、[overseas-social-mcp](https://github.com/CubericW/overseas-social-mcp) |
| [telegram-access](skills/telegram-access) | 让 agent 知道自己能读公开 Telegram 频道/群，并规范读取方式 | [overseas-social-mcp](https://github.com/CubericW/overseas-social-mcp)（Telethon 后端）|
| [obsidian](skills/obsidian) | 通过官方 Obsidian CLI 完整操作笔记库，附 vault 管理和"移动不卡确认框"两个脚本 | Obsidian（在设置里启用命令行界面）|

## 安装

把需要的 skill 目录复制（或软链接）到 `~/.claude/skills/`：

```bash
git clone https://github.com/CubericW/claude-skills
cp -R claude-skills/skills/video-digest ~/.claude/skills/
```

部分 skill 有额外的安装步骤，见各自的 `SKILL.md` / `README.md`：

- **handwriting-svg**：`bash ~/.claude/skills/handwriting-svg/setup.sh`（可选 `--with-waifu2x`）
- **obsidian**：`install -m 755 skills/obsidian/bin/* ~/.local/bin/`
- **video-digest**：可选配置 `~/.config/video-digest/config.json`，参考 `config.example.json`

## 致谢

这些 skill 自己的代码按 MIT 发布；它们调用的工具各有许可，均需自行安装，不随本仓库分发：
[yt-dlp](https://github.com/yt-dlp/yt-dlp)、[mlx-whisper](https://github.com/ml-explore/mlx-examples) / [faster-whisper](https://github.com/SYSTRAN/faster-whisper)、
Apple Vision（macOS OCR）/ [RapidOCR](https://github.com/RapidAI/RapidOCR)、[OpenCV](https://github.com/opencv/opencv)、
[Potrace](https://potrace.sourceforge.net/)（GPL）、[waifu2x-ncnn-vulkan](https://github.com/nihui/waifu2x-ncnn-vulkan)（MIT，可选）、
Obsidian CLI。video-digest 的功能设计参考了 [BibiGPT](https://bibigpt.co)。

## License

MIT — see [LICENSE](LICENSE).
