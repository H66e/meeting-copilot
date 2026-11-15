Meeting Copilot v0.2.1 · Windows 免安装预览版
===========================================

面向研究组会与经允许的面试练习。请在取得参与者同意、遵守机构规定和面试规则的前提下使用。

一、运行条件
------------
- Windows 10／11，64 位系统。
- 不需要安装 Python；请完整解压 ZIP 后使用。
- 本地语音转写需要可正常播放声音的输出设备。
- 云端应答与自由提问需要 OpenAI 兼容 API；没有 API 密钥时仍可打开界面、准备本地模型。
- Whisper 模型不随 ZIP 分发。首次使用默认从 Hugging Face 下载，下载后优先使用本机缓存。

二、启动
--------
1. 下载 MeetingCopilot-v0.2.1-Windows-x64.zip。
2. 完整解压到你有写入权限的目录，例如“文档\MeetingCopilot”。
3. 保持 MeetingCopilot.exe 与 _internal 文件夹在一起，不要只复制 exe，也不要直接在 ZIP 内运行。
4. 双击 MeetingCopilot.exe。
5. 在“设置”中选择组会／面试、输出设备和识别模型，然后点击“开始聆听”。

程序使用 WASAPI 回环采集电脑正在播放的会议声音，不要求会议参与者使用你的麦克风。请确认实际播放设备与设置中的输出设备一致。

三、配置与隐私
--------------
- 程序首次使用公共模板 config.example.yaml。需要固定修改端点或模型时，将它复制为同目录下的 config.yaml 再编辑。
- 推荐在 Windows 用户环境变量中设置 OPENAI_API_KEY、DEEPSEEK_API_KEY，然后重新启动程序。不要把真实密钥提交到仓库或发给他人。
- 数据、日志、导出纪要和窗口状态保存在 exe 所在目录下的 data／logs／exports／records；首次运行自动创建。
- 默认不保存整场原始音频。本地转写不会把音频发送给 LLM，但云端回答会发送所需文本，云端读屏会发送截图。
- 首次下载只获取 Whisper 模型，不上传会议音频。将 asr.allow_download 改为 false，或设置 MC_OFFLINE=1，可禁止模型联网下载。
- 打包版可读取已有的 data\kb 索引；从原始材料重建索引目前请使用源码版的 tools/build_kb.py。

四、离线启动自验
----------------
在程序目录打开 PowerShell，运行：

.\MeetingCopilot.exe --smoke-test

该命令真实打开窗口并自动关闭，不启动音频采集、Whisper 下载、API 请求或全局热键。返回码为 0 表示通过，详细结果见 logs\assistent.log。

五、校验与安全提示
------------------
发布页同时提供 .zip.sha256 文件。PowerShell 可运行：

Get-FileHash .\MeetingCopilot-v0.2.1-Windows-x64.zip -Algorithm SHA256

输出应与校验文件中的 SHA256 一致。

当前版本未配置商业代码签名，Windows 可能提示未知发布者。请只从项目 GitHub Releases 下载并核对来源与哈希；无法确认来源时不要运行，也不需要关闭安全软件。

这是实验性预览版。语音识别与模型回答可能不准确，重要数字、经历和结论请自行核实；尚未覆盖所有电脑、会议软件及真实会议环境。

六、从源码构建
--------------
需要 Windows、Python 3.14、requirements.txt 中的依赖和 PyInstaller：

python -m pip install -r requirements.txt pyinstaller
python tools/build_windows.py

脚本会构建 onedir 文件夹、复制公共说明、执行离线启动自验，再生成 ZIP 与 SHA256。它不会把本机 config.yaml、密钥、会议材料、日志、导出或模型缓存打入发布包。

许可证：Apache-2.0，详见 LICENSE.txt。
