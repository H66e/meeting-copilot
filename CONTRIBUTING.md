# 参与贡献

感谢改进 Meeting Copilot。项目优先关注：识别链路的可验证改进、低摩擦交互、双场景提示词、Windows 设备兼容和离线测试。

## 开始

1. Fork 并克隆仓库，创建主题分支。
2. 按 README 创建虚拟环境并安装依赖。
3. 复制 `config.example.yaml` 为 `config.yaml`，密钥只放在环境变量中。
4. 修改代码后运行离线测试；界面改动还要运行 `python ui_split.py --selftest`。

## 提交要求

- 一个 PR 聚焦一个问题，说明动机、改动、测试与已知限制。
- 界面或音频体验变化，请提供操作步骤；可附合成数据截图或短演示。
- 不提交 `config.yaml`、API 密钥、真实会议录音／转写、个人材料、日志或导出纪要。
- 测试使用合成文本、临时目录和模拟网络，不依赖贡献者的私人文件。
- 不为了“修复测试”而删除有效断言；涉及行为变化时同步更新测试和文档。
- 保持 OpenAI 兼容 provider 可配置，避免把某个商业端点或模型写死。

## 测试清单

```bash
python tests/test_audio_asr.py
python tests/test_brain_vision.py
python tests/test_ui_hotkeys.py
python tests/test_scenarios.py
python tools/build_kb.py --dry-run
```

有 Windows 桌面时：

```bash
python ui_split.py --selftest
python main.py --check
```

真实 API、麦克风、音频文件和会议现场测试不是 CI 的隐含前提；如果运行过，请在 PR 中明确标注，且不要粘贴敏感输出。

## 问题报告

请提供 Windows 版本、Python 版本、复现步骤、预期／实际行为和已脱敏的错误日志。不要上传原始录音或完整屏幕截图；先用合成内容确认问题是否可复现。

参与项目即表示同意遵守 [行为准则](CODE_OF_CONDUCT.md)。许可证为 [Apache-2.0](LICENSE)。
