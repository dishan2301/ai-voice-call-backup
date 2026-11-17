"""Conservative extraction of literal promises; false negatives beat invented promises."""
import re


PROMISE = re.compile(
    r"\b(?:i|we)(?:'ll|\s+will)\b.{0,160}\b(?:send|share|provide|revert|reply|deliver|pay|confirm|update|complete|finish|submit|call)\b",
    re.IGNORECASE,
)
DUE = re.compile(
    r"\b(?:(?:by|before|on)\s+)?(?:today|tomorrow|tonight|(?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|\d{1,2}(?:st|nd|rd|th)?(?:\s+[A-Za-z]+(?:\s+\d{2,4})?)?)\b",
    re.IGNORECASE,
)


def extract_commitments(body: str, labels) -> list[dict]:
    direction = "YOU_OWE" if "SENT" in {str(label).upper() for label in labels} else "THEY_OWE"
    sentences = re.split(r"(?<=[.!?])\s+|[\r\n]+", body or "")
    found = []
    for sentence in sentences:
        text = " ".join(sentence.split()).strip()
        if not text or len(text) > 500 or not PROMISE.search(text):
            continue
        # Relative/explicit dates are useful even without a preposition: “I’ll
        # send it tomorrow.” Keep the exact source phrase; never infer a date.
        due = DUE.search(text)
        found.append({"direction": direction, "text": text,
                      "due_text": due.group(0) if due else None})
    return found[:5]
