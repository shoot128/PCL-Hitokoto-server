# PCL 一言 · Linux 句库服务

服务端域名：**`yiyan.overpass.top`**

这是 PCL 自定义主页的 Linux 服务端，用来替代 Windows 端的本地 `Hitokoto.ps1` 数据源：

- 向 PCL 提供完整句库下载；
- 向 PCL 的“自定义 API”提供随机一言；
- 可选地接收新句子（需要管理员 Token）。

**固定端口：`13469`**（在 10000–65535 范围内随机选定）。
服务只监听 `127.0.0.1:13469`，由同机 Nginx 反向代理到 `https://yiyan.overpass.top`。

---

## 1. 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/hitokoto.json` | 完整句库，给 PCL“下载完整句库”用 |
| `GET` | `/api/quotes` | 同 `/hitokoto.json` |
| `GET` | `/api/hitokoto` | 随机一句，给 PCL“自定义 API”用 |
| `POST` | `/api/quotes` | 新增一条，需要 Token |
| `POST` | `/api/quotes/bulk` | 批量新增，需要 Token，单次最多 500 条 |

`/api/hitokoto` 返回 Hitokoto v1 兼容字段：

```json
{
  "id": "offline-001",
  "uuid": "offline-001",
  "hitokoto": "路漫漫其修远兮，吾将上下而求索。",
  "type": "诗词",
  "from": "《离骚》",
  "from_who": "屈原",
  "source": "《离骚》",
  "author": "屈原",
  "creator": "pcl-hitokoto-linux",
  "created_at": 1792820404
}
```

`/hitokoto.json` 返回：

```json
{
  "name": "PCL Hitokoto Library",
  "source": "https://github.com/hitokoto-osc/sentences-bundle",
  "license": "AGPL-3.0",
  "updatedAt": "2026-10-24T06:47:14+00:00",
  "count": 10526,
  "quotes": [
    {
      "id": "9818ecda-9cbf-4f2a-9af8-8136ef39cfcd",
      "text": "与众不同的生活方式很累人呢，因为找不到借口。",
      "source": "幸运星",
      "author": "",
      "type": "动画",
      "typeCode": "a",
      "createdAt": "2016-07-15T18:05:09+00:00"
    }
  ]
}
```

PCL 的 `Hitokoto.ps1` 支持 `quotes` 包装，字段 `text` / `source` / `author` / `type` / `id` 都能直接识别。

---

## 2. Linux 依赖与部署

以下以 Debian / Ubuntu 为例。RHEL 系把 `apt` 换成 `dnf`，并把 systemd 里的 `www-data` 改成 `nginx` 即可。

### 2.1 安装依赖

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx rsync
```

要求 Python 3.10 或更高版本：

```bash
python3 --version
```

### 2.2 放置项目

在项目根目录（同时包含 `client/` 和 `server/`）执行，把服务端上传到 `/opt/hitokoto-server`：

```bash
sudo mkdir -p /opt/hitokoto-server
sudo rsync -av ./server/ /opt/hitokoto-server/
```

如果不用 `rsync`，用 `cp -a`：

```bash
sudo cp -a ./server/. /opt/hitokoto-server/
```

### 2.3 创建虚拟环境并安装 Python 依赖

```bash
cd /opt/hitokoto-server
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install -r requirements.txt
```

依赖只有：

```text
fastapi==0.115.6
uvicorn[standard]==0.32.1
```

### 2.4 配置环境变量

```bash
cd /opt/hitokoto-server
sudo cp .env.example .env
sudo chmod 600 .env
sudo nano .env
```

`.env` 示例：

```dotenv
HITOKOTO_HOST=127.0.0.1
HITOKOTO_PORT=13469
HITOKOTO_DATA_FILE=/opt/hitokoto-server/data/quotes.json
HITOKOTO_LOG_LEVEL=info

