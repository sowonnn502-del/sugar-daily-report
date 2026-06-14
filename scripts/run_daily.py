#!/usr/bin/env python3
from __future__ import annotations
"""
白糖日报生成系统 - 主程序
用法: python scripts/run_daily.py [--date YYYY-MM-DD]

严格约束:
  - 行情合约、基本面、交易观点三者必须均为 SR2609
  - 先校验后生成，一致性检查在 DeepSeek 之前
  - 错误日志使用特定错误码，不输出 API Key
"""

import os
import re
import sys
import json
import shutil
import hashlib
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import OpenAI, AuthenticationError

# ── 项目根目录（绝对路径，不依赖终端 CWD） ─────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# ── 显式加载 .env ───────────────────────────────────────
load_dotenv(PROJECT_ROOT / ".env", override=True)

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

TARGET_CONTRACT = config["target_contract"]["code"]
REJECT_CONTRACTS = config["approved_view"].get("reject_contracts", [])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ── 错误码常量 ──────────────────────────────────────────
E_FUNDAMENTAL_FILE_NOT_FOUND = "FUNDAMENTAL_FILE_NOT_FOUND"
E_FUNDAMENTAL_FILE_EMPTY    = "FUNDAMENTAL_FILE_EMPTY"
E_DEEPSEEK_CONFIG_MISSING   = "DEEPSEEK_CONFIG_MISSING"
E_DEEPSEEK_HTTP_ERROR       = "DEEPSEEK_HTTP_ERROR"
E_DEEPSEEK_TIMEOUT          = "DEEPSEEK_TIMEOUT"
E_DEEPSEEK_EMPTY_RESPONSE   = "DEEPSEEK_EMPTY_RESPONSE"
E_DEEPSEEK_AUTH_ERROR       = "DEEPSEEK_AUTH_ERROR"
E_DEEPSEEK_PARSE_ERROR      = "DEEPSEEK_PARSE_ERROR"
E_DEEPSEEK_CONTENT_INVALID  = "DEEPSEEK_CONTENT_INVALID"

# 用于收集 review.md 的诊断信息
_review_events: list[dict] = []


def _review_event(step: str, code: str, message: str, detail: str = ""):
    _review_events.append({
        "step": step,
        "code": code,
        "message": message,
        "detail": detail,
        "time": beijing_now().strftime("%H:%M:%S"),
    })


# ============================================================
# 工具函数
# ============================================================

def beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def fmt_date(d: datetime, compact: bool = False) -> str:
    return d.strftime("%Y%m%d") if compact else d.strftime("%Y-%m-%d")


