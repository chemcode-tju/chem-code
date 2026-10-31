# 模型登录与配置

在研究空间左下角点击 **设置 → 模型**，或打开 `/settings#model`。保存即生效，无需重启；
正在运行的请求使用开始时的配置，后续请求使用新配置。工作空间账号登录和模型授权是两件事。

## ChatGPT 独立应用授权

1. 打开左下角 **设置 → 模型**，选择 **ChatGPT 账号授权登录**。
2. 点击 **Continue with ChatGPT**，再打开页面给出的官方授权链接。
   在运行 Chemistry Agent 的同一台电脑上完成登录与授权。
3. 首次注册向 OpenAI 传入真实应用名 `Chemistry Agent`；官方页面允许用户编辑名称。
   授权后可以在 [ChatGPT 用量设置](https://chatgpt.com/settings/usage) 单独管理该应用。
4. 刷新模型列表，选择默认模型，点击 **保存并测试连接**。列表来自当前授权账号的
   `/v1/models`，具体调用是否获准以实际响应为准。留空时使用目录中的第一个可见模型。
5. 可以添加其他 ChatGPT 账号、切换已保存的注册、重新授权和退出。相同邮箱的不同
   注册显示独立后缀，避免混淆工作空间。退出保留注册 ID，清除令牌并尝试撤销远端会话；
   若远端撤销未确认，界面提示到 ChatGPT 设置断开应用。

**为什么原先显示 Codex？** 旧功能直接启动 `codex login`，使用 Codex 自身的应用身份。
新功能采用官方 Sign in with ChatGPT 动态应用注册，不需要 OpenAI API Key，也不需要 Codex。
首次使用 `dynamic_agent_client` 和 `agent_name_hint`，后续重用 OpenAI 签发的客户端 ID。
更换应用身份必须重新授权；已有 Codex 登录不会被自动复制或转换，继续可用直至切换。

独立授权使用 PKCE、随机 state/nonce、本机动态端口回调和 ID token 签名/issuer/audience/
expiry/nonce 校验。新的账号只有完成身份和套餐 scope 校验后才会成为活动账号。
注册失败时保留已签发的 ID 供重试，未验证的新凭据不会覆盖原账号。
令牌保存在服务端私有文件，刷新以文件锁串行执行并原子替换；不放入浏览器存储或日志。
模型调用使用公开 `/v1/responses`，固定 `store:false`、`stream:true`；只有收到
`response.completed` 才算成功。模型只输出工具调用计划，由应用注册表执行计算工具。

此官方流程面向开源、本地或自行托管的应用；付费或由开发者远程托管的产品需要另行接入。
浏览器回调为 `127.0.0.1`，远端服务器需要官方自托管流程或另行配置 API 连接，不能把
本机回调改成任意公开域名。当前登录任务在单进程内管理，服务请使用单 worker。

### 保留的 Codex 兼容方式

设置页的 **兼容旧 Codex 登录** 内仍提供设备码登录，名称继续显示 Codex。
服务电脑需安装官方 Codex CLI（已按 0.164.0 验证），可用 `brew install --cask codex`，
或设置 `CHEMISTRY_CODEX_BINARY`。设备码需在 ChatGPT 安全设置中启用。
每个工作空间账号使用独立 `codex/` 目录，不读取日常桌面 Codex 凭据。
该方式的模型列表来自 Codex `model/list`，可能是缓存目录，不保证当前账号有每个模型的权限。

## 在对话中选择模型

输入框发送按钮左侧提供模型下拉列表、刷新按钮和设置入口。
选择仅保存到当前会话，新会话默认跟随设置页的默认模型；刷新或重开会话仍保留选择。
也可手动填写模型 ID。发送过程中暂停切换，摘要和工具续轮使用同一请求内冻结的模型配置。
会话偏好绑定服务类型与地址，换服务后不会把旧偏好直接套用到新服务。

接口：`GET /api/model/models` 读取已保存连接的目录；`GET/PUT /sessions/{id}/model`
读取/保存会话偏好，`model:null` 表示跟随默认。`POST /chat` 未传 `model` 时使用会话偏好，
显式传入时仅覆盖本次请求。目录刷新本身不执行推理，不接受前端另传地址或密钥。

## API Key 和其他模型服务

- **DeepSeek API Key**：填写 DeepSeek 根地址、模型 ID 和 API Key，可开启思考模式。
- **OpenAI 兼容 API / 本地模型**：适用于提供 `/chat/completions` 的服务，支持自定义
  Base URL、模型 ID 和密钥。例如兼容的云服务，以及支持该协议的本机 Ollama/vLLM 服务。
  本机 HTTP 接口允许不填写密钥；远程地址要求 HTTPS 和密钥。
- Anthropic 原生 Messages、Gemini 原生协议及仅支持 Responses 的端点不在此适配范围。
  使用这些服务时需要其 Chat Completions 兼容端点/网关。
- 自动化计算需要模型支持工具调用。单纯的文字连接测试不能保证模型能可靠完成建模。
  兼容接口目前发送标准 `max_tokens`；只接受 `max_completion_tokens` 等不同参数的模型
  需要兼容网关，或使用 ChatGPT 账号方式。

服务地址填写接口根路径，例如 `https://example.com/v1`，不要带 `/chat/completions`。
保存后不显示密钥原文；同一服务留空保留已有密钥。更换服务类型或地址时必须重新填写，
避免把旧服务的密钥发给新服务。删除密钥后切换为 ChatGPT 账号模式。

旧 `DEEPSEEK_*` 环境配置仍作为未设置个人模型时的服务默认配置，以兼容原有部署。
个人设置覆盖默认配置；删除个人密钥不会意外恢复服务器默认密钥。
所有请求错误均隐藏上游响应体；配置校验错误不会回显提交的密钥。

## 保存位置与访问

配置保存在数据库旁的 `model-credentials/<用户标识的哈希>/settings.json`；
独立授权在同目录的 `chatgpt-plan.json`，旧 Codex 凭据在 `codex/`。目录权限为 `0700`，配置与独立授权文件为 `0600`。
这些是服务端私有文件，并非加密数据库；服务所在操作系统账号和备份管理员能访问。
请保护整个数据目录及其备份，不要将其加入版本控制或公开文件服务。
浏览器不使用 localStorage 保存密钥，接口只返回 `has_api_key`。

本地单用户模式仅允许本机访问模型设置；远程访问须启用原有的工作空间账号认证和 HTTPS。
账号开启后设置按用户隔离，写操作校验同源。ChatGPT 登录不会开启工作空间共享账号模式，
也不会改变自动化计算分支原有的本地访问和任务提交确认限制。

此集成适合可信用户的研究空间。Codex 推理通过独立临时目录、只读沙箱和关闭内置执行工具
运行，输出结构化 `tool_calls`，再交给 Chemistry Agent 原有工具注册表执行。
现有计算提交仍需在界面确认。不要将 Codex 执行入口开放为不受信任的公共服务。

`CHEMISTRY_CODEX_TIMEOUT_SECONDS` 控制单次 ChatGPT 响应等待（默认 150 秒）；
`CHEMISTRY_RUN_TIMEOUT_SECONDS` 控制整个对话任务（默认 180 秒）。多轮建模可按需增加后者。
连接测试发送一次简短消息，会使用所选服务的真实额度；自动测试使用模拟响应，不消耗额度。

官方参考：[独立注册](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)、
[账号与续期](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)、
[模型与推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)。
