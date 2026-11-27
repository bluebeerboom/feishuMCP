# 飞书 Wiki 表格 MCP

这个 MCP 同时支持两种资源，但不会混用接口：

- 普通电子表格：`spreadsheet_token + sheet_id`，使用 Sheets API。
- Wiki 页面里的多维表格：`app_token + table_id + view_id`，使用 Bitable API。

链接中包含 `table=tbl...` 时，必须按第二种资源处理。若 Wiki 节点本身是 `sheet`，MCP 会先读取 Sheets `metainfo`，从 `BITABLE_BLOCK` 的 `blockToken`（形如 `app_token_table_id`）拆出真正的 Bitable app token；不能把外层 spreadsheet token 当作 Bitable app token。

## 配置

1. 在 PowerShell 运行 `set-feishu-secret.ps1` 设置新的 App Secret。
2. 重启 Codex，使 MCP 进程加载新代码和环境变量。
3. 直接使用 Wiki 链接注册资源。若页面只嵌入一个多维表格，可以使用不带参数的裸 Wiki 链接；MCP 会自动读取 Sheets `metainfo` 并发现 `app_token/table_id`。页面包含多个表格时，再使用带 `table=tbl...` 的具体链接：

```text
https://hlinfobg.feishu.cn/wiki/E4rTwRZxviTjcIkVnLrca9GSnhb?table=tblcR27mUkXc3q58&view=vewLgt4u2h
```

只读发现（不写配置）可以调用 `feishu_discover_wiki_resource`；登记资源调用 `feishu_register_wiki_table`。也可以直接在 `resources.json` 中配置：

```json
{
  "contract_dev_log": {
    "title": "合同管理系统 开发日志",
    "resource_type": "bitable",
    "node_token": "E4rTwRZxviTjcIkVnLrca9GSnhb",
    "wiki_url": "https://hlinfobg.feishu.cn/wiki/E4rTwRZxviTjcIkVnLrca9GSnhb?table=tblcR27mUkXc3q58&view=vewLgt4u2h",
    "app_token": "由 Sheets metainfo 的 blockToken 拆出的 app_token",
    "table_id": "tblcR27mUkXc3q58",
    "view_id": "vewLgt4u2h"
  }
}
```

## 常用工具

- `feishu_list_resources`：查看资源类型和 token。
- `feishu_discover_wiki_resource`：从裸 Wiki 或完整表格链接自动发现真实 Bitable。
- `feishu_register_wiki_table`：从 Wiki 链接注册资源，支持自动发现唯一嵌入表格。
- `feishu_list_tables`：列出应用下的数据表。
- `feishu_bitable_list_fields`：读取字段名、字段 ID 和字段类型。
- `feishu_bitable_query_records`：读取记录，可按字段精确筛选。
- `feishu_bitable_insert_record`：插入一条记录，必须显式传 `confirm=true`。
- `feishu_bitable_insert_records`：批量插入记录，必须显式传 `confirm=true`。
- `feishu_bitable_update_record`：更新记录，必须显式传 `confirm=true`。
- `feishu_bitable_delete_records`：删除记录，必须显式传 `confirm=true`。
- `feishu_query_rows`：根据资源类型自动选择 Sheet 或 Bitable 查询。

插入前先读取字段：

```text
读取 contract_dev_log 的字段定义
```

然后按字段名插入：

```text
向 contract_dev_log 插入一条记录，字段为：{"字段名": "字段值"}
```

旧的 `feishu_append_rows` 仅用于普通电子表格；对 Bitable 会主动报错并提示使用 `feishu_bitable_insert_record`，避免把字段对象误当成二维单元格。

`.env` 和 `resources.json` 已加入 `.gitignore`，不要把 Secret 写入代码、README 或提交记录。

如果查询成功但写入返回 `91403 Forbidden`，说明应用已能读取资源，但还没有新增/编辑记录权限。需要同时开通多维表格写权限，并在目标表格的文档应用权限中添加该应用。
