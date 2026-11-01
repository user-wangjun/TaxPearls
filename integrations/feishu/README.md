# 飞书机器人首次联通

这是独立运行的企业自建应用接收程序。默认运行联通探针；可选启用本文末尾的双向确认账号绑定。使用飞书官方 Node SDK，通过长连接接收私聊文字与新版卡片回传，通过消息 API 发送卡片；无需公网回调地址、不调用 AI、不下载附件。Node 不直接访问业务数据库，账号绑定经鉴权桥接调用 Python 网站服务。

## 1. 创建测试应用

1. 打开 [飞书开发者后台](https://open.feishu.cn/app/)，创建**企业自建应用**，例如“TaxPearls 测试助手”。内部联通与对外共享是两个验收范围；最终需要覆盖外部企业成员和飞书个人版用户，不能只验收应用所属组织。
2. 在“添加应用能力”添加“机器人”。测试版本/可用范围仅包含测试者；不要选择只有 webhook 推送能力的群自定义机器人。
3. 在“权限管理”申请两个应用身份权限：`im:message.p2p_msg:readonly`（读取用户发给机器人的单聊消息）、`im:message:send_as_bot`（以应用身份发消息）。当前不申请通讯录、群聊消息或文件权限。
4. 在“凭证与基础信息”找到 App ID 和 App Secret；只写到下一节的本地 `.env`，不要发送到聊天、截图或提交到 Git。

## 2. 启动本地接收程序

要求 Node.js >=22.9。在本目录运行：

```powershell
npm.cmd ci --ignore-scripts
Copy-Item -LiteralPath .env.example -Destination .env
```

仅首次创建 `.env` 时运行复制命令，已有配置时直接编辑，避免覆盖。填写 `TAXPEARLS_FEISHU_APP_ID`、`TAXPEARLS_FEISHU_APP_SECRET`。可以额外配置 tenant_key、测试者 open_id 白名单；首次测试也可以只用飞书后台的应用可用范围限制。

```powershell
npm.cmd run check
npm.cmd start
```

`check` 仅校验本地配置格式，不访问飞书。启动后只有 `transport_connected` 表示 WebSocket 已建立；仍需执行消息和按钮实测。该程序持续运行，关闭终端或 Ctrl+C 会断开。一次只运行一个客户端，避免飞书把事件分配给另一个测试进程。

## 3. 配置两类长连接订阅

保持接收程序运行，在开发者后台“事件与回调”配置：

- **事件配置**：订阅方式选“使用长连接接收事件”；添加 `im.message.receive_v1`（接收消息）。
- **自动入口事件**：同一事件配置中添加 `im.chat.access_event.bot_p2p_chat_entered_v1`（用户进入与机器人的会话），让用户打开会话后无需输入文字就收到入口卡片。另可添加 `p2p_chat_create`（用户和机器人的会话首次被创建），覆盖首次建立会话；程序兼容两种事件并合并短期重复推送。
- **回调配置**：订阅方式选“使用长连接接收回调”；添加 `card.action.trigger`（卡片回传交互）。选新版回调，不选“消息卡片回传交互（旧）”。

后台可能要求先建立长连接才能保存订阅方式。保存后创建版本，按本企业要求发布/由管理员审核，并将测试者加入可用范围；也可使用后台的测试企业与测试版本功能。内部测试通过不等于外部用户已能使用。

### 对外共享与纯个人用户

官方[机器人支持外部群和外部用户单聊](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/develop-robots/add-bot-to-external-group)的常见问题明确说明外部单聊支持飞书个人版。当前应用无需因此改为商店应用：此对外共享能力适用于企业自建应用。

在“版本管理与发布 → 创建版本 → 对外共享”开启“允许外部用户与机器人单聊”，配置首次单聊是否需要应用所有者审核，再保存发布。提供方可通过企业/团队认证或个人实名认证满足开启条件；个人实名认证仅在中国大陆支持。无需为当前单聊测试额外开启外部群权限。

外部用户通过机器人的分享二维码或 [AppLink](https://open.feishu.cn/document/uAjLw4CM/uYjL24iN/applink-protocol/supported-protocol/open-a-bot) 打开会话，按飞书提示确认开启单聊或提交申请。机器人不能只凭一个外部用户 ID 主动发起单聊；用户先建立会话后，机器人才能继续主动推送。企业用户还受其组织的对外沟通设置约束。

机器人分享码用于打开会话，**不等于 TaxPearls 账号已经绑定**。产品绑定使用下文的短期请求、飞书会话身份、网站登录确认和原私聊最终确认。对外共享机器人不能获取外部用户的 user_access_token，因此不能直接套用内部员工的 OAuth 扫码登录作为所有外部用户的绑定方案。

外部验收至少覆盖一个其他组织账号、一个飞书个人版账号的扫码打开、开启单聊、收到卡片、三个按钮回传，并在业务绑定实现后分别验证任务权限与解除绑定。未完成这些实测，不宣称客户扫码接入已经完成。

## 4. 真实验收

发布自动入口事件后，先切到其他聊天，再进入机器人私聊，**不发文字**。应自动看到“TaxPearls · 联通测试”，有“创建任务”“进行中的任务”“已完成的任务”。逐一点击，应该弹出“按钮回传成功”，原卡片显示所点入口的名称。首次建立会话也应发送入口。

在同一进程中，已有入口有效时（10 分钟内）反复进入会话不重复发卡片；卡片失效后再次进入会刷新。点击卡片成功后更新有效期，人工发送“菜单”（或其他私聊文字）仍可补发。短期去重状态仅保存在进程内，重启后首次进入会重新发送，旧卡片签名随重启失效。旧的文字回传仍可用，不是自动入口的前提。

本地状态分开判断：`transport_connected` 是连接，`conversation_entered` 是收到会话事件，`message_received` 是收到私聊文字，`card_delivered` 是飞书消息 API 返回消息编号，`card_action_received` 是收到按钮回传。只有用户在客户端看到卡片和更新，才能确认完整双向链路。

按钮明确标为联通测试，尚不创建或查询真实任务，因此也不会把“未读取任务”显示成“暂无任务”。按钮 10 分钟失效或进程重启后，重新进入会话获取新卡片，也可发送“菜单”补发。群聊、机器人消息、附件被忽略，不记录原文、凭据或原始 SDK 错误。发送失败会提示本地状态，修复后重新进入会话或发消息；进程内去重和队列只用于本次验证，不是生产持久投递服务。

联通后再复用 TaxPearls 现有渠道绑定与权限校验接业务：创建任务、分类材料上传、进行中查询、检查、报告、完成并归档。飞书身份不会直接获得客户数据权限。

## 本地回归

```powershell
npm.cmd test
```

测试使用合成事件和模拟消息发送，另检查已安装 SDK 的长连接事件帧分发与卡片回传编码；不会联网，也不等于真实飞书送达。

官方参考：[长连接](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/long-connection-mode)、[卡片交互示例](https://open.feishu.cn/document/develop-a-card-interactive-bot/explanation-of-example-code)、[官方 Node SDK](https://github.com/larksuite/node-sdk)。

## 可选：启用双向确认账号绑定

此版本先支持 TaxPearls 企业管理员和会计。已绑定不代表任务接口已接入，也不自动开启风险通知订阅。用户流程：网站扫码打开机器人 → 自动出现绑定链接卡片 → 登录 TaxPearls 并核对账号/机构 → 网站确认 → 原飞书私聊点击确认 → 网站显示绑定成功。

### 两端配置

网站根目录 `.env` / 服务端环境配置：

```dotenv
TAXPEARLS_FEISHU_BINDING_ENABLED=1
TAXPEARLS_FEISHU_APP_ID=同一个机器人应用ID
TAXPEARLS_FEISHU_APP_TENANT_KEY=自建应用所属租户key
TAXPEARLS_FEISHU_BRIDGE_KEY=独立随机32字节密钥的64位十六进制
TAXPEARLS_PUBLIC_BASE_URL=https://taxpearls.wxtech.site
```

`APP_TENANT_KEY` 是应用所属租户，不能随外部扫码用户的租户变化。网站正常运行所需的 `TAXPEARLS_FIELD_KEY` 等配置仍须保留。

机器人本目录 `.env`：保留现有 App ID/Secret，增加：

```dotenv
TAXPEARLS_FEISHU_BINDING_ENABLED=1
TAXPEARLS_FEISHU_BRIDGE_URL=http://127.0.0.1:8000
TAXPEARLS_FEISHU_BRIDGE_KEY=与网站相同的独立桥接密钥
```

`BRIDGE_URL` 是 Node 可访问的 Python 服务源站，与手机访问的 `PUBLIC_BASE_URL` 是不同用途。桥接只允许 HTTPS 或本机回环 HTTP，不接受任意明文远程地址。两个程序在同机时可使用回环地址；可在反向代理层禁止公网直接访问 `/api/internal/feishu-binding`。目前不提供公网隧道，也不自动部署域名。手机测试需域名确实指向本版本网站，否则旧线上页面没有绑定接口。

开发者可在本机生成独立密钥（不要贴进聊天或提交）：

```powershell
node -e "console.log(require('node:crypto').randomBytes(32).toString('hex'))"
```

网站安装根目录 Python 依赖后正常启动；Node 在本目录 `npm.cmd start`。登录网站的“用户设置”有绑定管理入口，也可直接打开 `/connect/feishu`。二维码在服务器本地生成，不调用外部二维码服务；码中只有机器人 AppLink。为防止记录敏感凭据，机器人发来的绑定链接使用 URL fragment，网页立即移除地址中的 ticket 并只以 POST 提交。

### 状态与校验

- `pending`：持有链接尚未确认，不赋予登录或任务权限。
- `web_confirmed`：账号和当前登录会话已固定，等待原飞书身份和原私聊确认。网页确认携带与已展示账号、机构及会话绑定的服务端校验凭据；其他标签页切换账号后，旧页面必须重新读取并核对账号。网站确认后切换账号必须取消后重新发起，旧确认卡片失效。
- `completed`：网站与飞书确认均通过，才写入现有 `channel_bindings`；账号、机构、角色及原登录会话在最终确认时重新验证。
- `cancelled` / 过期：不可完成绑定。请求 10 分钟有效；重复确认幂等，不重复建记录，解绑后旧卡片不能恢复绑定。

Node 每 2.5 秒拉取网站已确认的请求，通过消息 API 把最终确认发到原私聊。卡片点击先快速响应“核对中”，后台完成后才发送成功/失败结果；不会在后端拒绝时显示成功。发送确认卡片失败后，网站保留请求并允许 30 秒后再次尝试。桥接请求用独立 HMAC 密钥、60 秒时间窗和持久 nonce 防重放；外部身份和会话保存在现有加密存储层中。原飞书账号只看到掩码账号提示，不自动获得网站会话。

### 验证、迁移与回退

```powershell
# 仓库根目录，用项目 Python 环境
python -B -X utf8 -m unittest tests.test_feishu_binding tests.test_channel_bindings
# 本目录
npm.cmd test
```

SQLite 启动迁移为增量增加 `feishu_binding_requests`、`feishu_bridge_nonces` 和对应索引；沿用敏感字段密钥，并复用已有绑定记录及权限解析。启用前按现有备份流程备份数据库和恢复密钥。关闭两端的 `TAXPEARLS_FEISHU_BINDING_ENABLED` 即停止新绑定；已建立的关系需由本人在启用的管理页解绑。不要删除用户数据或在已有新记录后直接回滚数据库。

本地自动化使用合成账户验证 Node→Python 的真实 HTTP 签名、浏览器登录确认、模拟原飞书回传、绑定结果和解绑，不等于生产部署或真实手机扫码验收。上线仍需用外部企业账号和纯个人飞书账号分别实测，确认域名访问、会话及消息送达。
