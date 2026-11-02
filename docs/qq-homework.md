# 班级作业采集（QQ → 小新）

小新里「看下今天的作业」「上周二的作业」「最近一周的作业」这些问题，答案来自提前抓好的作业库，
不是现场去爬 QQ（QQ 要人操作、慢、不稳）。

    .venv/bin/python -m tools.qq_homework --days 10         抓最近 10 天（已有记录的跳过）
    .venv/bin/python -m tools.qq_homework --days 10 --force 连已有的也重抓
    .venv/bin/python -m tools.qq_homework --day 2026-10-23  只抓某一天
    .venv/bin/python -m tools.qq_homework --days 3 --no-images  不抓附图（快，调试用）

数据：`XIAOXIN_DATA/homework/<YYYY-MM-DD>.json`，一天一个文件，**按天幂等**（重抓就覆盖那一天，不会出两份）。
接口：`GET /api/homework?date=|since=&until=`、`PUT /api/homework/<day>`（只有管理员能写）。
小新的工具是 `homework_report`，界面上同时出一张作业卡（`server/static/homework.js`）。

## 采集路径（pad，有线 adb 序列号 HA2KGGAH）

1. `am force-stop` 再启动 QQ（**不要用返回键退**：QQ 停在二级页面时连按会退出 QQ，跑到别的 app 上去）。
2. 会话列表 → 群「勤惜2024级3班」→ 底部工具栏「作业」→「全部作业」。
3. 列表从上往下滚，**每次只滚半屏**（1250 → 520），前后两屏大面积重叠；两屏画面几乎一样就是滚到底。
4. 截图分批交给视觉模型（一批 6 张、批间重叠 2 张）读出日期、学科、正文、提醒、有没有附图、正文里提到的文件名；
   同一条在多批里都读到时，**正文取最长的那份**（被屏幕切断的那份会短），notes/mentions 取并集。
5. 附图：在列表截图里按像素找「不是白底」的大块（`photo_spots`），点开全屏预览截一张干净的，裁掉状态栏传进小新。
6. 正文提到「群里的 xxx.pdf」：群聊底部「文件」→ 找同名 → 预览 →「保存到手机」→ `/sdcard/Download/QQ/` → `adb pull` → 传进小新。
7. 按天 `PUT` 回小新。

## 坑

- 列表是**自绘控件**：`uiautomator dump` 只能读到标题栏和「最后一条消息」那行，群名、作业正文都读不到。
  所以关键按钮用 `find_on_screen()`（截图 + 视觉给坐标），正文只能靠视觉读。
- QQ 冷启动首屏有加载时间，找不到群要等几秒重试（脚本里重试 4 次）。
- 一次滚到底能看到好几个月（实测 9/15–11/2 共 25 天），所以 `--days 10` 也会顺手把更早的几天一起存上，这是好事。
- 周末、没发作业的日子本来就没有记录，脚本最后会把「还缺」的日子列出来，不用当成失败。
- `Tencent/QQfile_recv/nt_data` 是 `drwx--S---`，shell 读不了，群文件只能走「保存到手机」。
- 分享面板坐标会浮动，点之前先 `uiautomator dump` 取 bounds；别用长按存图（「发送给好友」和「保存到手机」挨着，容易误发）。
- 跑之前 `svc power stayon usb`，跑完脚本会自己还原。
