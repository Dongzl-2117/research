"""值班检查时，从配置的读者入口核对 /health；部署回退独立看源站。"""

import json
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def public_health_notice(base_url: str, expected_commit: str) -> str:
    try:
        request = Request(
            base_url.rstrip("/") + "/health",
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "ResearchLibrary-Health/1.0",
            },
        )
        with urlopen(request, timeout=5) as response:
            data = json.loads(response.read(4096))
    except HTTPError as exc:
        exc.close()
        return f"公网入口健康检查失败（HTTP {exc.code}）"
    except ValueError:
        return "公网入口未返回可识别的健康状态"
    except (OSError, HTTPException) as exc:
        return f"从本站访问公网入口失败：{exc}"

    if not isinstance(data, dict) or not isinstance(data.get("commit"), str) or not data["commit"]:
        return "公网入口未返回可识别的健康状态"
    if data.get("status") != "ok":
        return "公网入口报告应用未就绪"
    commit = data["commit"]
    if expected_commit and expected_commit != "unknown" and commit != expected_commit:
        return f"公网入口报出的提交 {commit[:12]} 与本机镜像 {expected_commit[:12]} 不一致"
    return ""
