# ECS（47.116.13.227）登录

下次要登这台，先读这份。

记录日期：2026-10-31。从这台 Mac 实测过：`ssh ecs` 免密通。

## 先分清

同一个公网 IP，端口不同，是两台机器：

| 入口 | 实际是哪台 | 怎么登 |
|---|---|---|
| `47.116.13.227:22` | **ECS 本机**（阿里云华东2 上海，Ubuntu 22.04，2 核 1 GiB，5 Mbps 固定带宽，实例 i-uf61r8ozr5y1dzrea90u） | `ssh ecs`（root） |
| `47.116.13.227:6001` | **gpu2**，经 ECS 上 frps 的隧道进去 | `ssh gpu2`，见 [gpu2-h100.2-login.md](gpu2-h100.2-login.md) |

## 登录

`~/.ssh/config`：

```
Host ecs
    HostName 47.116.13.227
    Port 22
    User root
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
```

```bash
ssh ecs
ssh ecs 'systemctl is-active frps coturn'
```

公钥是 2026-10-31 用户在阿里云控制台「远程连接 → 网页连接」里手动加进 `/root/.ssh/authorized_keys` 的（这台 Mac 的 `~/.ssh/id_ed25519.pub`，注释 `biancaoming`）。免密失败时，同样走网页连接登进去查，不要猜密码。

## 上面跑着什么

| 服务 | 配置 | 用途 | 谁管 |
|---|---|---|---|
| frps | `/usr/local/bin/frps -c /etc/frp/frps.toml`，systemd `frps` | gpu2 的 frpc 连 7000，映射 6001（gpu2 ssh）、8183（小新 / mainbrain）等端口 | 不归小新仓库管，**别动** |
| coturn | `/etc/turnserver.conf`（600），systemd `coturn`，日志 `/var/log/turnserver/turn.log` | 小新视频通话的 TURN 中继 | `deploy/ecs.sh` 从 `deploy/ecs/turnserver.conf` 渲染，`TURN_SECRET` 在 `.env` |

ECS 上不存家庭数据、不跑小新业务代码。改 coturn 只改模板再跑 `deploy/ecs.sh`，不要在 ECS 上手改 `/etc/turnserver.conf`。

## 安全组（阿里云控制台 → 实例 → 网络与安全组 → 入方向）

| 端口 | 用途 | 状态（2026-10-31） |
|---|---|---|
| 22/tcp | ssh ECS | 通 |
| 7000/tcp | frps | 通 |
| 6001、8183 等 /tcp | frp 映射的 gpu2 端口 | 通 |
| 3478/tcp | TURN | 通 |
| 3478/udp | TURN | 通（外网 Allocate 实测成功） |
| 49160-49200/udp | TURN 中继端口段 | 通（两端端口抓包实测） |

ECS 自己的 ufw 是关着的，挡端口的只有安全组。端口不通先抓包：`ssh ecs 'timeout 8 tcpdump -ni any udp port 3478'`，抓不到包就是安全组没放行。

## 注意

- **重启 ECS 会断 gpu2 的隧道**：`ssh gpu2`、小新 8183 都会断几分钟。2026-10-31 登录时提示 `System restart required`，还没重启，要挑不用小新的时候。
- 带宽只有 5 Mbps，而且所有经 frp 的 HTTPS 也走这里。不要在 ECS 上下大文件、跑大流量测试。
- 内存只有 1 GiB，别往上装重服务。
