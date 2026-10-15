"""Labeled evaluation corpus for PR 2 deterministic capture (capture.py).

Smaller than the 100-turn corpus the PR 2 spec suggests (40 labeled
utterances across the required categories) — see
docs/plans/2026-10-15-holographic-pr2-capture-consolidation.md for why a
smaller, honestly-measured corpus was used instead of a fabricated 100-case
one. Numbers measured here are asserted as regression floors and also
printed, so `pytest -s` shows the actual current precision/recall.

Label meaning: `should_capture=True` means at least one candidate SHOULD be
extracted from this utterance; False means none should be. This measures
capture.extract_candidates() only — not consolidation or the write firewall,
which have their own test coverage in test_holographic_capture.py.
"""

# (text, should_capture, category) — category is for reporting only.
_CORPUS = [
    # Durable preferences (positive)
    ("I use Python uv for MCP projects.", True, "preference"),
    ("I prefer dark mode for all my editors.", True, "preference"),
    ("My preferred editor is Vim.", True, "preference"),
    ("I always check git status before committing.", True, "preference"),
    ("From now on, use tabs instead of spaces.", True, "preference"),

    # Project decisions (positive)
    ("Project Alpha uses PostgreSQL.", True, "project"),
    ("My Solana analytics uses PostgreSQL.", True, "project"),
    ("We decided to use Rust for the backend.", True, "project"),
    ("I've decided to deploy on GCP.", True, "project"),
    ("My MCP projects use Python uv.", True, "project"),

    # Corrections (positive)
    ("I've decided to switch my Solana analytics database to ClickHouse.", True, "correction"),
    ("I've switched Solana analytics to ClickHouse.", True, "correction"),
    ("I no longer use TypeScript.", True, "correction"),
    ("Actually, we're using SQLite now.", True, "correction"),
    ("Replace the old API with the new endpoint.", True, "correction"),

    # Explicit memory management (positive, operation=forget)
    ("Forget my preferred programming language.", True, "forget"),
    ("Forget my editor preference.", True, "forget"),
    ("Don't remember this information.", True, "forget"),
    ("Remove the memory about my database choice.", True, "forget"),

    # Questions (negative)
    ("What database am I using for my Solana analytics?", False, "question"),
    ("Which editor do I prefer?", False, "question"),
    ("What is PostgreSQL?", False, "question"),
    ("Can you explain how ClickHouse works?", False, "question"),
    ("Why would I use uv over pip?", False, "question"),

    # Temporary / incidental statements (negative)
    ("Hello, how are you?", False, "chatter"),
    ("Thanks, that worked!", False, "chatter"),
    ("ok cool", False, "chatter"),
    ("Let me check that real quick.", False, "chatter"),

    # Hypothetical examples (negative)
    ("For example, I might use PostgreSQL for a toy project.", False, "hypothetical"),
    ("We're evaluating ClickHouse.", False, "hypothetical"),
    ("Suppose I switched to MySQL, what would break?", False, "hypothetical"),
    ("What if we used SQLite instead?", False, "hypothetical"),

    # Quoted / reported speech (negative)
    ('"I love PostgreSQL", said the article.', False, "quoted"),
    ("The docs said PostgreSQL scales well.", False, "quoted"),

    # Credentials (positive extraction allowed — firewall rejects at write time, not here)
    ("My API key is sk-proj-abc123def456ghi789jkl012.", True, "credential"),
    ("Here is my password: hunter2hunter2.", True, "credential"),

    # Synthetic/compaction-style summaries (negative — third-person, not a direct statement)
    ("Summary: the user mentioned preferring PostgreSQL earlier in the conversation.", False, "compaction"),
    ("[Context] Previously discussed: user's database is PostgreSQL.", False, "compaction"),

    # Raw tool/debug output (negative)
    ("Traceback (most recent call last): File \"x.py\", line 1", False, "stacktrace"),
    ("HTTP 500 Internal Server Error returned by the API.", False, "stacktrace"),
]


def test_capture_precision_recall_on_labeled_corpus(capsys):
    from plugins.memory.holographic import capture

    tp = fp = tn = fn = 0
    misses = []
    for text, should_capture, category in _CORPUS:
        captured = bool(capture.extract_candidates(text))
        if should_capture and captured:
            tp += 1
        elif should_capture and not captured:
            fn += 1
            misses.append(("missed", category, text))
        elif not should_capture and captured:
            fp += 1
            misses.append(("false_positive", category, text))
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0

    with capsys.disabled():
        print(f"\nholographic capture eval (n={len(_CORPUS)}): "
              f"precision={precision:.2f} recall={recall:.2f} "
              f"tp={tp} fp={fp} tn={tn} fn={fn}")
        for kind, category, text in misses:
            print(f"  {kind} [{category}]: {text!r}")

    # Regression floors measured against this corpus — see the PR 2 doc for
    # the actual numbers and the two documented, intentional gaps above
    # (credentials are allowed through extraction and caught by the write
    # firewall instead; "I always check X" isn't a recognized pattern yet).
    assert precision >= 0.85
    assert recall >= 0.75