# 留空 = 禁用所有 POST 写入接口
# 需要写入时生成一个长随机 Token：
#   openssl rand -hex 32
HITOKOTO_ADMIN_TOKEN=
```

端口只使用 **13469**。这个值必须与 Nginx 里的 `server 127.0.0.1:13469` 完全一致。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HITOKOTO_HOST` | `127.0.0.1` | 只监听本机回环地址 |
| `HITOKOTO_PORT` | `13469` | 监听端口，代码会校验必须在 10000–65535 |
| `HITOKOTO_DATA_FILE` | `data/quotes.json` | 句库文件；相对路径基于 `app.py` 所在目录 |
| `HITOKOTO_ADMIN_TOKEN` | 空 | 为空时 POST 接口返回 503 |
| `HITOKOTO_LOG_LEVEL` | `info` | `critical/error/warning/info/debug/trace` |

### 2.5 数据文件与权限

仓库自带 `data/quotes.json`，已替换为一言开源社区官方句库 [hitokoto-osc/sentences-bundle](https://github.com/hitokoto-osc/sentences-bundle) 的转换版本：

- 当前 **10526** 条，约 3.3 MB；
- 上游版本 `1.0.1334`，导入时间 `2026-10-24`；
- 保留 12 个分类：动画、漫画、游戏、文学、原创、网络、其他、影视、诗词、网易云、哲学、抖机灵；
- 来源、字段转换和分类计数见 [data/README.md](data/README.md) 与 [data/SOURCE.json](data/SOURCE.json)；
- 上游采用 **AGPL-3.0**，许可证原文在 [data/LICENSE-AGPL-3.0.txt](data/LICENSE-AGPL-3.0.txt)。

需要重新导入时，在服务端项目根目录执行：

```bash
python3 tools/import_sentences_bundle.py
```

脚本会重新下载官方分类并生成 `data/quotes.json`，旧文件备份为 `data/quotes.json.bak`。也可以直接替换 `data/quotes.json`，或启动后用 POST 接口写入。

让服务用户可写数据目录：

```bash
sudo chown -R root:root /opt/hitokoto-server
sudo chmod -R a+rX /opt/hitokoto-server
sudo chown -R www-data:www-data /opt/hitokoto-server/data
sudo chmod 600 /opt/hitokoto-server/.env
```

`.env` 保持 `600 root:root` 即可；systemd 在启动时以 root 读取 `EnvironmentFile`，再切换到 `www-data`。

### 2.6 前台试启动

```bash
cd /opt/hitokoto-server
set -a
. ./.env
set +a
.venv/bin/python app.py
```

看到类似输出即表示监听成功：

```text
INFO:     Uvicorn running on http://127.0.0.1:13469
```

另开一个终端验证：

```bash
curl -fsS http://127.0.0.1:13469/healthz
curl -fsS http://127.0.0.1:13469/hitokoto.json | head -c 300
curl -fsS http://127.0.0.1:13469/api/hitokoto
```

按 `Ctrl+C` 停止前台进程。

---

## 3. systemd（可选，推荐）

```bash
sudo cp /opt/hitokoto-server/systemd/hitokoto.service /etc/systemd/system/hitokoto.service
sudo systemctl daemon-reload
sudo systemctl enable --now hitokoto
sudo systemctl status hitokoto --no-pager
```

查看日志：

```bash
sudo journalctl -u hitokoto -n 50 --no-pager
```

服务文件内容见 [systemd/hitokoto.service](systemd/hitokoto.service)。它假设：

- 代码在 `/opt/hitokoto-server`；
- 服务用户是 `www-data`；
- 数据目录是 `/opt/hitokoto-server/data`；
- 环境变量从 `/opt/hitokoto-server/.env` 读取。

如果实际路径或用户不同，修改 `User`、`Group`、`WorkingDirectory`、`EnvironmentFile`、`ReadWritePaths`。

---

## 4. Nginx 反向代理

### 4.1 放置配置

```bash
sudo cp /opt/hitokoto-server/nginx/yiyan.overpass.top.conf /etc/nginx/conf.d/yiyan.overpass.top.conf
sudo nginx -t
sudo systemctl reload nginx
```

完整配置见 [nginx/yiyan.overpass.top.conf](nginx/yiyan.overpass.top.conf)，关键点是：

- `upstream hitokoto_backend { server 127.0.0.1:13469; }`
- `server_name yiyan.overpass.top;`
- `listen 443 ssl http2;`
- 证书：
  - `/mnt/ssl/overpass.top/fullchain.pem`
  - `/mnt/ssl/overpass.top/privkey.pem`
  - 证书路径沿用之前固定的目录；请确保证书 SAN 包含 `yiyan.overpass.top`，或使用 `*.overpass.top` 通配符证书。如果证书实际放在 `/mnt/ssl/yiyan.overpass.top/`，把两行 `ssl_*` 路径改成该目录。
- 80 端口 `301` 跳转到 HTTPS；
- 代理头：`Host`、`X-Real-IP`、`X-Forwarded-For`、`X-Forwarded-Proto`。

完整内容：

```nginx
upstream hitokoto_backend {
    server 127.0.0.1:13469;
    keepalive 16;
}

server {
    listen 80;
    server_name yiyan.overpass.top;

    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name yiyan.overpass.top;

    ssl_certificate     /mnt/ssl/overpass.top/fullchain.pem;
    ssl_certificate_key /mnt/ssl/overpass.top/privkey.pem;

    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    client_max_body_size 2m;

    access_log /var/log/nginx/yiyan.overpass.top.access.log;
    error_log  /var/log/nginx/yiyan.overpass.top.error.log warn;

    location / {
        proxy_pass http://hitokoto_backend;

        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";

        proxy_connect_timeout 5s;
        proxy_send_timeout    30s;
        proxy_read_timeout    30s;
    }
}
```

### 4.2 检查前置条件

```bash
getent hosts yiyan.overpass.top
sudo ls -l /mnt/ssl/overpass.top/fullchain.pem /mnt/ssl/overpass.top/privkey.pem
```

证书文件必须存在、未过期，并且 Nginx 能读取。域名必须解析到这台服务器。确认证书覆盖新域名：

```bash
openssl x509 -in /mnt/ssl/overpass.top/fullchain.pem -noout -text \
  | grep -A1 "Subject Alternative Name"
```

输出中应包含 `DNS:yiyan.overpass.top`，或包含 `DNS:*.overpass.top`。

### 4.3 测试

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -I https://yiyan.overpass.top/healthz
curl -fsS https://yiyan.overpass.top/hitokoto.json | head -c 300
```

如果 502，检查应用是否真的在 13469 上监听：

```bash
sudo ss -lntp | grep 13469
sudo systemctl status hitokoto --no-pager
```

---

## 5. 在 PCL 里怎么填

| PCL 位置 | 填写内容 |
| --- | --- |
| “API 设置” → 自定义 API | `https://yiyan.overpass.top/api/hitokoto` |
| “句库下载” → 句库地址 | `https://yiyan.overpass.top/hitokoto.json` |

PCL 会在 API 地址后自动追加 `encode=json` 和 `charset=utf-8`；服务端会忽略这些额外查询参数，不影响返回。

“下载完整句库”会解析 `quotes` 数组，并保存到 PCL 的 `Hitokoto\library.json`，之后搜索和“从本地库随机一句”都会使用。

---

## 6. 写入接口示例

先在 `.env` 设置 Token，例如：

```bash
openssl rand -hex 32
```

把结果写入：

```dotenv
HITOKOTO_ADMIN_TOKEN=你的长随机Token
```

重启服务：

```bash
sudo systemctl restart hitokoto
```

### 6.1 新增一条

```bash
export HITOKOTO_ADMIN_TOKEN='你的长随机Token'

curl -fsS -X POST https://yiyan.overpass.top/api/quotes \
  -H "Authorization: Bearer $HITOKOTO_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "示例句子。",
    "source": "示例出处",
    "author": "示例作者",
    "type": "文学"
  }'
```

也支持 `X-Admin-Token` 头：

```bash
curl -fsS -X POST https://yiyan.overpass.top/api/quotes \
  -H "X-Admin-Token: $HITOKOTO_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"另一条示例。","source":"网络","author":"","type":"其他"}'
```

### 6.2 批量新增

请求体是数组，单次最多 500 条：

```bash
curl -fsS -X POST https://yiyan.overpass.top/api/quotes/bulk \
  -H "Authorization: Bearer $HITOKOTO_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '[
    {"text":"批量句子一。","source":"示例","author":"","type":"其他"},
    {"text":"批量句子二。","source":"示例","author":"","type":"其他"}
  ]'
```

行为：

- 按 `text` 去重，英文忽略大小写；
- 单条重复返回 `409`；
- 批量重复会计入 `duplicates`，不报错；
- 写入时先写临时文件再原子替换，避免半截 JSON。

---

## 7. 端口与防火墙

应用只监听回环地址，不需要对公网开放 13469：

```bash
# 只开放 Nginx 的 80/443
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

不要开放 `13469`。Nginx 和本服务在同一台机器上，通过 `127.0.0.1` 通信。

如果 Nginx 在容器/另一台机器里，才需要把 `HITOKOTO_HOST` 改成 `0.0.0.0` 并配置防火墙；当前同机部署没有这个必要，也不建议直接暴露应用端口。

---

## 8. 排错

### 8.1 `systemctl status` 显示失败

```bash
sudo journalctl -u hitokoto -n 100 --no-pager
```

常见原因：

- `.env` 不存在或权限/格式错误；
- `HITOKOTO_PORT` 不在 10000–65535；
- `data/quotes.json` 不是合法 JSON；
- 端口被其他进程占用：

```bash
sudo ss -lntp | grep 13469
```

### 8.2 Nginx 502

- 应用没启动：`sudo systemctl status hitokoto`；
- 端口不一致：`.env` 的 `HITOKOTO_PORT` 与 Nginx `server 127.0.0.1:13469` 必须相同；
- 应用只监听 `127.0.0.1`，Nginx 也必须在同一台机器；
- 查看 Nginx 错误日志：

```bash
sudo tail -n 50 /var/log/nginx/yiyan.overpass.top.error.log
```

### 8.3 Nginx 配置不生效

确认 `/etc/nginx/nginx.conf` 的 `http {}` 中包含：

```nginx
include /etc/nginx/conf.d/*.conf;
```

检查最终配置：

```bash
sudo nginx -T | grep -n "yiyan.overpass.top\|13469"
```

### 8.4 POST 返回 503 / 401

- `503`：`.env` 里的 `HITOKOTO_ADMIN_TOKEN` 为空，写入接口被禁用；
- `401`：Token 不匹配，检查 `Authorization: Bearer ...` 或 `X-Admin-Token`。

### 8.5 句库为空或没有更新

```bash
curl -fsS https://yiyan.overpass.top/hitokoto.json | python3 -m json.tool | head -n 20
```

检查 `data/quotes.json` 是否有 `quotes` 数组；服务启动时会把数组或 `quotes/data/results/items` 包装统一读入内存。

---

## 9. 文件说明

```text
app.py                       FastAPI 服务
requirements.txt             Python 依赖
.env.example                 环境变量模板
data/quotes.json             官方句库（10526 条）
data/SOURCE.json             来源、版本、分类计数
data/LICENSE-AGPL-3.0.txt    上游 AGPL-3.0 许可证
data/README.md               句库来源与转换说明
tools/import_sentences_bundle.py  官方句库重新导入脚本
nginx/yiyan.overpass.top.conf      Nginx 配置
systemd/hitokoto.service     systemd 单元（可选）
README.md                    本文件
```

---

## 10. 已知限制

- 只支持单进程写入。不要用 `--workers` 多开，否则各进程的内存句库会不一致。
- 句库全量加载进内存；当前 10526 条约 3.3 MB，建议控制在几万条以内。
- 写入接口没有分页/审核/验证码，公网部署时务必设置强 Token，或保持 `HITOKOTO_ADMIN_TOKEN` 为空只读。
- 本服务不包含 TLS 终止，TLS 由 Nginx 负责。
- 句库来自 [hitokoto-osc/sentences-bundle](https://github.com/hitokoto-osc/sentences-bundle)，按 **AGPL-3.0** 使用；详见 [data/README.md](data/README.md)。

---

## 11. 许可证

- 本服务端代码与转换后的句库：**AGPL-3.0**，见 [LICENSE](LICENSE)。
- 上游句库：<https://github.com/hitokoto-osc/sentences-bundle>，AGPL-3.0；许可证原文见 [data/LICENSE-AGPL-3.0.txt](data/LICENSE-AGPL-3.0.txt)。
- 客户端仓库（[PCL-Hitokoto-client](https://github.com/shoot128/PCL-Hitokoto-client)）使用 MIT License。

如果通过官方提供的超链接/CDN 调用句库，请按上游说明处理 AGPL 豁免；自建分发时请遵守 AGPL。

