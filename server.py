"""Feishu MCP for Wiki-mounted Sheets and Bitable tables.

The important distinction is that a Wiki page is only a container. A URL
with ``table=tbl...`` points to a Bitable table and must use the Bitable API;
it is not a Sheets worksheet even when the Wiki node reports ``obj_type=sheet``.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

API_BASE = os.getenv("FEISHU_API_BASE", "https://open.feishu.cn/open-apis").rstrip("/")
APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
DEFAULT_SPREADSHEET_TOKEN = os.getenv("FEISHU_SPREADSHEET_TOKEN", "").strip()
DEFAULT_SHEET_ID = os.getenv("FEISHU_SHEET_ID", "").strip()
RESOURCE_FILE = Path(os.getenv("FEISHU_RESOURCES_FILE", "resources.json"))
if not RESOURCE_FILE.is_absolute():
    # Keep registration deterministic when an MCP host launches the server
    # from an unrelated working directory.
    RESOURCE_FILE = Path(__file__).resolve().parent / RESOURCE_FILE
TOKEN_CACHE: dict[str, Any] = {"value": None, "expires_at": 0.0}
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

mcp = FastMCP("feishu-spreadsheet")


class FeishuError(RuntimeError):
    """An error returned by the Feishu Open API."""


def _required(value: str, name: str) -> str:
    if not value:
        raise FeishuError(f"缺少配置 {name}，请填写本机 .env 文件")
    return value


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuError(f"飞书接口返回了非 JSON 响应，HTTP {response.status_code}") from exc
    if response.status_code >= 400:
        if response.status_code == 403 and payload.get("code") == 91403:
            raise FeishuError(
                "飞书拒绝写入（91403 Forbidden）：请在开放平台为应用开通多维表格新增记录/编辑权限，"
                "并在目标多维表格的文档应用权限中添加该应用"
            )
        raise FeishuError(f"飞书接口请求失败，HTTP {response.status_code}，{payload}")
    if payload.get("code", 0) != 0:
        raise FeishuError(f"飞书接口返回错误 {payload.get('code')}：{payload.get('msg', '未知错误')}")
    return payload.get("data", {})


async def _tenant_access_token() -> str:
    now = time.time()
    if TOKEN_CACHE.get("value") and now < float(TOKEN_CACHE.get("expires_at", 0)):
        return str(TOKEN_CACHE["value"])
    app_id = _required(APP_ID, "FEISHU_APP_ID")
    app_secret = _required(APP_SECRET, "FEISHU_APP_SECRET")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            f"{API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
    try:
        auth_payload = response.json()
    except ValueError as exc:
        raise FeishuError(f"飞书鉴权接口返回了非 JSON 响应，HTTP {response.status_code}") from exc
    if response.status_code >= 400 or auth_payload.get("code", 0) != 0:
        raise FeishuError(
            f"飞书鉴权失败，HTTP {response.status_code}，{auth_payload.get('msg', auth_payload)}"
        )
    token = str(auth_payload.get("tenant_access_token", "")).strip()
    if not token:
        raise FeishuError(f"飞书鉴权响应缺少 tenant_access_token：{auth_payload}")
    TOKEN_CACHE["value"] = token
    TOKEN_CACHE["expires_at"] = now + max(60, int(auth_payload.get("expire", 7200)) - 120)
    return token


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = await _tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.request(
            method,
            f"{API_BASE}{path}",
            headers=headers,
            params=params,
            json=body,
        )
    return _decode_response(response)


def _spreadsheet(token: str | None) -> str:
    return _required((token or DEFAULT_SPREADSHEET_TOKEN).strip(), "FEISHU_SPREADSHEET_TOKEN")


def _sheet(sheet_id: str | None) -> str:
    return _required((sheet_id or DEFAULT_SHEET_ID).strip(), "FEISHU_SHEET_ID")


def _load_resources() -> dict[str, dict[str, Any]]:
    if not RESOURCE_FILE.exists():
        return {}
    try:
        data = json.loads(RESOURCE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeishuError(f"无法读取资源配置 {RESOURCE_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise FeishuError(f"资源配置 {RESOURCE_FILE} 必须是 JSON 对象")
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _save_resources(resources: dict[str, dict[str, Any]]) -> None:
    RESOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = RESOURCE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(resources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(RESOURCE_FILE)


def _resource(alias: str) -> dict[str, Any]:
    config = _load_resources().get(alias.strip(), {})
    if not config:
        raise FeishuError(f"未找到资源别名 {alias}，请先配置 resources.json")
    return config


def _resource_type(config: dict[str, Any]) -> str:
    if str(config.get("resource_type", "")).lower() in {"bitable", "table", "multi_table"}:
        return "bitable"
    if config.get("table_id"):
        return "bitable"
    return "sheet"


def _resolve_sheet_resource(resource: str, spreadsheet_token: str, sheet_id: str) -> tuple[str, str, dict[str, Any]]:
    config = _resource(resource) if resource.strip() else {}
    token = spreadsheet_token.strip() or str(config.get("spreadsheet_token", config.get("app_token", "")))
    sheet = sheet_id.strip() or str(config.get("sheet_id", ""))
    return _spreadsheet(token), _sheet(sheet), config


def _resolve_bitable_resource(
    resource: str,
    app_token: str,
    table_id: str,
    view_id: str = "",
) -> tuple[str, str, str, dict[str, Any]]:
    config = _resource(resource) if resource.strip() else {}
    token = app_token.strip() or str(config.get("app_token", ""))
    table = table_id.strip() or str(config.get("table_id", ""))
    view = view_id.strip() or str(config.get("view_id", ""))
    return _required(token, "app_token"), _required(table, "table_id"), view, config


def _full_range(sheet_id: str, cell_range: str) -> str:
    value = cell_range.strip()
    return value if "!" in value else f"{sheet_id}!{value}"


def _parse_wiki_url(wiki_url: str, *, require_table: bool = False) -> dict[str, str]:
    parsed = urlparse(wiki_url.strip())
    if not parsed.path or "/wiki/" not in parsed.path:
        raise FeishuError("wiki_url 必须是飞书知识库链接")
    node_token = parsed.path.rstrip("/").split("/wiki/", 1)[1].split("/", 1)[0]
    query = parse_qs(parsed.query)
    table_id = (query.get("table", [""])[0] or "").strip()
    view_id = (query.get("view", [""])[0] or "").strip()
    sheet_id = (query.get("sheet", [""])[0] or "").strip()
    if not node_token:
        raise FeishuError("wiki_url 缺少节点 token")
    if require_table and not table_id:
        raise FeishuError("wiki_url 缺少 table 参数；请使用包含 table=tbl... 的表格链接")
    if table_id and not re.fullmatch(r"tbl[A-Za-z0-9]+", table_id):
        raise FeishuError(f"table 参数不是有效的多维表格 table_id：{table_id}")
    return {"node_token": node_token, "table_id": table_id, "view_id": view_id, "sheet_id": sheet_id}


def _field_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in data.get("items", []) if isinstance(item, dict)]


def _record_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in data.get("items", []) if isinstance(item, dict)]


def _normalize_bitable_fields(field_items: list[dict[str, Any]], fields: dict[str, Any]) -> dict[str, Any]:
    """Accept field names or IDs, but send the API field names it requires."""
    id_to_name = {
        str(field.get("field_id")): str(field.get("field_name"))
        for field in field_items
        if field.get("field_id") and field.get("field_name")
    }
    allowed_names = {
        str(field.get("field_name")) for field in field_items if field.get("field_name")
    }
    allowed_ids = set(id_to_name)
    unknown = sorted(key for key in fields if key not in allowed_names and key not in allowed_ids)
    if unknown:
        raise FeishuError(f"字段不存在：{unknown}。请先调用 feishu_bitable_list_fields")
    return {id_to_name.get(key, key): value for key, value in fields.items()}


def _find_embedded_bitable_token(value: Any, table_id: str) -> str:
    """Find an embedded Base token in Sheets metainfo without guessing blindly."""
    if isinstance(value, dict):
        joined = " ".join(str(value.get(key, "")) for key in ("tableId", "table_id", "tableToken", "table_token"))
        if table_id and table_id in joined:
            for key in ("appToken", "app_token", "blockToken", "block_token", "token"):
                candidate = str(value.get(key, "")).strip()
                if candidate and candidate != table_id:
                    return candidate
        for child in value.values():
            found = _find_embedded_bitable_token(child, table_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_embedded_bitable_token(child, table_id)
            if found:
                return found
    elif isinstance(value, str) and table_id:
        marker = f"_{table_id}"
        if value.endswith(marker) and value[: -len(marker)]:
            return value[: -len(marker)]
    return ""


def _extract_embedded_bitable_refs(metadata: dict[str, Any]) -> list[dict[str, str]]:
    """Extract embedded Bitable references from Sheets metainfo.

    Sheets returns a BITABLE_BLOCK token in the form ``app_token_table_id``.
    This is the stable bridge needed when the user supplies only a Wiki URL.
    """
    refs: list[dict[str, str]] = []
    for sheet in metadata.get("sheets", []) if isinstance(metadata, dict) else []:
        if not isinstance(sheet, dict):
            continue
        block_info = sheet.get("blockInfo") or sheet.get("block_info") or {}
        if not isinstance(block_info, dict):
            continue
        block_type = str(block_info.get("blockType", block_info.get("block_type", ""))).upper()
        block_token = str(block_info.get("blockToken", block_info.get("block_token", ""))).strip()
        if block_type != "BITABLE_BLOCK" or not block_token or "_" not in block_token:
            continue
        app_token, table_id = block_token.rsplit("_", 1)
        if not app_token or not re.fullmatch(r"tbl[A-Za-z0-9]+", table_id):
            continue
        refs.append(
            {
                "app_token": app_token,
                "table_id": table_id,
                "sheet_id": str(sheet.get("sheetId", sheet.get("sheet_id", ""))),
                "block_token": block_token,
                "title": str(sheet.get("title", sheet.get("name", ""))),
            }
        )
    return refs


async def _discover_wiki_resource(wiki_url: str) -> dict[str, Any]:
    """Resolve a bare Wiki URL to its embedded Bitable (read-only discovery)."""
    parsed = _parse_wiki_url(wiki_url)
    node = await feishu_resolve_wiki_node(parsed["node_token"])
    obj_type = str(node.get("obj_type", "")).lower()
    obj_token = str(node.get("obj_token", "")).strip()
    if obj_type == "bitable":
        table_id = parsed["table_id"]
        if not table_id:
            raise FeishuError("Wiki 节点是多维表格，但链接缺少 table 参数；请提供包含 table=tbl... 的链接")
        return {
            "node": node,
            "node_token": parsed["node_token"],
            "app_token": obj_token,
            "table_id": table_id,
            "view_id": parsed["view_id"],
            "source_obj_type": obj_type,
            "source_spreadsheet_token": "",
            "embedded_bitable_refs": [],
        }
    if obj_type != "sheet":
        raise FeishuError(f"不支持的 Wiki 节点类型：{node.get('obj_type')}")
    metadata = await feishu_get_sheet_metadata(spreadsheet_token=obj_token, table_id=parsed["table_id"])
    refs = metadata.get("embedded_bitable_refs", [])
    if parsed["table_id"]:
        refs = [ref for ref in refs if ref.get("table_id") == parsed["table_id"]]
    elif parsed["sheet_id"]:
        refs = [ref for ref in refs if ref.get("sheet_id") == parsed["sheet_id"]]
    if not refs:
        raise FeishuError("未在该 Wiki 电子表格元数据中找到嵌入多维表格；请确认链接或授权")
    if len(refs) > 1:
        raise FeishuError("该 Wiki 页面包含多个嵌入多维表格，请提供带 table=tbl... 的具体表格链接")
    ref = refs[0]
    return {
        "node": node,
        "node_token": parsed["node_token"],
        "app_token": ref["app_token"],
        "table_id": ref["table_id"],
        "view_id": parsed["view_id"],
        "sheet_id": parsed["sheet_id"],
        "source_obj_type": obj_type,
        "source_spreadsheet_token": obj_token,
        "embedded_bitable_refs": refs,
    }


@mcp.tool()
async def feishu_resolve_wiki_node(node_token: str) -> dict[str, Any]:
    """将知识库节点 token 解析为真实文档 token。"""
    node_token = node_token.strip()
    if not node_token:
        raise FeishuError("node_token 不能为空")
    data = await _request("GET", "/wiki/v2/spaces/get_node", params={"token": node_token})
    node = data.get("node", {})
    return {
        "node_token": node.get("node_token"),
        "obj_token": node.get("obj_token"),
        "obj_type": node.get("obj_type"),
        "title": node.get("title"),
    }


@mcp.tool()
async def feishu_discover_wiki_resource(wiki_url: str) -> dict[str, Any]:
    """只读解析 Wiki 链接，自动发现其嵌入的 Bitable app_token/table_id。"""
    discovered = await _discover_wiki_resource(wiki_url)
    return {
        "node": discovered["node"],
        "node_token": discovered["node_token"],
        "source_obj_type": discovered["source_obj_type"],
        "source_spreadsheet_token": discovered["source_spreadsheet_token"],
        "app_token": discovered["app_token"],
        "table_id": discovered["table_id"],
        "view_id": discovered["view_id"],
        "sheet_id": discovered.get("sheet_id", ""),
        "embedded_bitable_refs": discovered["embedded_bitable_refs"],
    }


@mcp.tool()
async def feishu_list_resources() -> dict[str, Any]:
    """列出本地资源，明确区分 sheet 和 bitable。"""
    items = []
    for alias, config in _load_resources().items():
        items.append(
            {
                "alias": alias,
                "title": config.get("title", alias),
                "resource_type": _resource_type(config),
                "node_token": config.get("node_token", ""),
                "source_obj_type": config.get("source_obj_type", ""),
                "source_spreadsheet_token": config.get("source_spreadsheet_token", ""),
                "source_sheet_id": config.get("source_sheet_id", config.get("sheet_id", "")),
                "app_token": config.get("app_token", config.get("spreadsheet_token", "")),
                "table_id": config.get("table_id", ""),
                "view_id": config.get("view_id", ""),
                "sheet_id": config.get("sheet_id", ""),
                "default_range": config.get("default_range", ""),
            }
        )
    return {"resource_file": str(RESOURCE_FILE), "items": items}


def _validate_alias(alias: str) -> str:
    alias = alias.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias):
        raise FeishuError("alias 只能包含字母、数字、下划线和短横线")
    return alias


@mcp.tool()
async def feishu_register_resource(
    alias: str,
    spreadsheet_token: str,
    sheet_id: str,
    title: str = "",
    default_range: str = "A1:Z1000",
) -> dict[str, Any]:
    """注册传统电子表格资源，兼容旧配置。"""
    alias = _validate_alias(alias)
    token = _required(spreadsheet_token.strip(), "spreadsheet_token")
    sheet = _required(sheet_id.strip(), "sheet_id")
    resources = _load_resources()
    resources[alias] = {
        "title": title.strip() or alias,
        "resource_type": "sheet",
        "spreadsheet_token": token,
        "sheet_id": sheet,
        "default_range": default_range.strip() or "A1:Z1000",
    }
    _save_resources(resources)
    return {"alias": alias, "resource_file": str(RESOURCE_FILE), "saved": True}


@mcp.tool()
async def feishu_register_wiki_table(alias: str, wiki_url: str, title: str = "") -> dict[str, Any]:
    """从 Wiki 链接注册 Bitable；可自动发现唯一的嵌入表格。"""
    alias = _validate_alias(alias)
    discovered = await _discover_wiki_resource(wiki_url)
    node = discovered["node"]
    parsed = _parse_wiki_url(wiki_url)
    app_token = str(discovered["app_token"])
    table_id = str(discovered["table_id"])
    resources = _load_resources()
    resources[alias] = {
        "title": title.strip() or str(node.get("title") or alias),
        "resource_type": "bitable",
        "node_token": discovered["node_token"],
        "wiki_url": wiki_url.strip(),
        "source_obj_type": discovered["source_obj_type"],
        "source_spreadsheet_token": discovered["source_spreadsheet_token"],
        "source_sheet_id": discovered.get("sheet_id", ""),
        "app_token": app_token,
        "table_id": table_id,
        "view_id": parsed["view_id"],
        "sheet_id": discovered.get("sheet_id", ""),
    }
    _save_resources(resources)
    return {
        "alias": alias,
        "resource_type": "bitable",
        "app_token": app_token,
        "table_id": table_id,
        "view_id": parsed["view_id"],
        "resource_file": str(RESOURCE_FILE),
        "saved": True,
    }


@mcp.tool()
async def feishu_register_wiki_resource(
    alias: str,
    node_token: str = "",
    sheet_id: str = "",
    title: str = "",
    default_range: str = "A1:Z1000",
    table_id: str = "",
    view_id: str = "",
    wiki_url: str = "",
) -> dict[str, Any]:
    """兼容旧工具；有 table_id/wiki_url 时按 Bitable 注册，否则按 Sheet 注册。"""
    if wiki_url:
        return await feishu_register_wiki_table(alias, wiki_url, title)
    if table_id:
        alias = _validate_alias(alias)
        node = await feishu_resolve_wiki_node(node_token)
        node_obj_token = str(node.get("obj_token", "")).strip()
        if str(node.get("obj_type", "")).lower() == "bitable":
            app_token = _required(node_obj_token, "Wiki 节点 obj_token")
        elif str(node.get("obj_type", "")).lower() == "sheet":
            metadata = await feishu_get_sheet_metadata(
                spreadsheet_token=node_obj_token,
                table_id=table_id.strip(),
            )
            app_token = _required(
                str(metadata.get("embedded_bitable_app_token", "")).strip(),
                "嵌入多维表格 app_token",
            )
        else:
            raise FeishuError(f"不支持的 Wiki 节点类型：{node.get('obj_type')}")
        resources = _load_resources()
        resources[alias] = {
            "title": title.strip() or str(node.get("title") or alias),
            "resource_type": "bitable",
            "node_token": node_token.strip(),
            "source_obj_type": str(node.get("obj_type", "")),
            "source_spreadsheet_token": node_obj_token if str(node.get("obj_type", "")).lower() == "sheet" else "",
            "app_token": app_token,
            "table_id": table_id.strip(),
            "view_id": view_id.strip(),
        }
        _save_resources(resources)
        return {"alias": alias, "resource_type": "bitable", "saved": True}
    if not node_token or not sheet_id:
        raise FeishuError("注册 Sheet 需要 node_token 和 sheet_id；注册多维表格请传 table_id 或 wiki_url")
    node = await feishu_resolve_wiki_node(node_token)
    return await feishu_register_resource(
        alias=alias,
        spreadsheet_token=str(node.get("obj_token", "")),
        sheet_id=sheet_id,
        title=title or str(node.get("title", alias)),
        default_range=default_range,
    )


@mcp.tool()
async def feishu_remove_resource(alias: str, confirm: bool = False) -> dict[str, Any]:
    """删除本地资源配置，不会删除飞书数据。"""
    if not confirm:
        raise FeishuError("删除本地配置需要显式传入 confirm=true")
    resources = _load_resources()
    if alias not in resources:
        raise FeishuError(f"未找到资源别名 {alias}")
    del resources[alias]
    _save_resources(resources)
    return {"alias": alias, "removed": True}


@mcp.tool()
async def feishu_list_sheets(resource: str = "", spreadsheet_token: str = "") -> dict[str, Any]:
    """列出传统电子表格中的工作表。"""
    config = _resource(resource) if resource.strip() else {}
    if config and _resource_type(config) == "bitable":
        raise FeishuError("该资源是 Bitable，请使用 feishu_list_tables 或 feishu_bitable_list_fields")
    token = _spreadsheet(spreadsheet_token.strip() or str(config.get("spreadsheet_token", "")))
    return await _request("GET", f"/sheets/v3/spreadsheets/{token}/sheets/query")


@mcp.tool()
async def feishu_get_sheet_metadata(
    resource: str = "",
    spreadsheet_token: str = "",
    table_id: str = "",
) -> dict[str, Any]:
    """读取 Sheets 元数据，并在嵌入 Bitable 时返回其 app_token。"""
    config = _resource(resource) if resource.strip() else {}
    token = spreadsheet_token.strip() or str(
        config.get("source_spreadsheet_token", config.get("spreadsheet_token", config.get("app_token", "")))
    )
    token = _spreadsheet(token)
    data = await _request("GET", f"/sheets/v2/spreadsheets/{token}/metainfo")
    target_table = table_id.strip() or str(config.get("table_id", ""))
    embedded_refs = _extract_embedded_bitable_refs(data)
    embedded_app_token = _find_embedded_bitable_token(data, target_table) if target_table else ""
    if target_table:
        matching = [ref for ref in embedded_refs if ref.get("table_id") == target_table]
        if matching:
            embedded_app_token = matching[0]["app_token"]
    elif len(embedded_refs) == 1:
        target_table = embedded_refs[0]["table_id"]
        embedded_app_token = embedded_refs[0]["app_token"]
    return {
        "spreadsheet_token": token,
        "table_id": target_table,
        "embedded_bitable_app_token": embedded_app_token,
        "embedded_bitable_refs": embedded_refs,
        "metadata": data,
    }


@mcp.tool()
async def feishu_list_tables(resource: str = "", app_token: str = "") -> dict[str, Any]:
    """列出 Bitable 应用中的数据表。"""
    token = app_token.strip()
    if resource.strip():
        config = _resource(resource)
        token = token or str(config.get("app_token", ""))
    token = _required(token, "app_token")
    return await _request("GET", f"/bitable/v1/apps/{token}/tables", params={"page_size": 100})


@mcp.tool()
async def feishu_bitable_list_fields(
    resource: str = "",
    app_token: str = "",
    table_id: str = "",
) -> dict[str, Any]:
    """读取 Bitable 字段定义，插入前先用它确认字段名。"""
    token, table, _, _ = _resolve_bitable_resource(resource, app_token, table_id)
    data = await _request(
        "GET",
        f"/bitable/v1/apps/{token}/tables/{table}/fields",
        params={"page_size": 100},
    )
    return {"app_token": token, "table_id": table, "fields": _field_items(data), "raw": data}


@mcp.tool()
async def feishu_bitable_query_records(
    resource: str = "",
    app_token: str = "",
    table_id: str = "",
    view_id: str = "",
    page_size: int = 500,
    page_token: str = "",
    where_column: str = "",
    where_value: str = "",
) -> dict[str, Any]:
    """查询 Bitable 记录，返回 record_id 和字段对象。"""
    token, table, configured_view, _ = _resolve_bitable_resource(resource, app_token, table_id, view_id)
    page_size = max(1, min(page_size, 500))
    params: dict[str, Any] = {"page_size": page_size}
    selected_view = view_id.strip() or configured_view
    if selected_view:
        params["view_id"] = selected_view
    if page_token.strip():
        params["page_token"] = page_token.strip()
    data = await _request("GET", f"/bitable/v1/apps/{token}/tables/{table}/records", params=params)
    records = _record_items(data)
    if where_column:
        records = [
            item
            for item in records
            if str((item.get("fields") or {}).get(where_column, "")) == where_value
        ]
    fields_data = await _request(
        "GET",
        f"/bitable/v1/apps/{token}/tables/{table}/fields",
        params={"page_size": 100},
    )
    return {
        "resource_type": "bitable",
        "app_token": token,
        "table_id": table,
        "view_id": selected_view,
        "fields": _field_items(fields_data),
        "records": records,
        "page_token": data.get("page_token", ""),
        "has_more": bool(data.get("has_more", False)),
    }


@mcp.tool()
async def feishu_bitable_insert_records(
    records: list[dict[str, Any]],
    confirm: bool = False,
    resource: str = "",
    app_token: str = "",
    table_id: str = "",
) -> dict[str, Any]:
    """批量插入 Bitable 记录；每项是字段名到值的对象。"""
    if not confirm:
        raise FeishuError("写入 Bitable 需要显式传入 confirm=true")
    if not records or len(records) > 500:
        raise FeishuError("records 必须包含 1 到 500 条记录")
    if any(not isinstance(record, dict) or not record for record in records):
        raise FeishuError("records 中每项必须是非空 JSON 对象")
    token, table, _, _ = _resolve_bitable_resource(resource, app_token, table_id)
    fields_data = await _request(
        "GET",
        f"/bitable/v1/apps/{token}/tables/{table}/fields",
        params={"page_size": 100},
    )
    field_items = _field_items(fields_data)
    normalized_records = [
        _normalize_bitable_fields(field_items, record) for record in records
    ]
    data = await _request(
        "POST",
        f"/bitable/v1/apps/{token}/tables/{table}/records/batch_create",
        body={"records": [{"fields": record} for record in normalized_records]},
    )
    return {"resource_type": "bitable", "app_token": token, "table_id": table, **data}


@mcp.tool()
async def feishu_bitable_insert_record(
    fields: dict[str, Any],
    confirm: bool = False,
    resource: str = "",
    app_token: str = "",
    table_id: str = "",
) -> dict[str, Any]:
    """插入一条 Bitable 记录；先读取字段定义并校验字段名。"""
    if not confirm:
        raise FeishuError("写入 Bitable 需要显式传入 confirm=true")
    if not isinstance(fields, dict) or not fields:
        raise FeishuError("fields 必须是非空 JSON 对象")
    token, table, _, _ = _resolve_bitable_resource(resource, app_token, table_id)
    fields_data = await _request(
        "GET",
        f"/bitable/v1/apps/{token}/tables/{table}/fields",
        params={"page_size": 100},
    )
    field_items = _field_items(fields_data)
    normalized_fields = _normalize_bitable_fields(field_items, fields)
    data = await _request(
        "POST",
        f"/bitable/v1/apps/{token}/tables/{table}/records",
        body={"fields": normalized_fields},
    )
    return {"resource_type": "bitable", "app_token": token, "table_id": table, **data}


@mcp.tool()
async def feishu_bitable_update_record(
    record_id: str,
    fields: dict[str, Any],
    confirm: bool = False,
    resource: str = "",
    app_token: str = "",
    table_id: str = "",
) -> dict[str, Any]:
    """更新一条 Bitable 记录。"""
    if not confirm:
        raise FeishuError("修改 Bitable 需要显式传入 confirm=true")
    if not record_id.strip() or not fields:
        raise FeishuError("record_id 和 fields 不能为空")
    token, table, _, _ = _resolve_bitable_resource(resource, app_token, table_id)
    data = await _request(
        "PUT",
        f"/bitable/v1/apps/{token}/tables/{table}/records/{record_id.strip()}",
        body={"fields": fields},
    )
    return {"resource_type": "bitable", "app_token": token, "table_id": table, **data}


@mcp.tool()
async def feishu_bitable_delete_records(
    record_ids: list[str],
    confirm: bool = False,
    resource: str = "",
    app_token: str = "",
    table_id: str = "",
) -> dict[str, Any]:
    """删除 Bitable 记录。"""
    if not confirm:
        raise FeishuError("删除 Bitable 记录需要显式传入 confirm=true")
    ids = [str(record_id).strip() for record_id in record_ids if str(record_id).strip()]
    if not ids or len(ids) > 500:
        raise FeishuError("record_ids 必须包含 1 到 500 个 record_id")
    token, table, _, _ = _resolve_bitable_resource(resource, app_token, table_id)
    data = await _request(
        "POST",
        f"/bitable/v1/apps/{token}/tables/{table}/records/batch_delete",
        body={"records": ids},
    )
    return {"resource_type": "bitable", "app_token": token, "table_id": table, **data}


@mcp.tool()
async def feishu_read_range(
    cell_range: str = "",
    resource: str = "",
    sheet_id: str = "",
    spreadsheet_token: str = "",
) -> dict[str, Any]:
    """读取传统电子表格单元格范围。"""
    token, resolved_sheet, config = _resolve_sheet_resource(resource, spreadsheet_token, sheet_id)
    if config and _resource_type(config) == "bitable":
        raise FeishuError("该资源是 Bitable，不能读取单元格范围，请使用 feishu_bitable_query_records")
    cell_range = cell_range.strip() or str(config.get("default_range", ""))
    cell_range = _required(cell_range, "cell_range")
    return await _request(
        "GET",
        f"/sheets/v2/spreadsheets/{token}/values/{_full_range(resolved_sheet, cell_range)}",
    )


@mcp.tool()
async def feishu_query_rows(
    cell_range: str = "",
    where_column: str = "",
    where_value: str = "",
    resource: str = "",
    sheet_id: str = "",
    spreadsheet_token: str = "",
) -> dict[str, Any]:
    """按资源类型查询：Sheet 返回表头行对象，Bitable 返回记录对象。"""
    config = _resource(resource) if resource.strip() else {}
    if config and _resource_type(config) == "bitable":
        return await feishu_bitable_query_records(
            resource=resource, where_column=where_column, where_value=where_value
        )
    data = await feishu_read_range(
        cell_range=cell_range, resource=resource, sheet_id=sheet_id, spreadsheet_token=spreadsheet_token
    )
    value_range = data.get("valueRange", data)
    values = value_range.get("values", [])
    if not values:
        return {"headers": [], "rows": []}
    headers = [str(value) if value is not None else "" for value in values[0]]
    rows: list[dict[str, Any]] = []
    for offset, values_row in enumerate(values[1:], start=2):
        row = {headers[index]: value for index, value in enumerate(values_row) if index < len(headers)}
        if where_column and str(row.get(where_column, "")) != where_value:
            continue
        row["_row_number"] = offset
        rows.append(row)
    return {"headers": headers, "rows": rows}


@mcp.tool()
async def feishu_append_rows(
    values: list[list[Any]],
    start_cell: str = "A1",
    insert_data_option: str = "INSERT_ROWS",
    resource: str = "",
    sheet_id: str = "",
    spreadsheet_token: str = "",
) -> dict[str, Any]:
    """追加传统电子表格行；Bitable 请使用 feishu_bitable_insert_record。"""
    if not values or len(values) > 5000:
        raise FeishuError("values 必须包含 1 到 5000 行")
    config = _resource(resource) if resource.strip() else {}
    if config and _resource_type(config) == "bitable":
        raise FeishuError("该资源是 Bitable，不能用二维单元格 values 插入；请先读取字段，再调用 feishu_bitable_insert_record")
    token, resolved_sheet, _ = _resolve_sheet_resource(resource, spreadsheet_token, sheet_id)
    if insert_data_option not in {"OVERWRITE", "INSERT_ROWS"}:
        raise FeishuError("insert_data_option 只能是 OVERWRITE 或 INSERT_ROWS")
    return await _request(
        "POST",
        f"/sheets/v2/spreadsheets/{token}/values_append",
        params={"insertDataOption": insert_data_option},
        body={"valueRange": {"range": f"{resolved_sheet}!{start_cell}", "values": values}},
    )


@mcp.tool()
async def feishu_update_range(
    cell_range: str,
    values: list[list[Any]],
    confirm: bool = False,
    resource: str = "",
    sheet_id: str = "",
    spreadsheet_token: str = "",
) -> dict[str, Any]:
    """覆盖传统电子表格范围。"""
    if not confirm:
        raise FeishuError("修改操作需要显式传入 confirm=true")
    token, resolved_sheet, config = _resolve_sheet_resource(resource, spreadsheet_token, sheet_id)
    if config and _resource_type(config) == "bitable":
        raise FeishuError("该资源是 Bitable，请使用 feishu_bitable_update_record")
    return await _request(
        "PUT",
        f"/sheets/v2/spreadsheets/{token}/values",
        body={"valueRange": {"range": _full_range(resolved_sheet, cell_range), "values": values}},
    )


@mcp.tool()
async def feishu_delete_rows(
    start_index: int,
    end_index: int,
    confirm: bool = False,
    resource: str = "",
    sheet_id: str = "",
    spreadsheet_token: str = "",
) -> dict[str, Any]:
    """删除传统电子表格行。"""
    if not confirm:
        raise FeishuError("删除操作需要显式传入 confirm=true")
    if start_index < 1 or end_index < start_index:
        raise FeishuError("行号范围无效，start_index 和 end_index 从 1 开始")
    if end_index - start_index + 1 > 5000:
        raise FeishuError("单次最多删除 5000 行")
    token, resolved_sheet, config = _resolve_sheet_resource(resource, spreadsheet_token, sheet_id)
    if config and _resource_type(config) == "bitable":
        raise FeishuError("该资源是 Bitable，请使用 feishu_bitable_delete_records")
    return await _request(
        "DELETE",
        f"/sheets/v2/spreadsheets/{token}/dimension_range",
        body={
            "dimension": {
                "sheetId": resolved_sheet,
                "majorDimension": "ROWS",
                "startIndex": start_index,
                "endIndex": end_index,
            }
        },
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