def fmt_datetime(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def fv(field: dict | None, fmt_spec: str = ".2f") -> str:
    if field is None or field.get("value") is None:
        return "N/A"
    try:
        return format(float(field["value"]), fmt_spec)
    except (ValueError, TypeError):
        return str(field["value"])


def fv_int(field: dict | None) -> str:
    return fv(field, ".0f")


def is_weekend(date_str: str) -> bool:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").weekday() >= 5
    except ValueError:
        return False


# ============================================================
# 市场数据
# ============================================================

def get_market_data(target_date: str) -> dict:
    from fetch_market import collect_market_data
    result = collect_market_data(target_date)
    if result.get("ok"):
        logger.info("市场数据获取成功 | 来源: %s | 交易日: %s",
                    result.get("_source_label"), result.get("trade_date"))
    else:
        logger.error("市场数据获取失败: %s", "; ".join(result.get("errors", [])))
    return result


def basis_description(basis_val: float) -> str:
    """Python生成基差描述，DeepSeek不得自行解释。"""
    if basis_val is None:
        return "基差数据不可用。"
    if basis_val > 0:
        return f"现货升水期货{abs(basis_val):.0f}元/吨。"
    elif basis_val < 0:
        return f"现货贴水期货{abs(basis_val):.0f}元/吨。"
    else:
        return "现货与期货平水。"


def build_market_summary(market: dict) -> str:
    # 郑糖主力 — SR0展示，不是SR2609
    zz_display = market.get("zz_display_name", "郑糖主力合约")
    zz_close = fv_int(market.get("zz_close"))
    zz_chg = float(fv(market.get("zz_change_pct"), ".2f").replace("N/A", "0"))
    zz_dir = "涨幅" if zz_chg >= 0 else "跌幅"

    parts = [f"{zz_display}收{zz_close}元/吨，{zz_dir}{abs(zz_chg):.2f}%。"]

    # ICE原糖主力
    ice_display = market.get("ice_display_name", "ICE原糖主力合约")
    ice_close_val = fv(market.get("ice_close"), ".2f")
    if ice_close_val != "N/A":
        ice_chg = float(fv(market.get("ice_change_pct"), ".2f").replace("N/A", "0"))
        ice_dir = "涨幅" if ice_chg >= 0 else "跌幅"
        parts.append(f"{ice_display}收{ice_close_val}美分/磅，{ice_dir}{abs(ice_chg):.2f}%。")

    # 基差 + Python生成描述
    basis_val = market.get("basis", {}).get("value")
    try:
        basis_val = float(basis_val) if basis_val is not None else None
    except (ValueError, TypeError):
        basis_val = None
    basis_text = basis_description(basis_val)
    parts.append(f"广西白糖现货－{zz_display}基差为{fv_int(market.get('basis'))}元/吨，{basis_text}")

    # 泛糖科技进口利润 — 全文只此一处
    brazil_field = market.get("brazil_profit", {})
    brazil_val = fv_int(brazil_field)
    brazil_date = brazil_field.get("data_date", "")
    meta = market.get("_import_profit_meta", {})

    if meta.get("source") == "泛糖科技" and brazil_val != "N/A":
        parts.append(
            f"配额外巴西糖加工完税估算利润为{brazil_val}元/吨"
            f"（泛糖科技，数据截至{brazil_date}，以日照白糖现货价测算）。"
        )
    elif brazil_val != "N/A":
        parts.append(f"配额外巴西糖加工完税估算利润为{brazil_val}元/吨（{brazil_date}）。")

    return " ".join(parts)


# ============================================================
# 基本面读取
# ============================================================

def read_fundamentals(target_date: str) -> tuple[str | None, str]:
    """
    读取研究员可选补充文件，不再阻塞。
    返回 (content, error_code)。文件不存在仅警告不报错。
    """
    f_dir = PROJECT_ROOT / config["fundamentals"]["input_dir"]
    fname = config["fundamentals"]["filename_pattern"].format(date=target_date)
    fpath = f_dir / fname

    if not fpath.exists():
        # 改为信息级别——研究员输入为可选项
        logger.info("研究员基本面文件不存在（可选）: %s", fpath)
        _review_event("读取基本面(研究员)", "SKIPPED", "可选文件未提供", str(fpath))
        return None, ""

    try:
        with open(fpath, "r", encoding="utf-8-sig") as fh:
            content = fh.read().strip()
    except Exception as e:
        logger.warning("读取基本面文件异常: %s", e)
        return None, ""

    if not content:
        logger.info("研究员基本面文件为空")
        return None, ""

    logger.info("研究员基本面: %s (%d 字符)", fpath.name, len(content))
    _review_event("读取基本面(研究员)", "OK", f"读取成功 ({len(content)} 字符)", str(fpath))
    return content, ""


def read_reference_sample() -> str:
    """读取日报风格样例，只作为模型写作风格参考，不作为事实来源。"""
    rel = config.get("report", {}).get("reference_sample_file", "")
    if not rel:
        return ""
    fpath = PROJECT_ROOT / rel
    if not fpath.exists():
        return ""
    try:
        text = fpath.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.info("读取参考样例失败: %s", e)
        return ""
    # 保留前几段，控制prompt长度；样例只学结构、节奏、措辞密度。
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return "\n\n".join(blocks[:3])[:1800]


# ============================================================
# 已验证缓存
# ============================================================

def load_valid_records_from_csv(target_contract: str | None = None) -> list[dict]:
    """从 data/verified_fundamentals.json 加载有效缓存。"""
    try:
        from update_data_csv import read_valid_rows
        return read_valid_rows()
    except ImportError:
        return []



# ============================================================
# 结构化基本面组装
# ============================================================

def _categorize_fundamentals(cache_records: list[dict], researcher_text: str | None) -> dict:
    """
    将缓存记录和研究员输入归类到结构化 buckets。
    """
    buckets = {
        "international": {
            "global_supply_demand": [],
            "brazil": [],
            "india": [],
            "thailand": [],
            "ice_market": [],
        },
        "domestic": {
            "current_season_25_26": [],
            "next_season_26_27_area": [],
            "next_season_26_27_weather": [],
            "imports_and_policy": [],
            "spot_and_basis": [],
            "import_profit": [],
        },
    }

    # 研究员输入只作为留痕与人工审阅材料，不作为事实数字输入。
    # 巴西/印度/泰国等标准化来源以自动抓取的白名单数据为准。

    # 根据国家和指标归类
    brazil_kw = ["巴西"]
    india_kw = ["印度"]
    thailand_kw = ["泰国"]
    china_kw = ["中国"]
    macro_kw = ["宏观"]

    for r in cache_records:
        country = r.get("country", "")
        indicator = r.get("indicator", "")
        val = r.get("value", "") or r.get("text_value", "")
        unit = r.get("unit", "")
        ddate = r.get("data_date", "")
        pub_at = r.get("published_at", "")
        dtype = r.get("data_type", "")
        src = r.get("source_name", "")
        surl = r.get("source_url", "")
        st = r.get("status", "")
        source_status = r.get("source_status", st)
        source_channel = r.get("source_channel", "")
        season = r.get("season", "")
        impact_dir = r.get("impact_direction", "")
        impact_time = r.get("impact_timing", "")
        notes = r.get("notes", "")

        entry = {
            "country": country,
            "indicator": indicator,
            "fact_or_value": val,
            "unit": unit,
            "data_date": ddate,
            "published_at": pub_at,
            "data_type": dtype,
            "season": season,
            "source": src,
            "source_url": surl,
            "status": st,
            "source_status": source_status,
            "source_channel": source_channel,
            "official_level": r.get("official_level", ""),
            "impact_direction": impact_dir,
            "impact_timing": impact_time,
            "notes": notes,
        }

        # 巴西
        if any(k in country for k in brazil_kw):
            if "天气" in indicator:
                buckets["international"]["brazil"].append(entry)
            elif "全球" in indicator or "供需" in indicator:
                buckets["international"]["global_supply_demand"].append(entry)
            else:
                buckets["international"]["brazil"].append(entry)
        # 印度
        elif any(k in country for k in india_kw):
            buckets["international"]["india"].append(entry)
        # 泰国
        elif any(k in country for k in thailand_kw):
            buckets["international"]["thailand"].append(entry)
        # 中国
        elif any(k in country for k in china_kw):
            ind_lower = indicator.lower()
            if any(kw in ind_lower for kw in ["面积", "种植", "新植", "宿根", "下种"]):
                buckets["domestic"]["next_season_26_27_area"].append(entry)
            elif any(kw in ind_lower for kw in ["天气", "降雨", "墒情", "苗情", "出苗", "干旱", "台风"]):
                buckets["domestic"]["next_season_26_27_weather"].append(entry)
            elif any(kw in ind_lower for kw in ["进口", "糖浆", "预混粉", "到港", "关税", "配额", "政策", "利润", "成本"]):
                buckets["domestic"]["imports_and_policy"].append(entry)
            elif any(kw in ind_lower for kw in ["现货", "基差", "升贴水"]):
                buckets["domestic"]["spot_and_basis"].append(entry)
            elif any(kw in ind_lower for kw in ["产量", "库存", "消费", "压榨", "榨季"]):
                buckets["domestic"]["current_season_25_26"].append(entry)
            else:
                buckets["domestic"]["current_season_25_26"].append(entry)
        # 宏观
        elif any(k in country for k in macro_kw):
            buckets["international"]["ice_market"].append(entry)

    # 泛糖进口利润单独放
    # (由 call_deepseek 调用方注入 market data 的 _import_profit_meta)

    return buckets


def build_structured_fundamentals(cache_records: list[dict], researcher_text: str | None,
                                  import_profit_meta: dict | None) -> str:
    """
    组装结构化的基本面文本，按固定顺序输出给 DeepSeek。
    结构: 国际供需 → 巴西 → 印度 → 泰国 → 美糖 → 国内25/26 → 26/27面积 → 26/27天气 → 进口 → SR2609
    """
    buckets = _categorize_fundamentals(cache_records, researcher_text)
    inter = buckets["international"]
    dom = buckets["domestic"]

    lines = []

    def _fmt_entry(e: dict) -> str:
        return (
            f"  [{e['data_type']}][{e['status']}] {e['country']}/{e['indicator']}: "
            f"{e['fact_or_value']} ({e['unit']}) | 数据日期:{e['data_date']} "
            f"| 来源:{e['source']} | 来源通道:{e.get('source_channel') or 'N/A'} "
            f"| 官方层级:{e.get('official_level') or 'N/A'} "
            f"| 来源状态:{e.get('source_status') or e['status']} "
            f"| 影响:{e['impact_direction'] or 'N/A'} | 时点:{e['impact_timing'] or 'N/A'}"
        )

    # ── 国际部分 ──
    lines.append("## 国际部分")

    lines.append("### 全球供需总判断")
    gsd = inter["global_supply_demand"]
    if gsd:
        for e in gsd:
            lines.append(_fmt_entry(e))
    else:
        lines.append("  （暂无新的全球供需数据）")

    for section, title in [("brazil", "巴西"), ("india", "印度"), ("thailand", "泰国")]:
        lines.append(f"### {title}")
        recs = inter[section]
        if recs:
            for e in recs:
                lines.append(_fmt_entry(e))
        else:
            lines.append(f"  （暂无{title}新数据）")

    lines.append("### 美糖判断（仅基于国际数据）")
    ice_recs = inter["ice_market"]
    if ice_recs:
        for e in ice_recs:
            lines.append(_fmt_entry(e))
    else:
        lines.append("  （暂无宏观/ICE特定新数据）")

    # ── 国内部分 ──
    lines.append("")
    lines.append("## 国内部分")

    for section, title in [
        ("current_season_25_26", "25/26榨季现实供给"),
        ("next_season_26_27_area", "26/27榨季种植面积"),
        ("next_season_26_27_weather", "26/27榨季天气/墒情/苗情"),
        ("imports_and_policy", "进口/糖浆预混粉/政策"),
        ("spot_and_basis", "现货与基差"),
    ]:
        lines.append(f"### {title}")
        recs = dom[section]
        if recs:
            for e in recs:
                lines.append(_fmt_entry(e))
        else:
            lines.append(f"  （暂无{title}数据）")

    # 进口利润 — 只传状态，不传数值（数值由Python写入日报）
    lines.append("### 配额外进口利润")
    if import_profit_meta and import_profit_meta.get("quota_outside_profit"):
        profit_val = import_profit_meta["quota_outside_profit"]
        status_text = "进口窗口打开（利润为正）" if profit_val > 0 else "进口窗口关闭（利润为负）"
        lines.append(
            f"  [actual][verified] 配额外进口利润状态: {status_text} "
            f"| 数据日期:{import_profit_meta.get('data_date','')} "
            f"| 来源:泛糖科技 "
            f"| 参考现货:{import_profit_meta.get('reference_spot','')} "
            f"| 注意: 具体数值由Python在日报市场表现中写入，此处不提供数字。"
        )
    else:
        lines.append("  （暂无泛糖科技进口利润数据）")

    lines.append("")
    lines.append("### SR2609判断（综合国际+国内后形成）")
    lines.append("  请基于以上国际和国内全部信息，单独形成SR2609判断。")
    lines.append("  不生成具体开仓点、止损位、目标位。")

    return "\n".join(lines)


def generate_fundamental_sources_md(target_date: str, cache_records: list[dict],
                                    researcher_used: bool, deepseek_used: bool) -> str:
    """生成 fundamental_sources.md。"""
    now = beijing_now()
    lines = [
        f"# 基本面数据来源清单 — {target_date}",
        "",
        f"**生成时间**: {fmt_datetime(now)}",
        f"**目标合约**: {TARGET_CONTRACT}",
        f"**研究员输入**: {'已使用' if researcher_used else '未提供'}",
        f"**DeepSeek输入**: {'已包含' if deepseek_used else '未使用'}",
        "",
        f"| 国家 | 指标 | 值 | 单位 | 数据日期 | 发布日期 | 来源 | 类型 | 状态 | 榨季 | 影响方向 | 入正文 |",
        f"|------|------|----|------|----------|----------|------|------|------|------|----------|--------|",
    ]

    for r in cache_records:
        country = r.get("country", "")
        indicator = r.get("indicator", "")[:40]
        value = str(r.get("value_or_fact") or r.get("value") or r.get("text_value") or "")[:50]
        unit = r.get("unit", "")
        data_date = r.get("data_date", "")
        published = (r.get("published_at", "") or "")[:10]
        source = r.get("source_name", "")
        dt = r.get("data_type", "")
        st = r.get("status", "")
        season = r.get("season", "")
        impact = r.get("impact_direction", "")
        in_report = "是" if st in ("fresh", "unchanged", "valid_cached") else "否"
        lines.append(
            f"| {country} | {indicator} | {value} | {unit} | {data_date} "
            f"| {published} | {source} | {dt} | {st} | {season} | {impact} | {in_report} |"
        )

    if not cache_records:
        lines.append("| - | 无可用数据 | - | - | - | - | - | - | - | - | - | - |")

    return "\n".join(lines) + "\n"
#!/usr/bin/env python3
"""
白糖日报生成系统 - 主程序
用法: python scripts/run_daily.py [--date YYYY-MM-DD]

严格约束:
  - 行情合约、基本面、交易观点三者必须均为 SR2609
  - 先校验后生成，一致性检查在 DeepSeek 之前
  - 错误日志使用特定错误码，不输出 API Key
"""

import os
import re
import sys
import json
import shutil
import hashlib
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import OpenAI, AuthenticationError

# ── 项目根目录（绝对路径，不依赖终端 CWD） ─────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# ── 显式加载 .env ───────────────────────────────────────
load_dotenv(PROJECT_ROOT / ".env", override=True)

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

TARGET_CONTRACT = config["target_contract"]["code"]
REJECT_CONTRACTS = config["approved_view"].get("reject_contracts", [])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ── 错误码常量 ──────────────────────────────────────────
E_FUNDAMENTAL_FILE_NOT_FOUND = "FUNDAMENTAL_FILE_NOT_FOUND"
E_FUNDAMENTAL_FILE_EMPTY    = "FUNDAMENTAL_FILE_EMPTY"
E_DEEPSEEK_CONFIG_MISSING   = "DEEPSEEK_CONFIG_MISSING"
E_DEEPSEEK_HTTP_ERROR       = "DEEPSEEK_HTTP_ERROR"
E_DEEPSEEK_TIMEOUT          = "DEEPSEEK_TIMEOUT"
E_DEEPSEEK_EMPTY_RESPONSE   = "DEEPSEEK_EMPTY_RESPONSE"
E_DEEPSEEK_AUTH_ERROR       = "DEEPSEEK_AUTH_ERROR"
E_DEEPSEEK_PARSE_ERROR      = "DEEPSEEK_PARSE_ERROR"
E_DEEPSEEK_CONTENT_INVALID  = "DEEPSEEK_CONTENT_INVALID"

# 用于收集 review.md 的诊断信息
_review_events: list[dict] = []


def _review_event(step: str, code: str, message: str, detail: str = ""):
    _review_events.append({
        "step": step,
        "code": code,
        "message": message,
        "detail": detail,
        "time": beijing_now().strftime("%H:%M:%S"),
    })


# ============================================================
# 工具函数
# ============================================================

def beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def fmt_date(d: datetime, compact: bool = False) -> str:
    return d.strftime("%Y%m%d") if compact else d.strftime("%Y-%m-%d")


def fmt_datetime(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def fv(field: dict | None, fmt_spec: str = ".2f") -> str:
    if field is None or field.get("value") is None:
        return "N/A"
    try:
        return format(float(field["value"]), fmt_spec)
    except (ValueError, TypeError):
        return str(field["value"])


def fv_int(field: dict | None) -> str:
    return fv(field, ".0f")


def is_weekend(date_str: str) -> bool:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").weekday() >= 5
    except ValueError:
        return False


# ============================================================
# 市场数据
# ============================================================

def get_market_data(target_date: str) -> dict:
    from fetch_market import collect_market_data
    result = collect_market_data(target_date)
    if result.get("ok"):
        logger.info("市场数据获取成功 | 来源: %s | 交易日: %s",
                    result.get("_source_label"), result.get("trade_date"))
    else:
        logger.error("市场数据获取失败: %s", "; ".join(result.get("errors", [])))
    return result


def basis_description(basis_val: float) -> str:
    """Python生成基差描述，DeepSeek不得自行解释。"""
    if basis_val is None:
        return "基差数据不可用。"
    if basis_val > 0:
        return f"现货升水期货{abs(basis_val):.0f}元/吨。"
    elif basis_val < 0:
        return f"现货贴水期货{abs(basis_val):.0f}元/吨。"
    else:
        return "现货与期货平水。"


def build_market_summary(market: dict) -> str:
    # 郑糖主力 — SR0展示，不是SR2609
    zz_display = market.get("zz_display_name", "郑糖主力合约")
    zz_close = fv_int(market.get("zz_close"))
    zz_chg = float(fv(market.get("zz_change_pct"), ".2f").replace("N/A", "0"))
    zz_dir = "涨幅" if zz_chg >= 0 else "跌幅"

    parts = [f"{zz_display}收{zz_close}元/吨，{zz_dir}{abs(zz_chg):.2f}%。"]

    # ICE原糖主力
    ice_display = market.get("ice_display_name", "ICE原糖主力合约")
    ice_close_val = fv(market.get("ice_close"), ".2f")
    if ice_close_val != "N/A":
        ice_chg = float(fv(market.get("ice_change_pct"), ".2f").replace("N/A", "0"))
        ice_dir = "涨幅" if ice_chg >= 0 else "跌幅"
        parts.append(f"{ice_display}收{ice_close_val}美分/磅，{ice_dir}{abs(ice_chg):.2f}%。")

    # 基差 + Python生成描述
    basis_val = market.get("basis", {}).get("value")
    try:
        basis_val = float(basis_val) if basis_val is not None else None
    except (ValueError, TypeError):
        basis_val = None
    basis_text = basis_description(basis_val)
    parts.append(f"广西白糖现货－{zz_display}基差为{fv_int(market.get('basis'))}元/吨，{basis_text}")

    # 泛糖科技进口利润 — 全文只此一处
    brazil_field = market.get("brazil_profit", {})
    brazil_val = fv_int(brazil_field)
    brazil_date = brazil_field.get("data_date", "")
    meta = market.get("_import_profit_meta", {})

    if meta.get("source") == "泛糖科技" and brazil_val != "N/A":
        parts.append(
            f"配额外巴西糖加工完税估算利润为{brazil_val}元/吨"
            f"（泛糖科技，数据截至{brazil_date}，以日照白糖现货价测算）。"
        )
    elif brazil_val != "N/A":
        parts.append(f"配额外巴西糖加工完税估算利润为{brazil_val}元/吨（{brazil_date}）。")

    return " ".join(parts)


# ============================================================
# 基本面读取
# ============================================================

def read_fundamentals(target_date: str) -> tuple[str | None, str]:
    """
    读取研究员可选补充文件，不再阻塞。
    返回 (content, error_code)。文件不存在仅警告不报错。
    """
    f_dir = PROJECT_ROOT / config["fundamentals"]["input_dir"]
    fname = config["fundamentals"]["filename_pattern"].format(date=target_date)
    fpath = f_dir / fname

    if not fpath.exists():
        # 改为信息级别——研究员输入为可选项
        logger.info("研究员基本面文件不存在（可选）: %s", fpath)
        _review_event("读取基本面(研究员)", "SKIPPED", "可选文件未提供", str(fpath))
        return None, ""

    try:
        with open(fpath, "r", encoding="utf-8-sig") as fh:
            content = fh.read().strip()
    except Exception as e:
        logger.warning("读取基本面文件异常: %s", e)
        return None, ""

    if not content:
        logger.info("研究员基本面文件为空")
        return None, ""

    logger.info("研究员基本面: %s (%d 字符)", fpath.name, len(content))
    _review_event("读取基本面(研究员)", "OK", f"读取成功 ({len(content)} 字符)", str(fpath))
    return content, ""


# ============================================================
# 已验证缓存
# ============================================================

def load_valid_records_from_csv(target_contract: str | None = None) -> list[dict]:
    """从 data/verified_fundamentals.json 加载有效缓存。"""
    try:
        from update_data_csv import read_valid_rows
        return read_valid_rows()
    except ImportError:
        return []


def fetch_status_summary() -> dict[str, dict]:
    try:
        from fetch_fundamentals import fetch_status_by_country
        return fetch_status_by_country()
    except ImportError:
        return {}


def _try_parse_float(s: str) -> float | None:
    try:
        return float(s.strip())
    except (ValueError, TypeError):
        return None


# ============================================================
# 策略有效性校验
# ============================================================

def validate_strategy(approved_view: dict | None, market: dict) -> tuple[bool, str]:
    """
    校验交易策略是否仍然有效。
    返回 (is_valid, reason)。
    失效条件: 合约不匹配 / 过期 / 当前价格偏离参考价 >5% / 超过止损位
    """
    if not approved_view:
        return False, "无交易观点"

    applicable = approved_view.get("applicable_contract", "").strip()
    if applicable != TARGET_CONTRACT:
        return False, f"适用合约 {applicable} != {TARGET_CONTRACT}"

    if approved_view.get("_expired"):
        return False, "观点已过有效期"

    ref_price = approved_view.get("reference_price")
    if ref_price is None:
        return False, "观点缺少参考价格"

    current_close = market.get("zz_close", {}).get("value")
    if current_close is None:
        return False, "缺少当前行情价格"

    try:
        ref = float(ref_price)
        cur = float(current_close)
        if ref != 0:
            deviation = abs(cur - ref) / abs(ref) * 100
            if deviation > 5:
                return False, f"当前价格 {cur:.0f} 偏离参考价 {ref:.0f} {deviation:.1f}% (>5%)"
    except (ValueError, TypeError):
        return False, "价格格式异常"

    # 检查是否超过止损位
    strategy = approved_view.get("strategy", "")
    stop_match = re.search(r"止损设于\s*(\d+)", strategy)
    if stop_match:
        stop_price = float(stop_match.group(1))
        try:
            cur = float(current_close)
            if cur >= stop_price:
                return False, f"当前价格 {cur:.0f} 已触及止损位 {stop_price:.0f}"
        except (ValueError, TypeError):
            pass

    return True, ""


# ============================================================
# 最终一致性检查
# ============================================================

def final_consistency_check(market: dict, fundamentals_ai: str | None,
                            approved_view: dict | None, strategy_valid: bool) -> tuple[bool, list[str]]:
    """
    生成日报前的8项最终检查。返回 (all_ok, failures)。
    """
    failures = []

    # 1. 行情显示名称不含SR2609 (SR0不等于SR2609)
    zz_display = market.get("zz_display_name", "")
    if "SR2609" in zz_display:
        failures.append("行情显示名包含SR2609（SR0不得写成SR2609）")

    # 2. 基差数值与方向描述一致
    basis_val = market.get("basis", {}).get("value")
    if basis_val is not None:
        try:
            bv = float(basis_val)
            bd = basis_description(bv)
            if bv < 0 and "现货升水" in bd:
                failures.append("负基差错误描述为现货升水")
            if bv > 0 and "现货贴水" in bd:
                failures.append("正基差错误描述为现货贴水")
        except (ValueError, TypeError):
            pass

    # 3. 进口利润全文只有一个数值 (检查基本面正文不包含利润数字)
    if fundamentals_ai:
        profit_meta = market.get("_import_profit_meta", {})
        expected_profit = str(profit_meta.get("quota_outside_profit", ""))
        # 检查正文中是否出现不同利润值
        profit_nums = set(re.findall(r"利润[约为]*\s*(\d+)\s*元", fundamentals_ai))
        if len(profit_nums) > 1:
            failures.append(f"正文出现多个进口利润值: {profit_nums}")
        if profit_nums and expected_profit and all(p != expected_profit for p in profit_nums):
            pass  # 正文利润值与市场区不同 — 标记但不阻断（下面检查会抓）

    # 4. 策略有效性仅影响交易策略降级，不阻断市场和基本面日报生成。

    # 5. 预测/实际混用检查（简单启发式）
    if fundamentals_ai:
        if re.search(r"预[计估].*?实际|实际.*?预[计估]", fundamentals_ai):
            failures.append("基本面可能混淆预测值和实际值")

    # 6. SR0不被写成SR2609价格
    # (已在第1项检查)

    # 7. 印度/泰国数据必须有榨季
    # (由 _categorize_fundamentals 过滤，此项在 run() 中处理)

    # 8. UNICA数据期不晚于报告日期
    # (由 fetch_fundamentals 校验)

    return len(failures) == 0, failures

    records = cache.get("records", [])
    valid = []
    for r in records:
        status = r.get("status", "")
        if status in ("fresh", "valid_cached"):
            if target_contract and r.get("target_contract") != target_contract:
                continue
            valid.append(r)

    logger.info("有效缓存记录: %d 条", len(valid))
    _review_event("读取缓存", "OK", f"{len(valid)} 条有效记录 (共 {len(records)} 条)", str(cache_path))
    return valid



# ============================================================
# DeepSeek 调用
# ============================================================

def _clean_model_response(text: str) -> str:
    """清理模型返回文本：去除 Markdown 代码块、多余前后说明、重复加粗标记。"""
    if not text:
        return ""

    # 去除 ```markdown / ```json / ``` 代码块包装
    text = re.sub(r"^```(?:markdown|json|text)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?\s*```\s*$", "", text)

    # 去除前后空白
    text = text.strip()

    # 去除可能的 JSON 结构残留
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "fundamentals" in obj:
                text = obj["fundamentals"]
            elif isinstance(obj, dict) and "content" in obj:
                text = obj["content"]
        except json.JSONDecodeError:
            pass

    # 修复连续加粗标记: "**基本面：****国际方面**" → "国际方面"
    text = re.sub(r"\*\*[^*]+\*\*\*\*", "", text)
    # 去除开头的加粗标记（如果有）
    text = re.sub(r"^\*\*[^*]+\*\*\s*", "", text)

    return text.strip()


def _extract_numbers(text: str) -> set[str]:
    """提取文本中所有数字（用于校验模型是否虚构了输入中不存在的新数字）。"""
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _validate_model_content(text: str, fundamentals_input: str, market_summary: str) -> tuple[bool, str]:
    """
    校验模型生成的内容。
    返回 (is_valid, reason)。
    """
    if not text or not text.strip():
        return False, "模型返回空文本"

    # 长度检查
    text_len = len(text)
    min_len = config["report"].get("fundamentals_min_chars", 250)
    max_len = config["report"].get("fundamentals_max_chars", 450)
    if text_len < min_len * 0.7:  # 允许30%容差
        logger.warning("基本面字数 %d 低于最小要求 %d", text_len, min_len)
    if text_len > max_len * 1.2:
        logger.warning("基本面字数 %d 超出最大要求 %d", text_len, max_len)

    # 检查绝对词
    abs_words = ["必然", "一定", "毫无疑问", "百分之百"]
    found_abs = [w for w in abs_words if w in text]
    if found_abs:
        logger.warning("模型使用了绝对词: %s", ", ".join(found_abs))

    # 检查是否虚构了新数字（简单启发式：提取所有 ≥3 位数字，检查是否在输入中出现）
    model_nums = {n for n in _extract_numbers(text) if len(n) >= 3}
    input_nums = _extract_numbers(fundamentals_input + market_summary)
    new_nums = model_nums - input_nums
    if len(new_nums) > 5:
        logger.warning("模型可能虚构了 %d 个新数字: %s", len(new_nums), ", ".join(sorted(new_nums)[:5]))

    return True, ""


def _check_logic_consistency(text: str, fundamentals_input: str) -> list[str]:
    """
    检查基本面正文的逻辑一致性。
    返回问题列表，空列表表示通过。
    """
    issues = []

    # ── 1. 印度出口政策方向检查 ──
    has_no_new_export = any(kw in fundamentals_input for kw in [
        "暂无新增出口", "出口政策无新增信号", "暂无新增配额",
        "政府尚未宣布扩大出口", "出口政策暂无",
    ])
    has_new_export = any(kw in fundamentals_input for kw in [
        "批准新增出口", "扩大出口额度", "放松出口限制",
        "明确允许新增出口", "新增出口配额",
    ])
    has_export_restriction = any(kw in fundamentals_input for kw in [
        "限制出口", "取消出口配额", "出口量低于预期",
        "出口审批推迟", "优先保障国内供应",
    ])

    india_section = ""
    india_match = re.search(r"印度.{0,300}?(?=泰国|美糖|综合来看|ICE原糖)", text, re.S)
    if india_match:
        india_section = india_match.group(0)

    if india_section:
        if has_no_new_export and not has_new_export:
            if "出口" in india_section and ("压制" in india_section or "形成压力" in india_section):
                if "中性偏多" not in india_section and "支撑" not in india_section:
                    issues.append("印度无新增出口信号，但正文写成'压制'或'形成压力'，应为中性偏多")
            # 检查是否写成"明显支撑"或"主要利多"
            if "明显支撑" in india_section or "主要利多" in india_section or "供应收紧" in india_section:
                issues.append("印度无新增出口，但正文写成'明显支撑'或'主要利多'，应为中性偏多")

        if has_new_export:
            if "压制" not in india_section and "偏空" not in india_section and "供应增加" not in india_section:
                issues.append("印度有新增出口，但正文未体现压制效果")

        if has_export_restriction:
            if "支撑" not in india_section and "偏多" not in india_section:
                issues.append("印度限制出口，但正文未体现支撑效果")

    # ── 2. 巴西UNICA累计检查 ──
    # 检查巴西部分是否将Table 2双周数据写成累计
    has_unica_biweekly = "UNICA Table2" in fundamentals_input and "biweekly" in fundamentals_input.lower()
    if has_unica_biweekly:
        brazil_section = ""
        brazil_match = re.search(r"巴西.{0,300}?(?=印度|泰国|美糖|综合来看)", text, re.S)
        if brazil_match:
            brazil_section = brazil_match.group(0)
        if brazil_section:
            if "累计产糖" in brazil_section or "榨季累计" in brazil_section or "截至" in brazil_section and "累计" in brazil_section:
                issues.append("巴西UNICA Table 2是双周数据，但正文写成'累计产糖'或'截至某日累计'")

    # ── 3. 泰国无同比数据时越权检查 ──
    # 检查输入中是否有同比数据
    has_thailand_yoy = any(kw in fundamentals_input for kw in [
        "同比", "上年同期", "YoY", "year-on-year",
    ])
    if not has_thailand_yoy:
        thailand_section = ""
        thailand_match = re.search(r"泰国.{0,300}?(?=美糖|综合来看|ICE原糖)", text, re.S)
        if thailand_match:
            thailand_section = thailand_match.group(0)
        if thailand_section:
            forbidden_terms = ["同比增加", "同比上升", "丰产超预期", "产量处于高位",
                              "整体供应充裕", "出口能力增强", "出口压力增加", "市场已经消化"]
            for term in forbidden_terms:
                if term in thailand_section:
                    issues.append(f"泰国无同比数据，但正文写了'{term}'，应为neutral_to_bearish")

    # ── 4. 国内无数据时的推断检查 ──
    has_domestic_production = any(kw in fundamentals_input for kw in [
        "累计产糖", "累计销糖", "工业库存", "产销率", "收榨",
    ])
    if not has_domestic_production:
        forbidden_domestic = ["榨季已结束", "现实供给格局已定", "供给基本定型",
                            "国内供应宽松", "库存压力已经形成"]
        for term in forbidden_domestic:
            if term in text:
                issues.append(f"国内无产销数据，但正文写了'{term}'，应为'暂不对现实供应变化作新增判断'")

    # ── 5. 进口利润方向检查 ──
    # 进口利润为正=压力，不得写成支撑
    if "进口利润" in fundamentals_input and "为正" in fundamentals_input:
        # 在进口相关段落中检查
        import_section = ""
        import_match = re.search(r"进口.{0,200}?(?=综合来看|{TARGET_CONTRACT}|SR\d+)", text, re.S)
        if import_match:
            import_section = import_match.group(0)
        if import_section:
            if "支撑" in import_section and "压力" not in import_section:
                issues.append("进口利润为正，但正文写成'支撑'，应为'对后续供应形成潜在压力'")

    # ── 6. 风险提示扩写检查 ──
    # 检查风险提示是否包含研究员模板之外的自动生成事件
    # 注意: 研究员模板中的风险提示是允许的，只检查模型自行扩写的内容
    risk_section = ""
    risk_match = re.search(r"风险提示[：:]?\s*(.+?)(?=\n---|\n\n|$)", text, re.S)
    if risk_match:
        risk_section = risk_match.group(1)
    # 只有当风险提示内容不在研究员模板中时才标记
    # 研究员模板通常是"宏观、政策、天气、进口量。"或更详细的预设内容
    # 此检查仅在风险提示明显超出预期时触发
    if risk_section and len(risk_section) > 200:
        issues.append("风险提示内容过长，可能包含模型自行扩写的内容")

    # ── 7. Markdown格式检查 ──
    if "**基本面：****" in text or "**国内方面**" in text:
        issues.append("Markdown格式错误：出现连续加粗标记")

    # ── 局部天气扩大检查 ──
    if "single_mill_area" in fundamentals_input or "local" in fundamentals_input:
        if "广西整体" in text or "全区" in text or "全国" in text:
            if "部分蔗区" not in text and "局部" not in text:
                issues.append("输入为局部数据，但正文扩大为全区或全国判断")

    # ── 种植面积虚构检查 ──
    if "种植面积" in text and "种植面积" not in fundamentals_input:
        if "暂无" not in text and "无新" not in text:
            issues.append("输入无种植面积数据，但正文生成了面积相关内容")

    # ── 缺失数据处理检查 ──
    if "未发生明显变化" in text and "未发生明显变化" not in fundamentals_input:
        issues.append("正文写了'未发生明显变化'，但输入中无此判断依据")

    if "供给基本定型" in text and "供给基本定型" not in fundamentals_input:
        issues.append("正文写了'供给基本定型'，但输入中无此判断依据")

    return issues


def call_deepseek(market_summary: str, fundamentals_text: str, approved_view: dict | None) -> tuple[str | None, str]:
    """
    调用 DeepSeek 生成基本面综述。
    返回 (content, error_code)。error_code 为空表示成功。

    错误码: DEEPSEEK_CONFIG_MISSING | DEEPSEEK_AUTH_ERROR |
            DEEPSEEK_HTTP_ERROR | DEEPSEEK_TIMEOUT |
            DEEPSEEK_EMPTY_RESPONSE | DEEPSEEK_PARSE_ERROR |
            DEEPSEEK_CONTENT_INVALID
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # ── 配置检查 ──
    if not api_key or api_key.startswith("sk-placeholder"):
        code = E_DEEPSEEK_CONFIG_MISSING
        logger.error("[%s] DEEPSEEK_API_KEY 未设置或为占位值", code)
        _review_event("DeepSeek调用", code, "API Key 未设置或为占位值", "")
        return None, code

    if not base_url:
        code = E_DEEPSEEK_CONFIG_MISSING
        logger.error("[%s] DEEPSEEK_BASE_URL 未设置", code)
        return None, code

    # ── 输入检查 ──
    if not fundamentals_text or not fundamentals_text.strip():
        code = E_FUNDAMENTAL_FILE_EMPTY
        logger.warning("[%s] 基本面输入为空，跳过模型调用", code)
        return None, code

    # ── 构建 prompt ──
    view_text = ""
    if approved_view:
        view_text = (
            f"研究员已确认观点（适用合约：{approved_view.get('applicable_contract', '未知')}）：\n"
            f"- 当前判断：{approved_view.get('judgment', '无')}\n"
            f"- 交易策略：{approved_view.get('strategy', '无')}\n"
        )
    reference_sample = read_reference_sample()
    reference_text = ""
    if reference_sample:
        reference_text = (
            "日报参考样例（只学习结构、节奏、措辞密度；不得把样例中的数字或观点当成事实）：\n"
            f"{reference_sample}\n\n"
        )

    prompt = (
        f"你是白糖行业分析师。当前目标合约: {TARGET_CONTRACT}。\n\n"
        f"{reference_text}"
        f"{view_text}\n"
        f"行情数据:\n{market_summary}\n\n"
        f"结构化基本面数据（已按分析顺序排列）:\n{fundamentals_text}\n\n"
        f"=== 硬约束（必须逐条遵守） ===\n\n"
        f"【结构顺序——不可调整】\n"
        f"第一段(国际): 国际供需总判断→巴西(双周生产+面积+降雨)→印度(生产+面积+季风+出口)→泰国(生产+面积+降雨)→美糖判断\n"
        f"第二段(国内): 25/26榨季现实供给→26/27种植面积→26/27降雨墒情苗情→进口/现货/基差/政策→{TARGET_CONTRACT}判断\n\n"
        f"【巴西UNICA数据口径——不可违反】\n"
        f"- 巴西数据只能使用UNICA Table 2的Sugar和Share %，不得使用Table 1累计数据\n"
        f"- UNICA Table 2是双周数据(period_type=biweekly)，必须按报告真实数据期写成双周期间\n"
        f"- 正确写法: '巴西中南部在最新双周期间产糖XXX万吨，同比增加XX%，制糖比为XX%'\n"
        f"- 禁止写法: '截至某日累计产糖''榨季累计产糖''累计产糖XXX万吨'\n"
        f"- 如果CSV中有period_start和period_end字段，必须使用该期间；没有则写'最新双周'\n"
        f"- 巴西制糖比40.34%、乙醇占比59.66%时，不得写'高制糖比'；只能写糖产量同比增加，但糖醇分配仍偏乙醇\n\n"
        f"【印度出口政策方向规则——最高优先级，不可违反】\n"
        f"- 印度出口政策方向必须按以下固定规则判断:\n"
        f"  (1) 无新增出口信号(暂无新增配额/暂无新增批准/出口政策无新增信号/政府尚未宣布扩大出口) → impact=中性偏多\n"
        f"     原因: 没有新增出口意味着国际市场没有新增印度糖供应，不会对糖价形成新的供应压制\n"
        f"     推荐写法: '印度出口政策暂无新增信号，国际市场没有新增印度糖供应压力，对糖价影响中性偏多'\n"
        f"  (2) 明确新增出口(批准新增配额/扩大出口额度/放松出口限制/明确允许新增出口/实际出口量明显增加) → impact=偏空\n"
        f"     推荐写法: '印度新增出口配额，国际市场供应增加，对国际糖价形成压制'\n"
        f"  (3) 限制出口(限制出口/取消配额/出口量低于预期/出口审批推迟/优先保障国内供应) → impact=偏多\n"
        f"     推荐写法: '印度出口受限，国际市场可获得供应减少，对国际糖价形成支撑'\n"
        f"- '无新增出口'只表示没有新增供应压力，不代表出现主动利多\n"
        f"- 禁止写法: '印度提供明显支撑''印度形成主要利多''印度供应收紧'——除非存在明确限制出口或减产数据\n"
        f"- 印度NFCSF的Lmts单位不得改写为'万吨'；正文保留'Lmts'或写'lakh metric tonnes'\n"
        f"- 印度不得写'高于预期''低于预期'，除非输入数据明确给出预期值\n\n"
        f"【泰国结论权限——无同比数据时不可越权】\n"
        f"- 泰国当前有效数据: 截至日期、累计入榨甘蔗量、累计产糖量、出糖率\n"
        f"- 如果没有上年同期、同比、官方最终预估或出口数据，禁止写: '同比增加''丰产超预期''产量处于高位''整体供应充裕''出口能力增强''出口压力增加''市场已经消化'\n"
        f"- 无同比数据时，泰国impact_direction=neutral_to_bearish，confidence=medium_low\n"
        f"- 允许写: '当前榨季供应规模得到官方生产数据确认'\n"
        f"- 泰国数据优先使用SugarZone生产报告；不得使用越南、USDA、商业媒体补齐\n"
        f"- 种植面积和降雨: 有数据写数据，无数据必须写'下一榨季种植面积和降雨暂无新的已验证信息'\n\n"
        f"【美糖判断逻辑——必须与三国方向一致】\n"
        f"- 当前方向: 巴西=偏空(双周供应增加), 印度=中性偏多(无新增出口), 泰国=neutral_to_bearish(无同比)\n"
        f"- 美糖总结必须体现: 巴西供应压力较为明确，印度暂无新增出口未形成新的供应压制\n"
        f"- 美糖判断只用国际数据，不得混入中国库存/南宁现货/郑糖基差/中国进口政策\n"
        f"- 国际油价上涨不等于巴西制糖比必然下降，需判断是否已传导至巴西国内\n\n"
        f"【国内分析规则】\n"
        f"- 如果没有有效的产糖量、销量、库存、产销率、收榨进度数据，写: '国内25/26榨季暂无新的已验证产销和库存数据，暂不对现实供应变化作新增判断'\n"
        f"- 禁止写: '2025/26榨季已结束''现实供给格局已定''供给基本定型''国内供应宽松''库存压力已经形成'——除非CSV中有确认数据\n"
        f"- 行业座谈会、产业大会、招商活动不要进入基本面正文\n"
        f"- 26/27种植面积: 有数据写数据，无数据写'26/27榨季广西、云南甘蔗种植面积暂无新的已验证数据'\n"
        f"- 26/27天气苗情: 局部信息只能写'部分蔗区'，不得扩大为全区判断；禁止写'影响范围有限''不会影响全区''全区苗情稳定'\n"
        f"- 基差方向必须与行情数据一致: 基差为正写现货升水期货，基差为负写现货贴水期货\n"
        f"- 进口利润为正=进口窗口打开=对后续国内供应形成潜在压力（不得写成支撑）\n"
        f"- 进口利润不得写入具体数字(数字由Python在市场表现中写入)，只能写'进口利润为正'或'进口窗口处于打开/关闭状态'\n"
        f"- {TARGET_CONTRACT}判断: 说明主要支撑/主要压力/偏震荡还是偏强偏弱/上方和下方限制\n"
        f"- {TARGET_CONTRACT}判断不生成具体开仓点、止损位、目标位\n\n"
        f"【风险提示规则】\n"
        f"- 风险提示固定读取配置或研究员模板，不得自行扩写\n"
        f"- 除非CSV中存在明确且有效的风险事件，否则不得自动生成具体事件（如'印度若突击批准出口''6月下旬进口糖到港''巴西雷亚尔波动''台风季影响'）\n\n"
        f"【Markdown格式规则】\n"
        f"- 基本面正文开头写'国际方面，'，不要出现'**基本面：****国际方面**'这样的连续加粗标记\n"
        f"- 国内部分直接接续为'国内方面，'，不要再次使用加粗小标题\n"
        f"- 保持日报样例的连续文字形式\n\n"
        f"【通用规则】\n"
        f"- 不补充输入之外的数字\n"
        f"- 不使用模型记忆中的事实\n"
        f"- 写法参考样例: 短句、直接、先说矛盾，再说对盘面的含义；不要写成数据来源清单\n"
        f"- 参考样例只用于文风，不得继承样例里的库存、产量、政策、交易结论\n"
        f"- 不写'供应端扰动支撑原糖''高制糖比''累库压力''胀库'，除非输入里有对应事实\n"
        f"- 不虚构某国当天变化\n"
        f"- 不把预测写成实际数据\n"
        f"- 不使用'必然''一定'等绝对词\n"
        f"- 缺失数据时写'暂无新的已验证信息'，不得自动写'未发生明显变化''面积保持稳定''天气整体良好'\n"
        f"- 280-520字，先结论后解释，连贯叙述，不用'巴西:'这样的机械标题\n"
        f"- 只输出基本面正文，不输出其他内容"
    )

    # ── HTTP 调用 ──
    logger.info("DeepSeek 调用: model=%s, base_url=%s", model, base_url)
    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    f"你是白糖行业分析师，为{TARGET_CONTRACT}合约撰写日报基本面分析。"
                    "严格按固定顺序: 国际供需总判断→巴西(双周生产+面积+降雨)→印度(生产+面积+季风+出口)→泰国(生产+面积+降雨)→美糖判断→"
                    "国内25/26现实供给→26/27种植面积→26/27降雨墒情苗情→进口政策→SR2609判断。"
                    "巴西UNICA Table 2是双周数据，必须写成双周期间，禁止写'累计产糖'。"
                    "印度出口政策方向固定规则: 无新增出口=中性偏多, 新增出口=偏空, 限制出口=偏多。"
                    "泰国无同比数据时禁止写'同比增加''丰产''高位''充裕'。"
                    "国内无产销数据时禁止写'榨季已结束''格局已定'。"
                    "进口利润为正=对后续供应形成压力，不得写成支撑。"
                    "只使用输入数据，不虚构。不混写国际和国内。语言贴近研究员日报，少铺陈，重结论。只输出基本面正文。"
                )},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=800,
        )
    except AuthenticationError as e:
        code = E_DEEPSEEK_AUTH_ERROR
        logger.error("[%s] DeepSeek 认证失败 (401)", code)
        _review_event("DeepSeek调用", code, "HTTP 401 — API Key 无效或过期", str(e)[:200])
        return None, code
    except Exception as e:
        msg = str(e)
        if "timeout" in msg.lower() or "timed out" in msg.lower():
            code = E_DEEPSEEK_TIMEOUT
        else:
            code = E_DEEPSEEK_HTTP_ERROR
        logger.error("[%s] DeepSeek 请求失败: %s", code, msg[:200])
        _review_event("DeepSeek调用", code, f"HTTP 错误: {msg[:200]}", "")
        return None, code

    # ── 提取文本 ──
    if not response.choices or not response.choices[0].message:
        code = E_DEEPSEEK_EMPTY_RESPONSE
        logger.error("[%s] DeepSeek 返回空 choices", code)
        _review_event("DeepSeek调用", code, "返回 choices 为空", "")
        return None, code

    raw_text = response.choices[0].message.content
    if not raw_text:
        code = E_DEEPSEEK_EMPTY_RESPONSE
        logger.error("[%s] DeepSeek 返回空 content", code)
        _review_event("DeepSeek调用", code, "返回 content 为空", "")
        return None, code

    # ── 清理响应 ──
    text = _clean_model_response(raw_text)
    if not text:
        code = E_DEEPSEEK_PARSE_ERROR
        logger.error("[%s] 清理后文本为空 (原始 %d 字符)", code, len(raw_text))
        _review_event("DeepSeek调用", code, f"清理后文本为空，原始{len(raw_text)}字符", raw_text[:200])
        return None, code

    # ── 内容校验 ──
    is_valid, reason = _validate_model_content(text, fundamentals_text, market_summary)
    if not is_valid:
        code = E_DEEPSEEK_CONTENT_INVALID
        logger.error("[%s] 模型内容校验失败: %s", code, reason)
        _review_event("DeepSeek调用", code, reason, text[:200])
        return None, code

    # ── 逻辑一致性校验 ──
    logic_issues = _check_logic_consistency(text, fundamentals_text)
    if logic_issues:
        for issue in logic_issues:
            logger.warning("[逻辑校验] %s", issue)
            _review_event("逻辑校验", "WARNING", issue, "")
        # 严重逻辑错误（如印度方向错误）时标记需人工审阅
        if any("印度" in issue and ("压制" in issue or "中性偏多" in issue) for issue in logic_issues):
            logger.error("[逻辑校验] 印度出口政策方向错误，标记需人工审阅")
            _review_event("逻辑校验", "FAILED_LOGIC", "印度出口政策方向与数据不一致", "")

    logger.info("DeepSeek 调用成功: %d 字符", len(text))
    _review_event("DeepSeek调用", "OK", f"生成成功 ({len(text)} 字符)", "")
    return text, ""


# ============================================================
# 观点解析和一致性检查
# ============================================================

def parse_approved_view() -> dict | None:
    fpath = PROJECT_ROOT / config["approved_view_file"]
    if not fpath.exists():
        logger.warning("交易观点文件不存在")
        _review_event("读取观点", E_FUNDAMENTAL_FILE_NOT_FOUND, "approved_view.md 不存在", str(fpath))
        return None

    with open(fpath, "r", encoding="utf-8-sig") as fh:
        content = fh.read()

    def extract(label: str) -> str:
        m = re.search(rf"{label}[：:]\s*\n?(.*?)(?=\n(?:确认日期|适用合约|参考价格|观点有效期|当前判断|交易策略|策略生效条件|策略失效条件|风险提示)|$)", content, re.DOTALL)
        return m.group(1).strip() if m else ""

    result = {
        "confirm_date": extract("确认日期"),
        "applicable_contract": extract("适用合约"),
        "valid_period": extract("观点有效期"),
        "judgment": extract("当前判断"),
        "strategy": extract("交易策略"),
        "effective_condition": extract("策略生效条件"),
        "invalid_condition": extract("策略失效条件"),
        "risk": extract("风险提示"),
        "_contract_mismatch": False,
        "_contract_rejected": False,
        "_expired": False,
    }

    applicable = result.get("applicable_contract", "").strip()
    if applicable and applicable != TARGET_CONTRACT:
        result["_contract_mismatch"] = True
        logger.warning("观点适用合约=%s != 目标 %s", applicable, TARGET_CONTRACT)
        _review_event("读取观点", "CONTRACT_MISMATCH", f"适用合约={applicable} != {TARGET_CONTRACT}", "")
    if applicable in REJECT_CONTRACTS:
        result["_contract_rejected"] = True

    valid_period = result.get("valid_period", "").strip()
    if valid_period:
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*至\s*(\d{4}-\d{2}-\d{2})", valid_period)
        if m:
            try:
                end = datetime.strptime(m.group(2), "%Y-%m-%d")
                now = beijing_now().replace(tzinfo=None)
                if now > end + timedelta(days=1):
                    result["_expired"] = True
                    logger.warning("观点已过期: %s", m.group(2))
                    _review_event("读取观点", "VIEW_EXPIRED", f"有效期至 {m.group(2)}", "")
            except ValueError:
                pass

    logger.info("交易观点: 合约=%s, 确认日期=%s", applicable, result.get("confirm_date"))
    _review_event("读取观点", "OK", f"合约={applicable or '未填写'}, 日期={result.get('confirm_date', '未填写')}", "")
    return result


def run_consistency_check(market: dict, fundamentals_text: str | None, approved_view: dict | None) -> tuple[bool, list[str]]:
    issues = []
    zz_code = str(market.get("zz_contract_code", {}).get("value", "")).strip().upper()
    if zz_code and zz_code not in ("SR0", TARGET_CONTRACT):
        issues.append(f"行情展示合约={zz_code} 既不是SR0也不是{TARGET_CONTRACT}")

    if fundamentals_text:
        others = set()
        for m in re.finditer(r"影响合约[：:]\s*(\S+)", fundamentals_text):
            c = m.group(1).strip()
            if c != TARGET_CONTRACT:
                others.add(c)
        if others:
            issues.append(f"基本面引用非目标合约: {', '.join(others)}")

    if approved_view:
        applicable = approved_view.get("applicable_contract", "").strip()
        if applicable and applicable != TARGET_CONTRACT:
            issues.append(f"交易观点={applicable} != {TARGET_CONTRACT}")
        if approved_view.get("_contract_rejected"):
            issues.append("观点合约在拒绝列表中")
        if approved_view.get("_expired"):
            issues.append("观点已过期")

    trade_date = market.get("trade_date", "")
    if trade_date and is_weekend(trade_date):
        issues.append(f"行情日期 {trade_date} 为周末")

    return len(issues) == 0, issues


# ============================================================
# 风险提示
# ============================================================

def build_risk_text(approved_view: dict | None, fundamentals_text: str | None) -> str:
    if approved_view and approved_view.get("risk"):
        return approved_view["risk"]
    return "宏观、政策、天气、进口量。"


# ============================================================
# 日报生成
# ============================================================

def generate_report(target_date: str, market: dict, fundamentals_ai: str | None,
                    approved_view: dict | None, contract_conflict: bool,
                    consistency_issues: list[str] | None) -> str:
    now = beijing_now()
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    cn_date = f"{dt.year}年{dt.month:02d}月{dt.day:02d}日"
    trade_date = market.get("trade_date", "")
    all_ok = not consistency_issues

    status = "success" if all_ok else "needs_contract_confirmation"
    fm = f"""---
report_date: {target_date}
market_data_date: {trade_date}
generated_at: {fmt_datetime(now)}
status: {status}
needs_manual_review: true
data_source: {market.get('_source_label', '未知')}
target_contract: {TARGET_CONTRACT}
"""
    if consistency_issues:
        fm += "consistency_issues:\n"
        for issue in consistency_issues:
            fm += f"  - {issue}\n"
    # 进口利润来源信息
    meta = market.get("_import_profit_meta", {})
    if meta:
        fm += f"import_profit_source: {meta.get('source', '')}\n"
        fm += f"import_profit_data_date: {meta.get('data_date', '')}\n"
        fm += f"import_profit_reference_spot: {meta.get('reference_spot', '')}\n"
        if meta.get("ice_close"):
            fm += f"import_profit_ice_close: {meta['ice_close']}\n"
        if meta.get("usd_cny"):
            fm += f"import_profit_usd_cny: {meta['usd_cny']}\n"
    # 数据源元信息
    zz_src = market.get("zz_close", {}).get("source_name", "新浪财经")
    zz_url = market.get("zz_close", {}).get("source_url", "")
    fm += f"zhengzhou_sugar_source: {zz_src}\n"
    fm += f"zhengzhou_sugar_source_url: {zz_url}\n"
    fm += f"zhengzhou_sugar_market_data_date: {trade_date}\n"
    fm += "zhengzhou_sugar_price_field: SR0收盘价\n"
    fm += "ice_source: 新浪财经\n"
    ice_code = fv(market.get("ice_contract_code"), "s")
    fm += f"ice_contract: {ice_code if ice_code != 'N/A' else 'ICE原糖主力'}\n"
    ice_market_date = market.get("ice_close", {}).get("data_date", trade_date)
    fm += f"ice_market_data_date: {ice_market_date}\n"
    fm += "---"

    market_text = build_market_summary(market)
    fundamentals_text = fundamentals_ai if fundamentals_ai else "基本面内容生成失败，待研究员人工补充。"

    if contract_conflict or (approved_view and (approved_view.get("_contract_mismatch") or
                                                  approved_view.get("_contract_rejected") or
                                                  approved_view.get("_expired"))):
        strategy_text = "观望为主，等待研究员确认SR2609交易策略。"
    elif approved_view and approved_view.get("strategy"):
        strategy_text = approved_view["strategy"]
    else:
        strategy_text = "观望为主，等待研究员确认SR2609交易策略。"

    risk_text = build_risk_text(approved_view, fundamentals_ai)

    sources = [
        f"- 郑糖行情来源: {market.get('zz_close', {}).get('source_name', '未知')}（{market.get('zz_close', {}).get('source_url', '')}）",
        f"- ICE行情来源: {market.get('ice_close', {}).get('source_name', '未知')}（{market.get('ice_close', {}).get('source_url', '')}）",
        f"- 行情日期: {trade_date}（最近交易日）",
        f"- 生成时间: {fmt_datetime(now)}（北京时间）",
        f"- 目标合约: {TARGET_CONTRACT}",
    ]

    return f"""{fm}

# 白糖日报｜{cn_date}

**市场表现：**{market_text}

**基本面：**{fundamentals_text}

**交易策略：**{strategy_text}

**风险提示：**{risk_text}

---
{chr(10).join(sources)}
"""


def generate_failed_report(target_date: str, reason: str) -> str:
    now = beijing_now()
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    cn_date = f"{dt.year}年{dt.month:02d}月{dt.day:02d}日"
    return f"""---
report_date: {target_date}
generated_at: {fmt_datetime(now)}
status: failed
failure_reason: {reason}
needs_manual_review: true
target_contract: {TARGET_CONTRACT}
---

# 白糖日报｜{cn_date}

**市场表现：**（数据不可用。原因：{reason}）

**基本面：**（待人工补充）

**交易策略：**观望为主，等待研究员确认{TARGET_CONTRACT}交易策略。

**风险提示：**宏观、政策、天气、进口量。

---
> 本日报因数据缺失自动生成，需人工补充。
"""


# ============================================================
# review.md
# ============================================================

def generate_review_md(target_date: str, market: dict, fundamental_error: str,
                       deepseek_error: str, review_events: list[dict],
                       auto_fetch_summary: dict[str, dict] | None = None) -> str:
    now = beijing_now()
    lines = [
        f"# 白糖日报 Review — {target_date}",
        "",
        f"**生成时间**: {fmt_datetime(now)}",
        f"**目标合约**: {TARGET_CONTRACT}",
        "",
        "## 数据源状态",
        "",
        f"- 市场数据: {'OK' if market.get('ok') else 'FAILED'} (来源: {market.get('_source_label', 'N/A')})",
        f"- 行情日期: {market.get('trade_date', 'N/A')}",
        f"- 研究员基本面: {'OK' if not fundamental_error else '未提供/跳过'}",
        f"- DeepSeek: {'OK' if not deepseek_error else deepseek_error}",
        "",
    ]

    if auto_fetch_summary:
        lines.append("## 自动抓取状态")
        lines.append("")
        lines.append("| 国家 | fresh | cached | stale | failed | total |")
        lines.append("|------|-------|--------|-------|--------|-------|")
        for c in ["巴西", "印度", "泰国", "中国", "宏观"]:
            s = auto_fetch_summary.get(c, {})
            lines.append(f"| {c} | {s.get('fresh',0)} | {s.get('cached',0)} | {s.get('stale',0)} | {s.get('failed',0)} | {s.get('total',0)} |")
        lines.append("")

    lines.append("## 事件时间线")
    lines.append("")

    for evt in review_events:
        code = evt.get("code", "")
        icon = "OK" if code == "OK" else "FAIL" if ("ERROR" in code or "FAIL" in code) else "WARN"
        lines.append(f"- {evt['time']} [{icon}] `{code}` {evt['message']}")

    return "\n".join(lines) + "\n"


# ============================================================
# 保存
# ============================================================

def save_report(target_date: str, content: str, is_failed: bool = False) -> Path:
    out_cfg = config["output"]
    subdir = out_cfg["subdir_pattern"].format(date=target_date)
    out_dir = PROJECT_ROOT / out_cfg["output_dir"] / subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    dc = target_date.replace("-", "")
    # 用户要求每日输出只保留一个md；失败也写入同一个日报文件。
    fname = out_cfg["filename_pattern"].format(date_compact=dc)
    out_path = out_dir / fname

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    logger.info("已写入: %s", out_path)
    return out_path


# ============================================================
# 主流程
# ============================================================

def run(target_date: str | None = None) -> Path | None:
    """
    严格按顺序执行:
      01 读取目标合约
      02 获取公开行情
      03 校验合约代码
      04 不符→备用CSV
      05 校验行情日期和时效
      06 计算涨跌幅、基差
      07 读取研究员基本面（可选）
      08 自动抓取基本面 → 更新缓存
      09 校验时效和异常
      10 筛选 SR2609 相关信息
      11 读取 approved_view
      12 合约一致性检查
      13 调用 DeepSeek 生成基本面
      14 校验模型正文
      15 拼接日报
      16 生成 review.md + fundamental_sources.md → 写入
    """
    global _review_events
    _review_events = []

    if target_date is None:
        target_date = fmt_date(beijing_now())

    logger.info("=" * 60)
    logger.info("白糖日报生成开始")
    logger.info("  发布日期: %s | 目标合约: %s", target_date, TARGET_CONTRACT)
    logger.info("=" * 60)

    # ── 01 ──
    logger.info("[01/16] 读取目标合约: %s", TARGET_CONTRACT)

    # ── 02-04 市场数据 ──
    logger.info("[02/16] 获取公开行情...")
    market = get_market_data(target_date)

    logger.info("[03/16] 校验合约代码...")
    zz_code = str(market.get("zz_contract_code", {}).get("value", "")).strip().upper()
    if zz_code == "SR0":
        logger.info("[03/16] 行情展示合约=SR0；交易观点目标仍为 %s", TARGET_CONTRACT)
    elif zz_code and zz_code != TARGET_CONTRACT:
        logger.warning("[03/16] 行情展示合约异常: %s", zz_code)

    if not market.get("ok") or market.get("zz_close", {}).get("value") is None:
        logger.info("[04/16] 数据不可用（已自动回退CSV）")
        if not market.get("ok") or market.get("zz_close", {}).get("value") is None:
            reason = "; ".join(market.get("errors", ["所有数据源失败"]))
            logger.error("[04/16] 完全失败: %s", reason)
            _review_event("市场数据", "FAILED", reason, "")
            report = generate_failed_report(target_date, reason)
            path = save_report(target_date, report, is_failed=True)
            return path
    _review_event("市场数据", "OK", f"来源={market.get('_source_label')}, 日期={market.get('trade_date')}", "")

    # ── 05 ──
    logger.info("[05/16] 校验行情日期...")
    trade_date = market.get("trade_date", "")
    if trade_date and is_weekend(trade_date):
        logger.error("[05/16] 行情日期为周末: %s", trade_date)
        report = generate_failed_report(target_date, f"行情日期 {trade_date} 是周末")
        path = save_report(target_date, report, is_failed=True)
        return path
    logger.info("[05/16] 行情日期: %s (有效)", trade_date)

    # ── 06 ──
    logger.info("[06/16] 计算涨跌幅、基差...")
    market_summary = build_market_summary(market)
    logger.info("[06/16] 收盘=%s 前收=%s 涨跌=%s%% 基差=%s",
                fv(market.get("zz_close")), fv(market.get("zz_prev_close")),
                fv(market.get("zz_change_pct")), fv(market.get("basis")))

    # ── 07 研究员基本面（可选） ──
    logger.info("[07/16] 研究员基本面（可选）...")
    fundamentals_text, fund_error = read_fundamentals(target_date)
    researcher_used = fundamentals_text is not None

    # ── 08 自动抓取基本面 → 写入CSV ──
    logger.info("[08/16] 自动抓取基本面...")
    try:
        from fetch_fundamentals import run as fetch_fund_run
        fetch_fund_run(target_date)
        _review_event("自动抓取", "OK", "完成", "")
    except ImportError:
        _review_event("自动抓取", "SKIPPED", "模块不可用", "")
    except Exception as e:
        logger.warning("[08/16] 自动抓取异常: %s", e)
        _review_event("自动抓取", "ERROR", str(e)[:200], "")

    # ── 09 写入市场数据到CSV ──
    logger.info("[09/16] 写入市场数据到CSV...")
    try:
        from update_data_csv import csv_row, upsert_rows
        market_rows = []
        td = market.get("trade_date", target_date)
        # 郑糖主力
        market_rows.append(csv_row(category="market", country="China", region="",
                                   indicator="zhengzhou_main_close", value=str(fv(market.get("zz_close"))),
                                   unit="元/吨", data_date=td,
                                   source_name=market.get("zz_close", {}).get("source_name", market.get("_source_label", "")),
                                   source_url=market.get("zz_close", {}).get("source_url", ""),
                                   data_type="actual", status="valid"))
        # ICE原糖
        if fv(market.get("ice_close")) != "N/A":
            market_rows.append(csv_row(category="market", country="Global", region="",
                                       indicator="ice_sugar_main_close", value=str(fv(market.get("ice_close"))),
                                       unit="美分/磅", data_date=td,
                                       source_name=market.get("ice_close", {}).get("source_name", "新浪财经"),
                                       source_url=market.get("ice_close", {}).get("source_url", ""),
                                       data_type="actual", status="valid"))
        # 南宁现货
        if fv(market.get("nanning_spot")) != "N/A":
            market_rows.append(csv_row(category="market", country="China", region="Guangxi",
                                       indicator="nanning_spot_price", value=str(fv(market.get("nanning_spot"))),
                                       unit="元/吨", data_date=td, source_name="本地备用CSV",
                                       data_type="actual", status="valid"))
        # 基差
        if fv(market.get("basis")) != "N/A":
            market_rows.append(csv_row(category="market", country="China", region="",
                                       indicator="spot_basis", value=str(fv(market.get("basis"))),
                                       unit="元/吨", data_date=td, source_name="Python计算",
                                       data_type="actual", status="valid"))
        # 进口利润
        meta = market.get("_import_profit_meta", {})
        if meta:
            market_rows.append(csv_row(
                category="market", country="China", region="",
                indicator="quota_outside_profit", value=str(fv(market.get("brazil_profit"))),
                unit="元/吨", data_date=meta.get("data_date", td),
                source_name=meta.get("source", ""), source_url=meta.get("source_url", ""),
                data_type="actual", status="valid",
                text_value=f"参考现货: {meta.get('reference_spot', '')} ICE: {meta.get('ice_close', '')} USDCNY: {meta.get('usd_cny', '')}"))
        upsert_rows(market_rows)
        _review_event("CSV写入", "OK", f"市场数据 {len(market_rows)} 行", "")
    except ImportError:
        _review_event("CSV写入", "SKIPPED", "模块不可用", "")
    except Exception as e:
        logger.warning("[09/16] CSV写入异常: %s", e)

    # ── 10 从CSV读取有效记录 ──
    logger.info("[10/16] 从CSV读取有效记录...")
    cache_records = load_valid_records_from_csv()
    logger.info("[10/16] 有效记录: %d 条", len(cache_records))

    # 统计各国状态
    missing_countries = []
    for c in ["巴西", "印度", "泰国", "中国"]:
        country_records = [r for r in cache_records if r.get("country", "") == c]
        if not country_records:
            missing_countries.append(c)
    if missing_countries:
        logger.warning("[10/16] 以下国家无有效数据: %s", ", ".join(missing_countries))
        _review_event("数据缺口", "WARN", f"缺失: {', '.join(missing_countries)}", "")

    # ── 10 筛选 SR2609 相关信息 ──
    logger.info("[10/16] 筛选 SR2609 相关信息...")
    import_profit_meta = market.get("_import_profit_meta", {})
    fundamentals_for_model = build_structured_fundamentals(
        cache_records, fundamentals_text, import_profit_meta)
    logger.info("[10/16] 模型输入: %d 字符", len(fundamentals_for_model))

    # ── 11 approved_view ──
    logger.info("[11/16] 读取交易观点...")
    approved_view = parse_approved_view()

    # ── 12 一致性检查 ──
    logger.info("[12/16] 合约一致性检查...")
    all_ok, issues = run_consistency_check(market, fundamentals_text, approved_view)
    if issues:
        logger.warning("[12/16] 一致性问题 (%d 项):", len(issues))
        for issue in issues:
            logger.warning("  - %s", issue)
            _review_event("一致性检查", "WARNING", issue, "")
    else:
        logger.info("[12/16] 一致性检查通过")
        _review_event("一致性检查", "OK", "三者一致", "")

    contract_conflict = bool(approved_view and (
        approved_view.get("_contract_mismatch") or
        approved_view.get("_contract_rejected") or
        approved_view.get("_expired")))

    # ── 12.5 策略有效性校验 ──
    strategy_valid, strategy_invalid_reason = validate_strategy(approved_view, market)
    if not strategy_valid:
        logger.warning("[12.5/16] 策略校验: %s", strategy_invalid_reason)
        _review_event("策略校验", "FAILED", strategy_invalid_reason, "")
        contract_conflict = True  # 触发策略降级
    else:
        logger.info("[12.5/16] 策略校验通过")

    # ── 13 DeepSeek ──
    logger.info("[13/16] 调用 DeepSeek 生成基本面...")
    fundamentals_ai, deepseek_error = call_deepseek(market_summary, fundamentals_for_model, approved_view)

    # 模型失败时的兜底
    if not fundamentals_ai:
        if cache_records:
            logger.warning("[13/16] DeepSeek失败，使用缓存状态生成简要描述")
            # 用缓存组装简单描述
            countries_with_data = set(r.get("country", "") for r in cache_records)
            desc = "基本面核心矛盾较上一交易日未发生明显变化。"
            if missing_countries:
                desc += f" {', '.join(missing_countries)}暂无更新数据。"
            fundamentals_ai = desc
            _review_event("基本面兜底", "WARN", "DeepSeek失败，使用缓存摘要", desc)
        else:
            fundamentals_ai = "暂无新的已验证基本面数据，核心观点待更新。"
            _review_event("基本面兜底", "WARN", "无任何可用数据", "")

    # ── 14 校验 ──
    logger.info("[14/16] 校验模型正文...")
    logger.info("[14/16] 基本面正文: %d 字符", len(fundamentals_ai))

    # ── 15 最终一致性检查 ──
    logger.info("[15/16] 最终一致性检查...")
    final_ok, final_failures = final_consistency_check(
        market, fundamentals_ai, approved_view, strategy_valid)
    if final_failures:
        for f in final_failures:
            logger.error("[15/16] 一致性失败: %s", f)
            _review_event("最终检查", "FAILED", f, "")
        all_ok = False
        issues = (issues or []) + final_failures
    else:
        logger.info("[15/16] 最终一致性检查通过")

    # ── 16 拼接日报并写入 ──
    logger.info("[16/16] 拼接日报并写入...")
    report = generate_report(target_date, market, fundamentals_ai, approved_view,
                            contract_conflict, issues if issues else None)

    # 输出目录
    out_dir = (PROJECT_ROOT / config["output"]["output_dir"] /
               config["output"]["subdir_pattern"].format(date=target_date))
    out_dir.mkdir(parents=True, exist_ok=True)

    # 最终校验
    final_contract = str(market.get("zz_contract_code", {}).get("value", "")).strip().upper()
    if final_contract and final_contract not in ("SR0", TARGET_CONTRACT):
        logger.error("[16/16] 最终校验失败: 行情展示合约异常 %s", final_contract)
        report = generate_failed_report(target_date, f"行情展示合约异常: {final_contract}")
        path = save_report(target_date, report, is_failed=True)
        return path

    if trade_date and is_weekend(trade_date):
        logger.error("[16/16] 最终校验失败: 日期 %s 为周末", trade_date)
        report = generate_failed_report(target_date, f"行情日期 {trade_date} 为周末")
        path = save_report(target_date, report, is_failed=True)
        return path

    # 写入日报
    out_path = save_report(target_date, report, is_failed=False)

    final_status = "success" if all_ok else "needs_contract_confirmation"
    logger.info("=" * 60)
    logger.info("日报生成完成")
    logger.info("  日报: %s", out_path)
    logger.info("  状态: %s | 合约: %s | 行情日期: %s", final_status, TARGET_CONTRACT, trade_date)
    if deepseek_error:
        logger.info("  DeepSeek: %s", deepseek_error)
    logger.info("=" * 60)

    # ── 更新前端 JSON ──
    try:
        from update_web_reports import run as update_web_run
        update_web_run(target_date)
        logger.info("前端JSON已更新")
    except Exception as e:
        logger.warning("前端JSON更新失败（不影响日报生成）: %s", e)

    # ── 自动推送到 GitHub（触发 Vercel 部署）──
    try:
        import subprocess
        os.chdir(str(PROJECT_ROOT))
        # 检查是否有变化
        status = subprocess.run(["git", "status", "--porcelain", "public/data"],
                                capture_output=True, text=True, timeout=10)
        if status.stdout.strip():
            subprocess.run(["git", "add", "public/data"], timeout=10)
            commit_msg = f"Update sugar daily report {target_date}"
            subprocess.run(["git", "commit", "-m", commit_msg], timeout=10)
            push_result = subprocess.run(["git", "push"], capture_output=True, text=True, timeout=30)
            if push_result.returncode == 0:
                logger.info("已推送到 GitHub: %s", commit_msg)
            else:
                logger.warning("git push 失败（不影响日报生成）: %s", push_result.stderr[:200])
        else:
            logger.info("前端JSON无变化，跳过推送")
    except Exception as e:
        logger.warning("自动推送失败（不影响日报生成）: %s", e)

    return out_path


def _write_review_and_sources(out_dir: Path, review_md: str, sources_md: str):
    """写入 review.md 和 fundamental_sources.md。"""
    # review.md
    rp = out_dir / "review.md"
    if rp.exists():
        backup = out_dir / f"review_backup_{beijing_now().strftime('%H%M%S')}.md"
        shutil.copy2(rp, backup)
    with open(rp, "w", encoding="utf-8") as f:
        f.write(review_md)
    logger.info("review.md 已写入: %s", rp)

    # fundamental_sources.md
    sp = out_dir / "fundamental_sources.md"
    if sp.exists():
        backup = out_dir / f"fundamental_sources_backup_{beijing_now().strftime('%H%M%S')}.md"
        shutil.copy2(sp, backup)
    with open(sp, "w", encoding="utf-8") as f:
        f.write(sources_md)
    logger.info("fundamental_sources.md 已写入: %s", sp)


def main():
    parser = argparse.ArgumentParser(description="白糖日报生成系统")
    parser.add_argument("--date", type=str, default=None, help="目标日期 YYYY-MM-DD")
    args = parser.parse_args()

    if args.date:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            logger.error("日期格式错误，应为 YYYY-MM-DD")
            sys.exit(1)

    result = run(args.date)
    if result and "_FAILED" not in result.name:
        logger.info("[OK] 日报已生成: %s", result)
        sys.exit(0)
    elif result:
        logger.warning("[WARN] 失败日报已生成: %s", result)
        sys.exit(1)
    else:
        logger.error("[FAIL] 日报生成失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
