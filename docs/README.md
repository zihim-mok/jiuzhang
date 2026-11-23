# GitHub Pages 展示页

本目录只服务一个用途：把「九账算术」的前端交互界面以纯静态方式展示出来，供评审与外部查看。

## 文件说明

| 文件 | 说明 |
|---|---|
| `index.html` | 部署副本，内容来自 `frontend/web/standalone.html` |
| `.nojekyll` | 让 GitHub Pages 原样发布静态文件，不走 Jekyll 构建 |

## 为什么能纯静态运行

`standalone.html` 内置了一层**离线回放层**（位于文件末尾的独立 `<script>` 段）：

- 声明 `window.__OFFLINE__ = true`
- 内嵌 `window.__FIXTURES__` —— 从真实后端抓取的接口响应
- 重写 `window.fetch`，命中夹具即返回，不发起网络请求

因此浏览器里直接打开或用静态托管都能完整点击，**不需要 Python 后端、不需要端口、不需要网络**。

## 边界（重要）

这是**回放**，不是**在线服务**。

- 页面底部有常驻横幅标注「离线回放模式」，数据抓取日期为 2026-11-02
- 交互可点、页面可切换，但**不会产生新的业务数据**，不会真的建单、转账或退订
- 未预置的请求会返回提示，引导到本地启动服务后访问 `/ui`

要体验带真实规则引擎与确认单流程的版本，请在本地按 `README.md` 启动服务，访问 `http://localhost:8000/ui`。

## 同步方式

`index.html` 是 `frontend/web/standalone.html` 的逐字节副本，两份文件内容完全相同，更新时直接覆盖即可：

```bash
cp frontend/web/standalone.html docs/index.html
```

注：`standalone.html` 内注释提到的 `scripts/build_offline.py` 是夹具注入工具，未随本仓库提供；夹具数据已内嵌在 HTML 中，查看演示不需要该脚本。

## 访问地址

https://zihim-mok.github.io/jiuzhang/
