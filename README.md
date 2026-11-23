# LOL 战绩速览

进入选英雄时自动查询队友最近战绩：段位、胜率、KDA、最近 10 场、最近对局、常用英雄，并用英雄原画做成卡片。
只读取英雄联盟客户端的本地接口（LCU API），不碰游戏进程，不做任何自动操作。

## 使用

1. 登录英雄联盟客户端。
2. 双击 `dist\LOLStats.exe`（单文件，不需要装 Python）。
3. 进入选英雄后窗口会自动弹到最前面。不在选英雄时可以点「我的战绩」。

- 顶部可以切换统计范围（全部 / 排位 / 匹配 / 大乱斗）和场次（20 / 50 / 100 / 200）。
- 点任意玩家卡片，右侧滑出完整对局列表，按 Esc 关闭。
- 图钉按钮切换窗口置顶。
- 查过的对局缓存在 exe 旁边的 `cache` 文件夹，下次只拉新对局。

如果提示「没找到客户端」：国服客户端一般以管理员身份运行，右键 exe → 以管理员身份运行。

## 开发

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```

没装 pywebview 时，`python app.py` 会用 Edge 的应用窗口模式打开界面。

打包：运行 `build.bat`，生成 `dist\LOLStats.exe`。

### 文件

- `lcu.py`：找客户端端口和密码、分页并发拉战绩、本地缓存、读取英雄原画
- `app.py`：后台线程、本地网页服务、窗口
- `web/index.html`：界面
- `mock_lcu.py`、`test-with-mock.bat`：模拟客户端，没开游戏时测试用
- `icon.ico`：程序图标

### 离线测试

`test-with-mock.bat` 会启动模拟客户端并打开界面。想看到真实英雄原画，可以把官方 Data Dragon 的图片放进 `mock_assets/`
（文件名如 `145_load.jpg`、`145_square.png`、`145_splash.jpg`），没有图片时只显示色块。

## 注意

- 只显示客户端本来就能看到的队友信息，排位里被隐藏的对手不会查。
- 国服（腾讯）对第三方程序检测较严，建议先在小号上试。
- 客户端更新后接口可能变化，出问题时看标题栏的状态提示。
- 本项目与 Riot Games、腾讯无关。英雄联盟及相关图片版权归 Riot Games 所有。
