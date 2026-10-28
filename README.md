# 大肥鱼记忆桥

给 AstrBot 一块**独立于平台**的长期记忆。同一个角色在 QQ、桌面宠物、微信之间，记住同一件事。

## 开箱即用

插件**自带记忆中枢**（纯标准库，没有第三方依赖），装上就能用：

1. 从插件市场安装（或把目录放进 `data/plugins/`）
2. 面板里给「大肥鱼记忆桥」填上 `upstream_api_key`（你平时用的模型 key 就行）
3. 重载插件
4. QQ 私聊发 `桥状态` → 看到「记忆中枢在线（自带中枢）」就通了

之后它会自己：回复前检索记忆拼进 prompt、回复后把这一轮压成记忆存起来。

## 它解决什么问题

AstrBot 自带的记忆是**跟着这个 bot 走**的。如果你还有一个桌面宠物、或者以后想接微信，
它们的记忆天然分裂：QQ 上说过的事，桌宠不知道；桌宠答应过的事，QQ 上也不认。

这个插件把「记忆」从「平台」里拆出来：

```
QQ ──┐
桌宠 ─┼──→ 记忆中枢（插件自带，本地 HTTP）──→ 大模型
微信 ─┘        │
            brain.db（唯一真相）
```

中枢同时是 **OpenAI 兼容接口**。把桌面宠物的 `base_url` 改成 `http://127.0.0.1:8787`，
它就和你 QQ 上的角色共享同一份记忆——桌宠那边一行代码都不用改。

## 一键安装（Windows，省去手动配置）

在 [Releases](https://github.com/Junyuran/astrbot_plugin_dafeiyu_bridge/releases) 下载
`dafeiyu-oneclick-*.zip`，解压后双击 `install.cmd`。它会：

- 找到你的 AstrBot，装上本插件
- 找到你已装好的 NapCat，读出机器人 QQ 号
- 配好快速登录（免扫码）、自带中枢、自动拉起 NapCat
- 在桌面建一个「大肥鱼（AstrBot）」快捷方式

它**不包含 NapCat 本体**（NapCat 的许可证禁止再分发），需要你自己先装一次。

> 仓库里的 `installer/` 目录就是它的源码，也可以直接双击里面的 `install.cmd` 运行。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `embedded` | 开 | 自己起中枢；端口已有服务就直接用那个 |
| `brain_url` | `http://127.0.0.1:8787` | 中枢地址，桌宠也连这个 |
| `upstream_base_url` | `https://api.deepseek.com` | 抽取记忆用的模型接口 |
| `upstream_model` | `deepseek-chat` | 用便宜的小模型就够 |
| `upstream_api_key` | 空 | 不填则只注入不记录 |
| `person_id` | `owner` | 主人的人格 ID，各端要一致 |
| `owner_qq_ids` | 空 | 主人 QQ 号；留空=私聊的人都是主人 |
| `only_private` | 开 | 群聊不参与（最安全） |
| `top_k` | 5 | 每次注入几条 |
| `embedded_db_path` | 空 | 自带中枢的库文件位置；留空=插件数据目录。若你另外跑着独立中枢，填同一个 `brain.db` 路径才不会分裂 |
| `inject_enabled` / `ingest_enabled` | 开 | 分别关掉读/写 |
| `diag_enabled` | 开 | 上报自检记录，排查「为什么没同步」 |
| `debug` | 关 | 控制台打日志 |

## 命令

- `桥状态` —— 中枢模式、地址、记忆条数

## 记忆是怎么变成记忆的

1. 你说一句话，插件拿它去 `/memory/recall`，命中 5 条 + 最近 2 条拼进 system prompt
2. 回复结束后，这一轮丢给 `/memory/ingest`，后台用小模型抽成 0~3 条记忆原子
3. 写入时做两层去重：文本 hash 精确匹配 + 字符二元组 Jaccard ≥ 0.86

没有定时同步，每条消息都实时走一遍。

## 顺手拉起 NapCat（可选，仅 Windows）

如果你用 NapCat 接 QQ，可以让插件在 AstrBot 启动时把它一起拉起来，省得每次手动开、还要扫码：

| 配置 | 说明 |
|---|---|
| `napcat_autostart` | 开 |
| `napcat_dir` | 有 `NapCatWinBootMain.exe` 的那层，如 `F:\NapCat\NapCat.52230.Shell` |
| `napcat_uin` | 机器人 QQ 号；填了才走快速登录（免扫码） |

快速登录复用上次的登录态，所以**必须先成功登录过一次**。
改过密码、或在别的设备登录把 PC 端挤下线，仍然要扫一次。

## 隔离

- 群友各算各的人（`qq:{QQ号}`），**不会**串到主人身上
- 群聊默认完全不参与
- 别人的私聊也看不到你的记忆

## 隐私

记忆全部存在本机 SQLite（`data/plugin_data/astrbot_plugin_dafeiyu_bridge/brain.db`），
不经过任何第三方服务。会调用你自己配置的模型接口来做抽取。

## 出问题了？

看 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) —— 里面是实际踩过的坑：
开机自启被安全软件拦、安全警告弹窗、记忆条数变 0、NapCat 每次扫码、回复说“不知道”怎么查等等。

## 许可

MIT
