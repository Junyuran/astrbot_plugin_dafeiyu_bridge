# 大肥鱼一键安装（Windows）

给 AstrBot 装上「大肥鱼记忆桥」，并和你已经装好的 NapCat 配对。
装完的效果：**打开 AstrBot，NapCat 自动登录、记忆中枢自动起来**。

> 这个包里**不含 NapCat**。NapCat 的许可证禁止再分发，需要你自己装一次。

## 三步

**1. 先装 NapCat（如果还没有）**

到 <https://github.com/NapNeko/NapCatQQ/releases> 下载 Shell 版一键安装包，装好后**手动登录一次**（要扫码，只这一次）。

**2. 跑安装器**

双击 `install.cmd`。

它会自动：

- 找到你的 AstrBot 实例，装上插件
- 找到 NapCat 目录，读出机器人 QQ 号
- 把插件配置填好（自带中枢、自动拉起 NapCat、快速登录）
- 在桌面建一个「大肥鱼（AstrBot）」快捷方式

想先看它打算干什么，可以跑：

```bat
install.cmd -DryRun
```

**3. 重启 AstrBot，填 API Key**

面板 → 插件 → 大肥鱼记忆桥 → 把 `upstream_api_key` 填上（你自己的模型 key）。
然后在 QQ 私聊发 `桥状态`，看到「记忆中枢在线」就通了。

## 它到底改了哪些东西

| 位置 | 改动 |
|---|---|
| `data/plugins/astrbot_plugin_dafeiyu_bridge/` | 安装/升级插件（旧的会改名备份） |
| `data/config/astrbot_plugin_dafeiyu_bridge_config.json` | 写入配置（已有值会保留） |
| `<NapCat目录>/napcat.quick.bat` | 写入快速登录（原文件备份为 `.bak`） |
| 桌面 | 新建「大肥鱼（AstrBot）」快捷方式 |

不碰 AstrBot 本体、不碰 NapCat 的其他文件、不装任何依赖。

## 常见问题

**装完 QQ 还是要扫码**
说明 NapCat 那边的登录态失效了（改过密码 / 在别的设备登录过）。手动扫一次，之后就能免扫。

**`桥状态` 回「连不上记忆中枢」**
到 AstrBot 日志里搜 `[大肥鱼桥]`，看是端口被占还是启动失败。

**想卸载**
删掉 `data/plugins/astrbot_plugin_dafeiyu_bridge/` 和对应的 `_config.json`，再把 NapCat 的 `napcat.quick.bat.bak` 改回来即可。

## 许可

本安装器与插件为 MIT；NapCat 遵循其自身许可证，本包不含其任何文件。
