# astrbot-plugin-gamdl

基于 [gamdl](https://pypi.org/project/gamdl/) 的 AstrBot Apple Music 插件（单项目版，不拆分独立服务端）。

这个插件直接在 AstrBot 进程内完成：
- Apple Music 搜索/链接解析
- 下载队列与后台任务
- 封面/动态封面/歌词导出
- NapCat/OneBot 文件发送

## 快速开始（先部署 wrapper）

使用本插件前，建议先把 wrapper 跑起来，再去 AstrBot 管理页面填写配置。

### 第一步：初始化账号数据（一次性）

```bash
docker run -v ./rootfs/data:/app/rootfs/data -e args="-L <你的账号>:<你的密码> -F" --rm jelly714love/wrapper:x86
```

### 第二步：常驻运行 wrapper（arm64 / NanoPi 示例）

```bash
docker run -d \
  --name wrapper \
  --restart unless-stopped \
  -v ./rootfs/data:/app/rootfs/data \
  -p 10020:10020 \
  -p 20020:20020 \
  -p 30020:30020 \
  -e args="-H 0.0.0.0 -M 20020" \
  jelly714love/wrapper:nanopi-r2s
```

### 第三步：看日志并按提示操作

```bash
docker logs -f wrapper
```

参考教程（仅参考）：  
[https://applemusic.mintlify.app/amdl/quickstart/macos](https://applemusic.mintlify.app/amdl/quickstart/macos)

如果是 x86 平台，镜像建议使用：

```bash
ghcr.io/itouakirai/wrapper:x86
```

## AstrBot 配置页怎么填

在 AstrBot 的 `astrbot_plugin_gamdl` 配置页面，至少要填这两个：

- `use_wrapper = true`
- `wrapper_account_url = http://<wrapper主机IP>:30020`（如果你自己的 wrapper 用 `20030`，就填 `http://<IP>:20030`）
- `wrapper_decrypt_ip = <wrapper主机IP>:10020`

容器跨主机部署时，不要写 `127.0.0.1`，要写 AstrBot 容器可访问的主机 IP。

## 特性

- 支持平台：QQ NapCat（OneBot/aiocqhttp）
- 命令：`am` 前缀（`am 搜歌` / `am 搜专` / `am 搜人` / `am 链接` 等）
- 自动识别 Apple Music URL（可配置）
- 会话级设置记忆（按 `unified_msg_origin`）
- 长任务后台执行 + 主动回推
- 传输模式：逐个发送 / ZIP
- MV 发送优先视频，失败回退文件

## 与旧架构区别

本仓库是“单插件”架构，不需要再单独部署 `astrbot-applemusic-service`。

依赖关系：
- 上游下载能力来自 `gamdl`（以及你参考的 `gamdl-telegram-bot` 方案）
- AstrBot 插件层负责命令、会话、QQ 消息发送

## 安装

AstrBot 支持直接从仓库安装，安装时会自动执行 `pip install -r requirements.txt`。

仓库地址：
- [https://github.com/wuuduf/astrbot-plugin-gamdl](https://github.com/wuuduf/astrbot-plugin-gamdl)

也可手动安装：

```bash
pip install -r requirements.txt
```

## 命令

- `am help` / `am 帮助`
- `am 搜歌 <关键词>`
- `am 搜专 <关键词>`
- `am 搜人 <关键词>`
- `am 链接 <apple music url>`
- `am 歌词 <song-url|song-id|album-url|album-id>`
- `am 封面 <url|type id>`
- `am 动态封面 <url|type id>`
- `am 设置 [值 ...]`

搜索结果后可直接回复：
- `1`
- `1 zip`
- `1 歌词`
- `1 封面`
- `1 动态封面`
- `1 专辑`
- `1 mv`

## `am 设置` 详解

`am 设置`（不带参数）：查看当前会话设置。

`am 设置 <值 ...>`：修改当前会话设置，支持多个值空格拼接。

可用值：
- 音质：`alac` / `flac` / `aac` / `atmos`
- AAC：`aac` / `aac-lc` / `aac-binaural` / `aac-downmix`
- MV 音轨：`mv-atmos` / `mv-ac3` / `mv-aac`（也可直接 `atmos` / `ac3` / `aac`）
- 歌词格式：`lrc` / `ttml`
- 传输模式：`zip` / `逐个`（`one`）
- 自动附带：`歌词开` / `歌词关` / `封面开` / `封面关` / `动态封面开` / `动态封面关`

示例：
- `am 设置 zip`
- `am 设置 alac`
- `am 设置 aac-lc`
- `am 设置 歌词关 封面开`

说明：
- 设置按会话隔离（群聊/私聊互不影响）。
- 默认：自动附带歌词/封面/动态封面为关闭。

## 配置项（核心）

- `search_limit`：搜索展示数量
- `selection_timeout`：选歌等待超时
- `auto_parse_url`：是否自动识别链接
- `default_transfer_mode`：默认发送模式（one/zip）
- `max_concurrency`：后台任务并发（默认 1）
- `job_timeout_seconds`：任务超时
- `download_dir`：下载输出目录
- `temp_dir`：临时目录（歌词/封面/ZIP）
- `clean_cache_on_reload`：重载时清理临时目录
- `path_map`：路径映射（容器场景可用）
- `use_wrapper` / `wrapper_account_url` / `wrapper_decrypt_ip` / `cookies_path`：Apple 鉴权与解密来源
  - 账号 API 可配置成远程：例如 `http://192.168.1.10:20030`
  - 解密端口可配置成远程：例如 `192.168.1.10:10020`

完整项见：
- [_conf_schema.json](./_conf_schema.json)

## 数据目录

插件持久化数据均写入 AstrBot data 目录（不会写插件目录）：
- 会话设置
- 临时文件
- 下载目录（未配置 `download_dir` 时）

## 已知限制

- `station` 链接目前仅可解析，不支持下载（受上游 `gamdl` 能力限制）。
- `flac` 在本插件内会回退到 `alac` 下载策略（`gamdl` 输出链路限制）。

## 目录结构

- `main.py`：插件主类与命令入口
- `core/backend.py`：本地任务后端（搜索/解析/下载/封面/歌词）
- `core/client.py`：后端调用封装
- `core/service.py`：业务编排
- `core/session.py`：会话与设置持久化
- `core/sender.py`：NapCat 发送策略

## 版本记录

见 [CHANGELOG.md](./CHANGELOG.md)
