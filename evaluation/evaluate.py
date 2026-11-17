#!/usr/bin/env python3
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.email_analyzer import DemoAnalyzer
from app.email.thread_builder import build_threads
from app.schemas.mail import MailMessage


HIGH_LEVELS = {"CRITICAL", "HIGH"}


def evaluate(dataset_path: Path = ROOT / "evaluation/mail_eval.jsonl") -> Dict[str, float]:
    records = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    analyzer = DemoAnalyzer()
    predicted: List[dict] = []
    for index, record in enumerate(records):
        message = MailMessage(
            message_id=f"eval-{index}",
            thread_id=f"eval-{index}",
            sender_name=record.get("sender_name", ""),
            sender_email=record.get("sender_email", "sender@example.com"),
            subject=record["subject"],
            received_at=datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc),
            body_text=record["body"],
        )
        result = analyzer.analyze(build_threads([message])[0])
        predicted.append(result.model_dump(mode="json"))

    importance_correct = sum(
        result["importance"] == record["expected_importance"]
        for result, record in zip(predicted, records)
    )
    category_correct = sum(
        result["category"] == record["expected_category"]
        for result, record in zip(predicted, records)
    )
    true_positive = sum(
        result["needs_reply"] and record["expected_needs_reply"]
        for result, record in zip(predicted, records)
    )
    false_positive = sum(
        result["needs_reply"] and not record["expected_needs_reply"]
        for result, record in zip(predicted, records)
    )
    false_negative = sum(
        not result["needs_reply"] and record["expected_needs_reply"]
        for result, record in zip(predicted, records)
    )
    expected_high = sum(record["expected_importance"] in HIGH_LEVELS for record in records)
    missed_high = sum(
        record["expected_importance"] in HIGH_LEVELS and result["importance"] not in HIGH_LEVELS
        for result, record in zip(predicted, records)
    )
    expected_low = sum(record["expected_importance"] in {"LOW", "IGNORE"} for record in records)
    false_urgent = sum(
        record["expected_importance"] in {"LOW", "IGNORE"} and result["importance"] in HIGH_LEVELS
        for result, record in zip(predicted, records)
    )
    total = len(records)
    return {
        "examples": total,
        "importance_accuracy": importance_correct / total,
        "needs_reply_precision": true_positive / max(1, true_positive + false_positive),
        "needs_reply_recall": true_positive / max(1, true_positive + false_negative),
        "category_accuracy": category_correct / total,
        "critical_high_miss_rate": missed_high / max(1, expected_high),
        "false_urgent_rate": false_urgent / max(1, expected_low),
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), indent=2, sort_keys=True))
