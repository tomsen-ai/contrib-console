# contrib-console 贡献控制台

本地网页控制台,聚合我在所有开源项目的 PR / issue 实时状态,用**球权 + 已阅水位**告诉我该干什么、在等什么、什么被晾着。

核心理念:**完整性由确定性代码保证**——每条人工回复都进库、都在页面上,LLM 不参与数据链路。

## 特性

- **任务视图**:issue 和修它的 PR 自动配对成一个任务(靠 PR 声明的 closes/fixes 关联),一行一个任务,Issue / PR 各一列
- **球权判定**:最后说话的是别人 / 被要求修改 / CI 红了 → 球在我这;否则在等对面(等首审 / 等复审 / 等合并)
- **已阅水位**:有新动静的行高亮,点已阅消掉;支持待办备注、暂缓、完成
- **盯梢**:手动盯别人的 PR/issue,状态一变(如合并)立刻触发提醒,带上你留的备注
- **自动发现**:扫全 GitHub 足迹,发现新 repo 提示收编或忽略;自有仓库噪音硬排除
- **自动监控**:服务模式每 10 分钟自动采集,页面每分钟自动刷新
- 项目卡片可拖拽排序;深色 UI,无框架、无依赖

## 依赖

- Python 3 标准库(http.server + sqlite3),零第三方依赖
- 已登录的 [gh CLI](https://cli.github.com/)(数据全部通过 `gh` 抓取)

## 用法

```bash
python3 console.py          # 起服务 → http://localhost:7799
python3 console.py sweep    # 只跑一次采集后退出(供 cron / launchd 用)
```

全部逻辑在一个文件 `console.py` 里;数据存旁边的 `contrib.db`(SQLite,已 gitignore)。

首次使用:改掉 `console.py` 顶部的 `ME = "..."`(你的 GitHub 账号),启动后点"立即扫描"。

## macOS 开机自启(可选)

写一个 launchd plist 指向 `python3 console.py`(注意 `EnvironmentVariables.PATH` 里要含 gh 所在目录,如 `/opt/homebrew/bin`),放到 `~/Library/LaunchAgents/` 后:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<name>.plist
```
