# RepoSage 仓库分析报告

- Repository: `psf/requests`
- Commit: `3c7baaa05ab4aa49cc819ce3a819d260de19c157`
- 执行模式: `live`
- 任务: 说明 requests 中 Session.request 到适配器发送的调用链：Session.request、Session.send、HTTPAdapter.send 与 urllib3 连接池的关键调用关系与入口文件

## 1. 问题结论

Session.request 负责构造并准备 PreparedRequest，然后调用 Session.send；Session.send 按 URL 前缀通过 get_adapter 选出适配器并调用 adapter.send；HTTPAdapter.send 通过连接池管理器取得连接（conn），再调用 conn.urlopen 发送请求。入口文件是 src/requests/sessions.py 与 src/requests/adapters.py。

## 2. 仓库总体结构

sessions.py: Session.__init__ 挂载 HTTPAdapter；prepare_request 生成 PreparedRequest；request() 合并环境设置并调用 self.send(prep,...)；send() 通过 self.get_adapter(url=request.url) 选择适配器并调用 adapter.send(request,...)。adapters.py: HTTPAdapter.send() 调用 get_connection_with_tls_context 取得 urllib3 连接，随后 conn.urlopen(...) 发出请求，再由 build_response 包装成 Response。

## 3. 关键代码文件与调用关系

- **sessions.py 定义 Session.request 与 Session.send，并默认挂载 HTTPAdapter。**
  - 源码位置：`github://psf/requests@3c7baaa05ab4aa49cc819ce3a819d260de19c157/src/requests/sessions.py` · [L502-L503](https://github.com/psf/requests/blob/3c7baaa05ab4aa49cc819ce3a819d260de19c157/src/requests/sessions.py#L502-L503)
  - 证据 ID：`452208a4-c3f7-44ea-a420-6385cb5bd39d`
  - 原文：

    ```text
    self.mount("https://", HTTPAdapter())
            self.mount("http://", HTTPAdapter())
    ```

- **adapters.py 定义 HTTPAdapter.send，经由连接池连接调用 urllib3 的 urlopen。**
  - 源码位置：`github://psf/requests@3c7baaa05ab4aa49cc819ce3a819d260de19c157/src/requests/adapters.py` · [L662-L664](https://github.com/psf/requests/blob/3c7baaa05ab4aa49cc819ce3a819d260de19c157/src/requests/adapters.py#L662-L664)
  - 证据 ID：`5fbacbd1-58e9-4571-b967-e5572f46e001`
  - 原文：

    ```text
    conn = self.get_connection_with_tls_context(
                    request, verify, proxies=proxies, cert=cert
                )
    ```


### 调用关系

- **Session.request 准备好 PreparedRequest 后调用 Session.send。**
  - 源码位置：`github://psf/requests@3c7baaa05ab4aa49cc819ce3a819d260de19c157/src/requests/sessions.py` · [L651-L651](https://github.com/psf/requests/blob/3c7baaa05ab4aa49cc819ce3a819d260de19c157/src/requests/sessions.py#L651-L651)
  - 证据 ID：`dc4881c1-1a84-4ef2-8597-d58bb71d100e`
  - 原文：

    ```text
    resp = self.send(prep, **send_kwargs)
    ```

- **Session.send 用 get_adapter 选择适配器并调用 adapter.send。**
  - 源码位置：`github://psf/requests@3c7baaa05ab4aa49cc819ce3a819d260de19c157/src/requests/sessions.py` · [L778-L778](https://github.com/psf/requests/blob/3c7baaa05ab4aa49cc819ce3a819d260de19c157/src/requests/sessions.py#L778-L778)
  - 证据 ID：`944c6989-f53c-4781-8bab-d9cb3de63606`
  - 原文：

    ```text
    adapter = self.get_adapter(url=request.url)
    ```


## 4. 实际代码证据

- **HTTPAdapter.send 最终通过 urllib3 连接的 urlopen 发送请求体与头部。**
  - 源码位置：`github://psf/requests@3c7baaa05ab4aa49cc819ce3a819d260de19c157/src/requests/adapters.py` · [L696-L698](https://github.com/psf/requests/blob/3c7baaa05ab4aa49cc819ce3a819d260de19c157/src/requests/adapters.py#L696-L698)
  - 证据 ID：`5fbacbd1-58e9-4571-b967-e5572f46e001`
  - 原文：

    ```text
    resp = conn.urlopen(
                    method=request.method,
                    url=url,
    ```


## 5. 不确定的结论

- 未逐行读取 get_connection_with_tls_context 内部，其对 poolmanager.connection_from_pool_key 的调用细节未直接引用。
- proxy_manager 分支的建池代码只见到片段，未完整引用。

## 6. 建议继续调查的方向

- 可读取 adapters.py 的 get_connection_with_tls_context 与 init_poolmanager 行范围，补充 ConnectionPool 选择细节。

---

> 机械核验只验证源码引用存在、逐字匹配并定位行号；不证明业务语义或架构推断本身正确。

