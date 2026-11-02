# ADR 0002：MCP SDK 适配层

锁定环境中 langchain=1.4.7，不提供 langchain.mcp.MCPAdapter。通过实际导入检查确认，不能按原包中的 beta API 名称拼装调用。

选用官方 mcp=1.30.0 SDK，集中封装在 mgar_v3/mcp/client.py，支持 stdio 与 GitHub Streamable HTTP。只读角色仅映射 get_file_contents、issue_read；写入只在 Dispatcher 的专用传输中映射 add_issue_comment。

已执行跨进程 initialize/list/call，验证凭据不继承、固定 commit、Issue 内容快照、列表型工具响应和写后读回。测试服务为明确标记的受控替身；实际 GitHub MCP 仍须 R1 live 验收，不能由此推断远端工具格式已验证。
