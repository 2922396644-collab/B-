# B站高码流视频下载 - macOS

当前版本已经补齐到和 Windows MVP 对齐，主流程是：

1. 输入一个或多个 B 站链接
2. 点击“读取链接”
3. 看见标题、作者和预计下载规格
4. 点击“开始下载”
5. 首次选择保存目录，后续可直接复用默认目录

## 当前实现

- 支持单个链接和多链接批量
- 支持独立登录浏览器资料目录
- 支持读取登录后的 Cookie 来获取账号本来就有权限访问的最高可用清晰度
- 支持并行下载多个视频
- 支持设置默认下载路径
- 支持下载历史查看、筛选和打开文件
- 支持历史统计面板
- 支持浅色 / 深色 / 跟随系统主题

## 运行方式

在 `01-MAC` 目录双击 `run.command`。

也可以在终端里执行：

```bash
./run.command
```

首次运行会自动创建虚拟环境并安装依赖。

为避免外接磁盘上的 sidecar 文件干扰 Python 环境，mac 版本会把虚拟环境放在：

```text
~/Library/Caches/BiliHighQualityDownloader/venv
```

## 打包 App

在 `01-MAC` 目录执行：

```bash
./build.command
```

打包完成后会在下面生成 macOS App：

```text
01-MAC/dist/B站高码流视频下载.app
```

## 使用前准备

- 建议 macOS 已安装 `Google Chrome` 或 `Microsoft Edge`
- 建议系统里可直接使用 `ffmpeg`

如果第一次双击 `run.command` 提示没有执行权限，可以先执行：

```bash
chmod +x run.command
chmod +x build.command
```

如果想拿到账号本来能看的更高规格，先点 GUI 里的“打开登录浏览器”，在弹出的独立浏览器里登录 B 站，然后关闭这个浏览器窗口，再回到工具里读取和下载。

应用配置、缓存和独立浏览器资料目录默认保存在：

```text
~/Library/Application Support/BiliHighQualityDownloader/
```

## 重要边界

这个工具只会尝试下载你的账号本来就有权限访问的规格，不会绕过会员权限、DRM 或平台访问控制。
