#!/usr/bin/env python3
from __future__ import annotations
"""
解析 outputs/ 中的白糖日报 Markdown，生成前端 JSON。
用法:
  python scripts/update_web_reports.py
  python scripts/update_web_reports.py --date 2026-06-14
"""

import argparse
import json
import re
import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
PUBLIC_DATA_DIR = PROJECT_ROOT / "public" / "data"
REPORTS_DIR = PUBLIC_DATA_DIR / "reports"
INDEX_PATH = PUBLIC_DATA_DIR / "reports.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("update_web_reports")


def beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def parse_frontmatter(text: str) -> dict:
    """提取 YAML frontmatter。"""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).split("\n"):
        line = line.strip()
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta


def extract_section(body: str, label: str) -> str:
    """
    提取 **label：** 或 label： 后的正文，直到下一个同级标题或结尾。
    """
    # Pattern 1: **市场表现：**正文
    # Pattern 2: 市场表现：正文
    # Match until next **X：** or end
    patterns = [
        rf"\*\*{label}[：:]\*\*\s*(.*?)(?=\n\*\*[^*]+[：:]|\Z)",
        rf"(?:^|\n){label}[：:]\s*(.*?)(?=\n(?:市场表现|基本面|交易策略|风险提示)[：:]|\Z)",
    ]
    for pat in patterns:
        m = re.search(pat, body, re.S)
        if m:
            return m.group(1).strip()
    return ""


def parse_report_md(filepath: Path) -> dict | None:
    """解析单个日报 Markdown 文件。"""
    try:
        text = filepath.read_text(encoding="utf-8-sig")
    except Exception as e:
        logger.error("读取失败 %s: %s", filepath, e)
        return None

    meta = parse_frontmatter(text)
    if not meta:
        logger.warning("无 frontmatter: %s", filepath)
        return None

    report_date = meta.get("report_date", "")
    if not report_date:
        logger.warning("无 report_date: %s", filepath)
        return None

    # 提取正文（frontmatter 之后）
    body_match = re.search(r"^---\s*\n.*?\n---\s*\n(.*)", text, re.S)
    body = body_match.group(1).strip() if body_match else ""

    # 去掉标题行
    body = re.sub(r"^#\s+白糖日报[^\n]*\n*", "", body)

    # 去掉末尾的来源说明部分（---之后的内容）
    body = re.sub(r"\n---\s*\n.*$", "", body, flags=re.S)

    market = extract_section(body, "市场表现")
    fundamentals = extract_section(body, "基本面")
    strategy = extract_section(body, "交易策略")
    risk = extract_section(body, "风险提示")

    if not market and not fundamentals:
        logger.warning("无法提取市场表现或基本面: %s", filepath)
        return None

    # 构建全文
    full_parts = []
    if market:
        full_parts.append(f"市场表现：{market}")
    if fundamentals:
        full_parts.append(f"基本面：{fundamentals}")
    if strategy:
        full_parts.append(f"交易策略：{strategy}")
    if risk:
        full_parts.append(f"风险提示：{risk}")
    full_text = "\n\n".join(full_parts)

    # 标题取市场表现第一句
    title = market.split("。")[0] if market else f"白糖日报 {report_date}"

    # preview 取基本面前 80 字
    preview = fundamentals[:80] + "..." if len(fundamentals) > 80 else fundamentals

    # 生成时间
    generated_at = meta.get("generated_at", "")

    return {
        "date": report_date,
        "title": "白糖日报",
        "short_title": title,
        "market_data_date": meta.get("market_data_date", ""),
        "generated_at": generated_at,
        "target_contract": meta.get("target_contract", ""),
        "status": meta.get("status", ""),
        "needs_manual_review": meta.get("needs_manual_review", "") == "true",
        "market_performance": market,
        "fundamentals": fundamentals,
        "strategy": strategy,
        "risk": risk,
        "full_text": full_text,
        "preview": preview,
    }


def save_daily_json(report: dict):
    """保存单日 JSON。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date = report["date"]
    path = REPORTS_DIR / f"{date}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info("已写入: %s", path)


def build_index(all_reports: list[dict]):
    """构建并保存 reports.json 索引。"""
    # 按日期倒序
    all_reports.sort(key=lambda r: r["date"], reverse=True)

    index = {
        "updated_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        "reports": [
            {
                "date": r["date"],
                "title": r["short_title"],
                "preview": r["preview"],
                "file": f"/public/data/reports/{r['date']}.json",
            }
            for r in all_reports
        ],
    }

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    logger.info("索引已更新: %s (%d 篇日报)", INDEX_PATH, len(all_reports))


def find_all_reports() -> list[Path]:
    """找到 outputs/ 中所有日报 Markdown。"""
    reports = []
    if not OUTPUTS_DIR.exists():
        return reports
    for subdir in sorted(OUTPUTS_DIR.iterdir(), reverse=True):
        if not subdir.is_dir():
            continue
        for md_file in subdir.glob("白糖日报_*.md"):
            reports.append(md_file)
    return reports


def load_existing_index() -> dict:
    """加载现有索引。"""
    if INDEX_PATH.exists():
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"updated_at": "", "reports": []}


def run(target_date: str | None = None):
    """主入口。"""
    existing_index = load_existing_index()
    existing_dates = {r["date"] for r in existing_index.get("reports", [])}

    if target_date:
        # 只处理指定日期
        md_files = []
        for subdir in OUTPUTS_DIR.iterdir():
            if subdir.is_dir():
                for md_file in subdir.glob(f"白糖日报_{target_date.replace('-', '')}.md"):
                    md_files.append(md_file)
        if not md_files:
            logger.error("未找到 %s 的日报文件", target_date)
            return
    else:
        md_files = find_all_reports()

    if not md_files:
        logger.warning("outputs/ 中没有日报文件")
        return

    all_reports = []
    processed_dates = set()

    for md_file in md_files:
        report = parse_report_md(md_file)
        if not report:
            continue
        date = report["date"]
        if date in processed_dates:
            continue
        processed_dates.add(date)
        save_daily_json(report)
        all_reports.append(report)

    # 合并已有索引中未被覆盖的日报
    for r in existing_index.get("reports", []):
        if r["date"] not in processed_dates:
            # 尝试加载已有 JSON
            json_path = REPORTS_DIR / f"{r['date']}.json"
            if json_path.exists():
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        existing_report = json.load(f)
                    all_reports.append(existing_report)
                except Exception:
                    pass

    build_index(all_reports)


def main():
    parser = argparse.ArgumentParser(description="更新前端日报 JSON")
    parser.add_argument("--date", type=str, default=None, help="只处理指定日期 YYYY-MM-DD")
    args = parser.parse_args()
    run(args.date)


if __name__ == "__main__":
    main()
