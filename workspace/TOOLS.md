# TOOLS.md

## Crypto APIs
- CoinGecko: `CG-DZoCE8UMF3FWpeYhBMvqGq4g` (备用，Binance 挂时用)
- 脚本: `scripts/crypto_signal_monitor.py`

## 代理
- Clash(Mihomo) 127.0.0.1:7890，Binance 必须走代理
- 订阅源: `https://hcbskjd.kunlun01.com/api/kunlun01/215db5e69fbdbaf997df089b13bc69b4`（节点配置见 `scripts/clash-config.yaml`）

## 大模型（与 `/root/配置.md` 一致）
- Base URL: `https://api.deepseek.com`
- Model: `deepseek-v4-flash`（关闭深度思考）
- 网关配置: `openclaw.json` / `config.yaml` / `crypto/.env`
