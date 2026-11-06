"""翻唱：家里人录一段唱歌的声音，小新用这个声音唱任何一首歌，聊天里出一张能播放、拖动、滚歌词的音乐卡片。

  voices.py   唱歌声音库（DATA/sing/voices/）：谁录的、多长，一个名字一份，重录就换掉
  qq.py       QQ 音乐：扫码登录、每天续期、搜歌、歌词、下载原曲（子进程跑 qq_cli.py，凭证在 DATA/qqmusic/）
  job.py      后台翻唱：搜歌 → 原曲 → gpu2 上的 SoulX-Singer-SVC（127.0.0.1:18024）→ mp3 → 以小新的身份发进线程
  video.py    发到微信的视频：封面 + 转动的唱片 + 滚动歌词 + 翻唱的声音，可以只截一段（DATA/sing/videos/）
  api.py      /api/sing/*：录声音、试听、封面、分享视频，QQ 音乐登录（家长）
工具在 server/tools/sing.py（record_singing_voice、sing_song），界面在 static/sing.js。
"""
