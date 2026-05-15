# apc-sdk-python

Python SDK for APC (Application Control).

最小依赖(httpx + pyjwt + cryptography);不引入 Pydantic / loguru / typer。

## 安装

仓库内:
```toml
[tool.uv.sources]
apc-sdk-python = { path = "./apc_sdk", editable = true }
```

外部项目:
```bash
pip install "git+ssh://git@github.com/GySuper/m-zdfb.git#subdirectory=apc_sdk"
```

## 使用

完整接口和 fail-open + 7 天 grace 语义见同仓 `docs/superpowers/specs/2026-05-15-apc-sdk-integration-design.md`。

最小例子(Task 7 完成后才能跑):

```python
from pathlib import Path
from apc_sdk import ApcClient, ApcConfig, Verdict

client = ApcClient(ApcConfig(
    endpoint="https://203.0.113.5:8443",
    app_id="ap_xxxxxxxx",
    app_secret=os.environ["APC_APP_SECRET"],
    public_key=Path("license_public.pem").read_text(),
    cache_dir=Path.home() / ".cache" / "myapp" / "apc",
    cert_fingerprint=os.environ.get("APC_CERT_FP"),
))

if client.check() == Verdict.PASS:
    run_business_logic()
else:
    sys.exit(1)
```
