from pathlib import Path

from evaluation.evaluate import evaluate


def test_evaluation_has_at_least_30_varied_examples_and_reports_safety_metrics() -> None:
    dataset = Path(__file__).resolve().parents[1] / "evaluation/mail_eval.jsonl"
    assert len(dataset.read_text(encoding="utf-8").splitlines()) >= 30

    metrics = evaluate(dataset)

    assert metrics["importance_accuracy"] >= 0.8
    assert metrics["needs_reply_precision"] >= 0.9
    assert metrics["needs_reply_recall"] >= 0.9
    assert metrics["category_accuracy"] >= 0.8
    assert metrics["critical_high_miss_rate"] <= 0.1
    assert metrics["false_urgent_rate"] <= 0.1
