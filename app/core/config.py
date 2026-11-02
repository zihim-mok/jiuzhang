"""外部配置加载。

配置来源优先级：环境变量 > .env 文件 > config.yaml > 默认值。
密钥只从环境变量/.env/配置文件读取，禁止硬编码、禁止写入日志。

占位符纪律（2026-10-21）：`api_key` 若仍是占位文本（REPLACE_ME / YOUR_KEY / <...> 等），
视为**未配置**——不发起任何网络请求，healthz 明确提示「请替换」，避免"看起来配了其实一通 401"。
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # 项目根目录
CONFIG_PATH = Path(os.environ.get("APP_CONFIG", BASE_DIR / "config.yaml"))

# 占位符识别：命中即视为"还没填真 key"
_PLACEHOLDER_RE = re.compile(r"REPLACE|YOUR[_ ]?|<[^>]*>|PLACEHOLDER|EXAMPLE|CHANGEME|TODO|\bxxx+\b", re.I)


def _looks_placeholder(value: Optional[str]) -> bool:
    if not value:
        return True
    v = value.strip()
    return bool(_PLACEHOLDER_RE.search(v)) or len(v) < 8


def _key_state(provider: Optional[str], api_base: Optional[str], api_key: Optional[str]) -> str:
    """missing（缺项）/ placeholder（占位待替换）/ ok（可用形状）。"""
    if not (provider and api_base and api_key):
        return "missing"
    return "placeholder" if _looks_placeholder(api_key) else "ok"


class ModelFallbackConfig(BaseModel):
    provider: Optional[str] = None
    model_name: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    failover_threshold: int = 3

    @property
    def configured(self) -> bool:
        return _key_state(self.provider, self.api_base, self.api_key) == "ok"


class ModelConfig(BaseModel):
    provider: Optional[str] = None
    model_name: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    timeout_seconds: float = 8.0  # 单次意图理解超时（超时即回退规则分类）
    intent_strategy: str = "ai_first"  # ai_first: 读类请求优先 AI；rule_first: 规则优先
    reply_mode: str = "on"  # on: 非执行类支持 AI 自然回复；off: 完全保留原固定模板
    reply_timeout_seconds: float = 6.0
    fallback: ModelFallbackConfig = ModelFallbackConfig()

    @property
    def key_state(self) -> str:
        return _key_state(self.provider, self.api_base, self.api_key)

    @property
    def configured(self) -> bool:
        """是否已配置真实模型；占位符不算配置（否则会空转 401）。"""
        return self.key_state == "ok"


class DatabaseConfig(BaseModel):
    path: str = "app/data/banking_sim.db"

    @property
    def resolved_path(self) -> Path:
        p = Path(self.path)
        return p if p.is_absolute() else (BASE_DIR / self.path).resolve()


class ServiceConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    environment: str = "simulation"


class AppConfig(BaseModel):
    service: ServiceConfig = ServiceConfig()
    model: ModelConfig = ModelConfig()
    database: DatabaseConfig = DatabaseConfig()

    @property
    def model_stub_mode(self) -> bool:
        """未配置真实模型 → 模拟替身模式（所有输出标注『模拟』）。"""
        return not self.model.configured


_DOTENV_KEYS: set[str] = set()   # 记录哪些键是 .env 灌进来的（用于让 config.yaml 胜出，见 _apply_env_overrides）


def _parse_dotenv_value(raw: str) -> str:
    """解析单行 .env 值：去掉未加引号的行尾注释，保留引号内的 #。

    不实现变量插值/转义等完整 dotenv 语法；遇到未闭合引号不猜测，
    把原值原样交给后续配置校验，避免悄悄启用某个敏感开关。
    """
    raw = raw.strip()
    if raw.startswith(('"', "'")):
        quote = raw[0]
        close = raw.find(quote, 1)
        if close > 0 and (not raw[close + 1:].strip() or raw[close + 1:].lstrip().startswith('#')):
            return raw[1:close]
        return raw  # 不规范的引号文本不得被当成合法 on / ai_first
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _load_dotenv() -> None:
    """读取 .env（KEY=VALUE 逐行）；**不覆盖已存在的环境变量**。

    只做最小解析：忽略空行与 # 注释，去掉成对引号；不支持多行值与变量插值（够用即止）。
    路径可用 APP_ENV_FILE 覆盖（测试用），默认项目根 `.env`。

    优先级（2026-10-21 明确）：**真实环境变量 > config.yaml > .env > 默认值**。
    即：.env 只是"还没上环境变量时的便捷替代"，不会压掉已经在 config.yaml 里写好的配置
    （否则留一份占位 .env 会把 yaml 里的真配置顶掉 —— 这个坑在接入冒烟里被实测抓到）。
    """
    path = Path(os.environ.get("APP_ENV_FILE", BASE_DIR / ".env"))
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), _parse_dotenv_value(v)
        if k and k not in os.environ:
            os.environ[k] = v
            _DOTENV_KEYS.add(k)


def _pick_env(name: str, yaml_value, yaml_explicit: bool = False) -> Optional[str]:
    """优先级：真实环境变量 > 显式 config.yaml > .env > 程序默认值。

    必须区分 "YAML 显式填写" 与 "Pydantic 的默认值"；否则 m.reply_mode
    的默认值 on 会错误地压住 .env 文件中用户明确写出的 off。
    """
    v = os.environ.get(name)
    if v is None or v == "":
        return None
    if name in _DOTENV_KEYS and yaml_explicit and yaml_value not in (None, ""):
        return None
    return v


def _apply_env_overrides(cfg: AppConfig, raw_config: dict | None = None) -> None:
    """环境变量覆盖；仅 YAML 中**显式**提供的字段可以压住 .env。"""
    raw_config = raw_config or {}
    model_yaml = raw_config.get("model") or {}
    fb_yaml = model_yaml.get("fallback") or {}
    db_yaml = raw_config.get("database") or {}

    m, fb = cfg.model, cfg.model.fallback

    def m_env(name: str, field: str) -> Optional[str]:
        return _pick_env(name, getattr(m, field), field in model_yaml)

    def fb_env(name: str, field: str) -> Optional[str]:
        return _pick_env(name, getattr(fb, field), field in fb_yaml)

    if (v := m_env("MODEL_PROVIDER", "provider")):
        m.provider = v
    if (v := m_env("MODEL_API_KEY", "api_key")):
        m.api_key = v
    if (v := m_env("MODEL_API_BASE", "api_base")):
        m.api_base = v
    if (v := m_env("MODEL_NAME", "model_name")):
        m.model_name = v
    if (v := m_env("MODEL_INTENT_STRATEGY", "intent_strategy")) and v in ("ai_first", "rule_first"):
        m.intent_strategy = v
    if (v := m_env("MODEL_REPLY_MODE", "reply_mode")) and v in ("on", "off"):
        m.reply_mode = v
    if (v := m_env("MODEL_REPLY_TIMEOUT_SECONDS", "reply_timeout_seconds")):
        try:
            m.reply_timeout_seconds = max(1.0, min(float(v), 20.0))
        except ValueError:
            pass
    if (v := fb_env("MODEL_FALLBACK_PROVIDER", "provider")):
        fb.provider = v
    if (v := fb_env("MODEL_FALLBACK_API_KEY", "api_key")):
        fb.api_key = v
    if (v := fb_env("MODEL_FALLBACK_API_BASE", "api_base")):
        fb.api_base = v
    if (v := _pick_env("DB_PATH", cfg.database.path, "path" in db_yaml)):
        cfg.database.path = v


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    _load_dotenv()
    raw: dict = {}
    if CONFIG_PATH.is_file():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    cfg = AppConfig.model_validate(raw)
    _apply_env_overrides(cfg, raw)
    return cfg
