# 插画与字体

`assets/whale-girl.png` 是本项目通过 Codex 内置图像生成工具新生成的透明 RGBA 插画，生成于 2026-10-26。未直接打包网上参考图。鲸鱼娘是装饰元素，机器人名称由配置独立决定，不代表 DeepSeek 官方产品或官方角色设定。

## 检索与参考

- [鲸鱼娘开源资产库](https://treapgogo.github.io/deepseek-whale-girl/)：查看 Q 版参考图、角色描述与提示词。参考了蓝色渐变长发、鲸鳍耳、鲸尾、蓝白女仆服等社区角色特征。
- [社区源仓库](https://github.com/TreapGoGo/deepseek-whale-girl)：参考来源和作品许可各自独立，项目代码许可不自动覆盖角色图片。
- [本次查看的社区参考图](https://treapgogo.github.io/deepseek-whale-girl/assets/dafeiyu-chibi-character-sheet.png)：仅用于造型研究，未作为生成接口的图像输入，也未收录进安装包。

生成提示词不随发布包分发。插画为透明背景，采用蓝白女仆服、鲸尾和手持指南的造型。

## 字体

- 字族：Noto Sans SC，来源 [Noto CJK 官方仓库](https://github.com/notofonts/noto-cjk)。
- 通过 [pylopdf-fonts-zh-cn 0.1.1](https://pypi.org/project/pylopdf-fonts-zh-cn/0.1.1/) 安装，由该包提供字体文件及 SIL Open Font License 1.1 许可。插件不再重新分发字体和独立的 OFL.txt。
- 使用字体包公开的 `sans_path()` 定位 Noto Sans SC，无需操作系统安装字体。字体 SHA256 为 `faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9`，与原插件字体逐字节一致。
