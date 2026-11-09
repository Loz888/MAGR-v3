# RepoSage 仓库分析报告

- Repository: `encode/httpx`
- Commit: `b5addb64f0161ff6bfe94c124ef76f6a1fba5254`
- 执行模式: `live`
- 任务: 说明 httpx 中 Client 与 transport 的分层：从 Client.request/send 进入 _send_single_request，再调用 transport 与连接池的关键调用链，指出入口文件与主要类

## 1. 问题结论

httpx 采用“Client 高层编排 → _send_single_request 单次发送 → Transport 抽象 → HTTPTransport 适配 → httpcore 连接池”的分层。

## 2. 仓库总体结构

入口在 httpx/_client.py：Client/AsyncClient 负责认证、重定向、Cookie、超时等；_send_single_request 只发送单次请求。传输接口在 httpx/_transports/base.py：BaseTransport/AsyncBaseTransport。默认实现在 httpx/_transports/default.py：HTTPTransport/AsyncHTTPTransport 持有 httpcore 连接池并做协议适配。

## 3. 关键代码文件与调用关系

- **httpx/_client.py 是 Client 入口，request 转调 send，发送链最终到达 _send_single_request。**
  - 源码位置：`github://encode/httpx@b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_client.py` · [L825-L825](https://github.com/encode/httpx/blob/b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_client.py#L825-L825)
  - 证据 ID：`8c662add-c21d-4595-9e14-c2efc05df5c0`
  - 原文：

    ```text
            return self.send(request, auth=auth, follow_redirects=follow_redirects)
    ```

- **httpx/_transports/default.py 的 HTTPTransport 构造 httpcore.ConnectionPool，并在 handle_request 中调用连接池。**
  - 源码位置：`github://encode/httpx@b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_transports/default.py` · [L156-L156](https://github.com/encode/httpx/blob/b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_transports/default.py#L156-L156)
  - 证据 ID：`ae42ea9e-4f88-420f-af83-eac5af2a440e`
  - 原文：

    ```text
                self._pool = httpcore.ConnectionPool(
    ```


### 调用关系

- **Client.send 通过 _send_handling_auth → _send_handling_redirects → _send_single_request 逐层下发。**
  - 源码位置：`github://encode/httpx@b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_client.py` · [L914-L914](https://github.com/encode/httpx/blob/b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_client.py#L914-L914)
  - 证据 ID：`8c662add-c21d-4595-9e14-c2efc05df5c0`
  - 原文：

    ```text
            response = self._send_handling_auth(
    ```

- **_send_single_request 先用 _transport_for_url 选 transport，再调用 transport.handle_request；HTTPTransport 内再调用 self._pool.handle_request。**
  - 源码位置：`github://encode/httpx@b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_client.py` · [L1014-L1014](https://github.com/encode/httpx/blob/b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_client.py#L1014-L1014)
  - 证据 ID：`17c26d5c-031a-434f-8d4a-ea7f924b8c40`
  - 原文：

    ```text
                response = transport.handle_request(request)
    ```


## 4. 实际代码证据

- **BaseTransport 定义统一的 handle_request(Request)->Response 接口，Client 通过该抽象调用传输层。**
  - 源码位置：`github://encode/httpx@b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_transports/base.py` · [L26-L26](https://github.com/encode/httpx/blob/b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_transports/base.py#L26-L26)
  - 证据 ID：`fe651f91-2ac2-42e2-b1b1-76eef155f2f8`
  - 原文：

    ```text
        def handle_request(self, request: Request) -> Response:
    ```

- **HTTPTransport.handle_request 将 httpx.Request 转为 httpcore.Request，并交由 httpcore 连接池执行实际网络请求。**
  - 源码位置：`github://encode/httpx@b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_transports/default.py` · [L250-L250](https://github.com/encode/httpx/blob/b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_transports/default.py#L250-L250)
  - 证据 ID：`ae42ea9e-4f88-420f-af83-eac5af2a440e`
  - 原文：

    ```text
                resp = self._pool.handle_request(req)
    ```


## 5. 不确定的结论

- 未逐行展开 AsyncHTTPTransport.handle_async_request；异步链按对称结构理解，未作为关键结论引用。

## 6. 建议继续调查的方向

- 如需异步细节，可读取 httpx/_transports/default.py 中 AsyncHTTPTransport.handle_async_request 段落。

---

> 机械核验只验证源码引用存在、逐字匹配并定位行号；不证明业务语义或架构推断本身正确。

