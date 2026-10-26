# unnc-cal — 稳定版 UNNC 课表订阅源

把学校 Smart Scheduling 的课表转成一个**不会被清空**的日历订阅地址。

**订阅地址（直接加进苹果日历）：**

```
https://cy7wpjmdh4-cmd.github.io/unnc-cal/timetable.ics
```

## 为什么原来会每周消失

学校那个源（`timetabling.nottingham.edu.cn/ical/api/Activity/Personal/<identity>/ics`）有三个硬伤：

| # | 问题 | 后果 |
|---|------|------|
| 1 | **每响应一次就给所有事件重发随机 UID**（实测连抓两次，81 个 UID 重合 0 个） | 客户端每次刷新都看成“全部删除＋全部新建”，整块推倒重建；中途失败就留下空日历 |
| 2 | 后端出错时仍返回 **HTTP 200 + 纯文本 `Error: Unable to Fetch your ics`** | 任何 HTTP 客户端都以为是成功，解析器拿到垃圾数据 → 丢弃整条订阅 |
| 3 | 声明 `TZID=Asia/Hong_Kong`，但 `VTIMEZONE` 里**没有任何 STANDARD/DAYLIGHT 规则**；`Content-Type` 是 `text/plain` | 严格的解析器解析不出时区偏移，可能整组事件被丢 |

苹果的日历订阅大约一周同步一次，每次都撞上上面这套组合拳，于是形成“一周固定清空一回”。

## 这里怎么做

`.github/workflows/sync.yml` 每 6 小时跑一次 `sync.py`：

1. 抓上游 → **硬校验**（必须是合法 VCALENDAR、必须有 VEVENT、每事件必须有 SUMMARY/DTSTART、不是 `Error:` 文本）
2. 失败就重试 4 次；仍失败则**保留上一份好数据**，绝不发布坏内容
3. 通过后重写：
   - 所有时间转成 **UTC 绝对时刻**，彻底丢弃那个残缺的 VTIMEZONE
   - UID 改成 **按内容哈希的确定性 ID**（`sha1(SUMMARY|DTSTART|DTEND|LOCATION|DESCRIPTION)`）→ 内容不变则 UID 不变，苹果看到“无变化”
   - `DTSTAMP` 固定为 `DTSTART`，事件按时间排序 → 上游没变时输出**字节完全一致**（不会因为每次抓取都算作“已修改”）
4. 事件数骤降超过 40%（怀疑抓到半份数据）→ 拒绝发布，保留旧版
5. 上一份好数据超过 14 天没更新 → 工作流失败，GitHub 会给你发邮件提醒

## 运维

- **看同步状态**：<https://github.com/cy7wpjmdh4-cmd/unnc-cal/actions>
- **抓取用的订阅地址**存放在仓库 Secret `ICS_URL` 里，不落盘、不进 git（`docs/state.json` 已脱敏）
- **如果哪天课表在网页上更新了、这边没跟上**：去 Actions 页面点 `Run workflow` 手动跑一次
- **如果换了账号 / 学校给了新的 identity**：更新 Secret `ICS_URL` 即可，订阅地址不变
- 注意：本仓库是 public（GitHub Pages 免费版要求），`docs/timetable.ics` 里的课程名、教室对所有人可见；不含任何账号凭据

## 本地手动跑

```bash
ICS_URL="https://timetabling.nottingham.edu.cn/ical/api/Activity/Personal/<identity>/ics" python sync.py
```
