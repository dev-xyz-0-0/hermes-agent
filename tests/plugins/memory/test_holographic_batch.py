"""Phase 4 — Transactional batch ingestion tests.

See docs/plans/demo-reference-plan/checklist.md section 7 ("Phase 4 —
Transactional batch ingestion" and its "Batch tests" subsection) and
implementation-plan.md ("Phase 4 — Safe transactional batch ingestion" and
section 9.5, "Batch ingestion tests").
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from plugins.memory.holographic import backup as backup_module
from plugins.memory.holographic import batch
from plugins.memory.holographic import holographic as hrr
from plugins.memory.holographic.batch import (
    MAX_BATCH_SIZE,
    MAX_ITEM_CONTENT_LEN,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_FORGOTTEN,
    STATUS_INSERTED,
    STATUS_NOOP,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    BatchResult,
    SourceContext,
    ingest_candidates_batch,
)
from plugins.memory.holographic.capture import FactCandidate
from plugins.memory.holographic.retrieval import FactRetriever
from plugins.memory.holographic.store import MemoryStore, bank_name


@pytest.fixture
def numpy_enabled():
    if not hrr._HAS_NUMPY:
        pytest.skip("numpy is not installed in this environment")
    return True


@pytest.fixture
def store(tmp_path, numpy_enabled):
    s = MemoryStore(db_path=tmp_path / "batch.db", hrr_dim=32)
    yield s
    s.close()


@pytest.fixture
def retriever(store):
    return FactRetriever(store=store, hrr_dim=32)


def _cand(content: str, operation: str = "statement", scope: str = "", old_text: str = "") -> FactCandidate:
    return FactCandidate(content=content, operation=operation, scope=scope, old_text=old_text)


# Deliberately unrelated trivia sentences (not a shared word-substitution
# template) so consolidation's token-overlap/fuzzy-N-gram duplicate checks
# (_DUPLICATE_OVERLAP=0.6, _FUZZY_DUPLICATE_NGRAM_OVERLAP=0.4) never flag two
# of these as paraphrases of each other -- a templated "Fact number {i} about
# Topic{i}." pattern shares too many scaffold words across indices and gets
# auto-consolidated, which would make these tests about batching mechanics
# accidentally about consolidation policy instead.
_DISTINCT_SENTENCES = [
    "Dolphins communicate using a complex system of clicks and whistles.",
    "The Eiffel Tower was completed in eighteen eighty nine.",
    "Photosynthesis converts sunlight into chemical energy in plants.",
    "Mount Kilimanjaro is the tallest mountain in Africa.",
    "Honey never spoils if it is stored properly sealed.",
    "The human heart beats roughly one hundred thousand times daily.",
    "Octopuses have three separate hearts pumping their blood.",
    "Saturn has the most extensive ring system of any planet.",
    "Bamboo can grow nearly a meter in a single day.",
    "The Great Wall of China stretches thousands of kilometers.",
    "Venus rotates in the opposite direction from most planets.",
    "Sharks existed long before dinosaurs roamed the earth.",
    "The Amazon rainforest produces a significant share of oxygen.",
    "Glass is made primarily from melted sand and minerals.",
    "Penguins huddle together tightly to conserve body heat.",
    "The speed of light is nearly constant in a vacuum.",
    "Chess originated in India more than a thousand years ago.",
    "Coffee beans are technically the seeds of a cherry fruit.",
    "The Sahara desert expands and contracts with the seasons.",
    "Lightning can reach temperatures hotter than the surface of the sun.",
    "Jellyfish have survived on earth for over five hundred million years.",
    "The moon is slowly drifting away from the earth every year.",
    "Tomatoes were once believed to be poisonous in parts of Europe.",
    "A group of flamingos is called a flamboyance.",
    "The blue whale is the largest animal to have ever lived.",
    "Mount Everest grows a few millimeters taller every year.",
    "Cleopatra lived closer to our time than to the pyramids.",
    "Bananas are technically classified as a type of berry.",
    "The Pacific Ocean covers nearly a third of the earth's surface.",
    "Wombat droppings are shaped like perfect little cubes.",
    "Antarctica is technically classified as a desert continent.",
    "The human body contains enough carbon for thousands of pencils.",
    "Koalas sleep for nearly twenty hours out of every day.",
    "Niagara Falls freezes partially during extremely cold winters.",
    "The inventor of the telephone also worked on hearing aids.",
    "Crows can recognize human faces for several years afterward.",
    "The Sahara desert was once a lush green savanna.",
    "A single cloud can weigh more than a million pounds.",
    "The shortest war in history lasted under forty minutes.",
    "Giraffes only need a few minutes of sleep each day.",
    "The Great Barrier Reef is visible from outer space.",
    "Narwhals use their tusks to sense changes in water pressure.",
    "The Sun accounts for most of the mass in the solar system.",
    "Otters hold hands while sleeping so they do not drift apart.",
    "The Mariana Trench is deeper than Mount Everest is tall.",
    "Elephants can recognize themselves in a mirror reliably.",
    "The first computer programmer was a woman named Ada Lovelace.",
    "Vincent Van Gogh sold only one painting during his lifetime.",
    "Marie Curie won Nobel Prizes in two different scientific fields.",
    "Albert Einstein was offered the presidency of Israel once.",
]


def _distinct(i: int) -> str:
    if i >= len(_DISTINCT_SENTENCES):
        raise ValueError(f"need more fixture sentences for index {i}")
    return _DISTINCT_SENTENCES[i]


def _fact_count(store: MemoryStore, namespace: str = "") -> int:
    row = store._conn.execute(
        "SELECT COUNT(*) FROM facts WHERE namespace = ?", (namespace,)
    ).fetchone()
    return int(row[0])


def _bank_row(store: MemoryStore, namespace: str, category: str):
    return store._conn.execute(
        "SELECT * FROM memory_banks WHERE bank_name = ?",
        (bank_name(namespace, category),),
    ).fetchone()


class TestSuccessfulAtomicBatch:
    def test_commits_facts_entities_sources_and_bank(self, store, retriever):
        candidates = [
            _cand('Ada Lovelace wrote notes about the "Analytical Engine".'),
            _cand('Marie Curie discovered "radioactivity" early in her career.'),
            _cand('Vincent Van Gogh painted "Starry Night" in one sitting.'),
        ]
        result = ingest_candidates_batch(
            store, retriever, candidates,
            namespace="ns1", source=SourceContext(via="batch_test", platform="cli"),
        )
        assert result.committed is True
        assert result.counts[STATUS_INSERTED] == 3
        assert _fact_count(store, "ns1") == 3

        for item in result.items:
            assert item.fact_id is not None
            src = store._conn.execute(
                "SELECT via, platform FROM fact_sources WHERE fact_id = ?", (item.fact_id,)
            ).fetchone()
            assert src is not None
            assert src["via"] == "batch_test"
            assert src["platform"] == "cli"
            entity_links = store._conn.execute(
                "SELECT COUNT(*) FROM fact_entities WHERE fact_id = ?", (item.fact_id,)
            ).fetchone()[0]
            assert entity_links >= 1  # each sentence has at least one capitalized name

        bank = _bank_row(store, "ns1", "general")
        assert bank is not None
        assert bank["fact_count"] == 3

    def test_bank_rebuild_count_is_bounded_to_once_per_pair(self, store, retriever):
        """A 5-item batch into one (namespace, category) pair recomputes
        that bank's vector exactly once, not 5 times (implementation-
        plan.md Phase 4, requirement 6). `rebuild_bank()` itself is called
        once per mutation (5x, by add_fact) plus once to flush the deferred
        set -- that part is unavoidable call-site bookkeeping. What must be
        bounded is the actual expensive recompute (`hrr.bundle` over every
        fact vector in the bank), which only runs on a non-deferred call."""
        candidates = [_cand(_distinct(i)) for i in range(5)]
        # `rebuild_bank()`'s deferred-no-op branch returns before ever
        # computing `bank_name(...)` -- only a real, non-deferred rebuild
        # reaches that call -- so counting calls to it (rather than to
        # `rebuild_bank` itself, which legitimately gets called once per
        # mutation as cheap call-site bookkeeping) isolates the expensive
        # recompute work this requirement actually bounds.
        with patch("plugins.memory.holographic.store.bank_name", wraps=bank_name) as name_spy:
            result = ingest_candidates_batch(
                store, retriever, candidates,
                namespace="ns-bounded", source=SourceContext(),
            )
        assert result.counts[STATUS_INSERTED] == 5
        assert name_spy.call_count == 1
        assert name_spy.call_args[0] == ("ns-bounded", "general")


class TestMidBatchFailureRollsBack:
    def test_failure_on_later_item_rolls_back_everything(self, store, retriever):
        candidates = [_cand(_distinct(i)) for i in range(3)]
        original_add_fact = MemoryStore.add_fact
        call_count = {"n": 0}

        def _flaky_add_fact(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise RuntimeError("injected failure on item 3")
            return original_add_fact(self, *args, **kwargs)

        with patch.object(MemoryStore, "add_fact", _flaky_add_fact):
            result = ingest_candidates_batch(
                store, retriever, candidates,
                namespace="ns-fail", source=SourceContext(), atomic=True,
            )

        assert result.committed is False
        assert result.counts[STATUS_FAILED] == 1
        # Zero rows survive -- not "first two committed, third rolled back".
        assert _fact_count(store, "ns-fail") == 0
        assert store._conn.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 0
        assert store._conn.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0] == 0
        assert _bank_row(store, "ns-fail", "general") is None

    def test_non_atomic_failure_does_not_affect_other_items(self, store, retriever):
        candidates = [_cand(_distinct(i)) for i in range(3, 6)]
        original_add_fact = MemoryStore.add_fact
        call_count = {"n": 0}

        def _flaky_add_fact(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("injected failure on item 2")
            return original_add_fact(self, *args, **kwargs)

        with patch.object(MemoryStore, "add_fact", _flaky_add_fact):
            result = ingest_candidates_batch(
                store, retriever, candidates,
                namespace="ns-nonatomic", source=SourceContext(), atomic=False,
            )

        assert result.committed is True
        assert result.counts[STATUS_INSERTED] == 2
        assert result.counts[STATUS_FAILED] == 1
        assert _fact_count(store, "ns-nonatomic") == 2


class TestDryRun:
    def test_dry_run_performs_zero_writes(self, store, retriever):
        store.add_fact("Pre-existing fact about ExistingThing.", namespace="ns-dry")
        before_facts = _fact_count(store, "ns-dry")
        before_sources = store._conn.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0]
        before_bank = _bank_row(store, "ns-dry", "general")

        candidates = [
            _cand("Pre-existing fact about ExistingThing."),  # exact duplicate
            _cand("Brand new fact about NewThing here."),      # would insert
        ]
        result = ingest_candidates_batch(
            store, retriever, candidates,
            namespace="ns-dry", source=SourceContext(), dry_run=True,
        )

        assert result.dry_run is True
        assert result.committed is False
        assert _fact_count(store, "ns-dry") == before_facts
        assert store._conn.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == before_sources
        assert _bank_row(store, "ns-dry", "general") == before_bank

        statuses = {item.status for item in result.items}
        assert STATUS_DUPLICATE in statuses
        assert STATUS_INSERTED in statuses
        # The planned "inserted" item must not carry a real fact_id since
        # nothing was actually written.
        inserted_item = next(i for i in result.items if i.status == STATUS_INSERTED)
        assert inserted_item.fact_id is None


class TestPerItemStatusCoverage:
    def test_every_status_value_is_exercised(self, store, retriever):
        # STATUS_SUPERSEDED target.
        old_id = store.add_fact("My favorite color is blue.", namespace="ns-all")
        # STATUS_FORGOTTEN target.
        forget_target_id = store.add_fact("I used to live in Chicago.", namespace="ns-all")
        # STATUS_DUPLICATE target (exact match already in store).
        store.add_fact("The sky is blue today.", namespace="ns-all")

        candidates = [
            _cand("A completely new standalone fact about Zephyr."),          # inserted
            _cand("The sky is blue today."),                                  # duplicate
            _cand("my favorite color is actually green", operation="correction", old_text="favorite color is blue"),  # superseded
            _cand("I used to live in Chicago.", operation="forget"),          # forgotten
            _cand("delete the thing", operation="forget"),                    # noop (no match)
            _cand("x" * (MAX_ITEM_CONTENT_LEN + 1)),                          # rejected
        ]
        result = ingest_candidates_batch(
            store, retriever, candidates,
            namespace="ns-all", source=SourceContext(), atomic=False,
        )

        seen = {item.status for item in result.items}
        assert STATUS_INSERTED in seen
        assert STATUS_DUPLICATE in seen
        assert STATUS_SUPERSEDED in seen
        assert STATUS_FORGOTTEN in seen
        assert STATUS_NOOP in seen
        assert STATUS_REJECTED in seen

        # Now force a STATUS_FAILED with an injected exception.
        with patch.object(MemoryStore, "add_fact", side_effect=RuntimeError("boom")):
            failed_result = ingest_candidates_batch(
                store, retriever, [_cand("Yet another totally new fact about Quokka.")],
                namespace="ns-all", source=SourceContext(), atomic=False,
            )
        assert failed_result.items[0].status == STATUS_FAILED


class TestProvenance:
    def test_provenance_recorded_for_every_successful_item(self, store, retriever):
        candidates = [_cand(_distinct(i)) for i in range(6, 10)]
        source = SourceContext(via="builtin_import", platform="cli", author="tester")
        result = ingest_candidates_batch(
            store, retriever, candidates, namespace="ns-prov", source=source,
        )
        assert result.counts[STATUS_INSERTED] == 4
        for item in result.items:
            row = store._conn.execute(
                "SELECT via, platform, author FROM fact_sources WHERE fact_id = ?",
                (item.fact_id,),
            ).fetchone()
            assert row is not None
            assert row["via"] == "builtin_import"
            assert row["author"] == "tester"


class TestNamespaceIsolation:
    def test_same_text_different_namespaces_both_insert(self, store, retriever):
        text = "Shared phrasing used in two different chats."
        r1 = ingest_candidates_batch(
            store, retriever, [_cand(text)], namespace="chat_A", source=SourceContext(),
        )
        r2 = ingest_candidates_batch(
            store, retriever, [_cand(text)], namespace="chat_B", source=SourceContext(),
        )
        assert r1.counts[STATUS_INSERTED] == 1
        assert r2.counts[STATUS_INSERTED] == 1
        assert r1.items[0].fact_id != r2.items[0].fact_id

    def test_same_text_one_namespace_consolidates_intra_batch(self, store, retriever):
        text = "Repeated statement inside a single batch call."
        result = ingest_candidates_batch(
            store, retriever, [_cand(text), _cand(text)],
            namespace="ns-intra", source=SourceContext(),
        )
        assert result.counts[STATUS_INSERTED] == 1
        assert result.counts[STATUS_DUPLICATE] == 1
        assert _fact_count(store, "ns-intra") == 1

    def test_same_text_one_namespace_consolidates_cross_batch(self, store, retriever):
        text = "Repeated statement across two separate batch calls."
        r1 = ingest_candidates_batch(
            store, retriever, [_cand(text)], namespace="ns-cross", source=SourceContext(),
        )
        r2 = ingest_candidates_batch(
            store, retriever, [_cand(text)], namespace="ns-cross", source=SourceContext(),
        )
        assert r1.counts[STATUS_INSERTED] == 1
        assert r2.counts[STATUS_DUPLICATE] == 1
        assert _fact_count(store, "ns-cross") == 1

    def test_namespace_none_is_rejected(self, store, retriever):
        with pytest.raises(ValueError):
            ingest_candidates_batch(
                store, retriever, [_cand("x")], namespace=None, source=SourceContext(),
            )

    def test_isolation_blocked_refuses_to_run(self, store, retriever):
        with pytest.raises(PermissionError):
            ingest_candidates_batch(
                store, retriever, [_cand("x")], namespace="whatever",
                source=SourceContext(), isolation_blocked=True,
            )
        assert _fact_count(store, "whatever") == 0


class TestTombstonePolicy:
    def test_batch_forget_writes_tombstone(self, store, retriever):
        store.add_fact("I used to work at OldCo full time.", namespace="ns-tomb")
        result = ingest_candidates_batch(
            store, retriever,
            [_cand("I used to work at OldCo full time.", operation="forget")],
            namespace="ns-tomb", source=SourceContext(),
        )
        assert result.counts[STATUS_FORGOTTEN] == 1
        tombstones = store.get_tombstones(namespace="ns-tomb")
        assert len(tombstones) == 1

    def test_restating_forgotten_content_later_is_a_normal_new_insert(self, store, retriever):
        """Forgetting content does not block restating it later in a new
        batch -- that is the existing single-item policy (tombstones only
        guard against RESTORE reviving old backup rows, see backup.py), and
        batch ingestion must not invent a stricter or different rule."""
        text = "I used to work at OldCo full time."
        store.add_fact(text, namespace="ns-tomb2")
        ingest_candidates_batch(
            store, retriever, [_cand(text, operation="forget")],
            namespace="ns-tomb2", source=SourceContext(),
        )
        assert _fact_count(store, "ns-tomb2") == 0

        result = ingest_candidates_batch(
            store, retriever, [_cand(text)], namespace="ns-tomb2", source=SourceContext(),
        )
        assert result.counts[STATUS_INSERTED] == 1
        assert _fact_count(store, "ns-tomb2") == 1

    def test_restore_still_honors_tombstone_after_batch_forget(self, store, retriever, tmp_path):
        text = "A fact that will survive in an old backup."
        store.add_fact(text, namespace="")
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        info = backup_module.create_backup(str(store.db_path), str(backup_dir))

        ingest_candidates_batch(
            store, retriever, [_cand(text, operation="forget")],
            namespace="", source=SourceContext(),
        )
        assert _fact_count(store, "") == 0

        tombstones = frozenset(store.get_tombstones(namespace=None))
        store.close()
        restore_result = backup_module.restore_backup(info.path, str(store.db_path), tombstones=tombstones)
        assert restore_result is not None

        reopened = MemoryStore(db_path=store.db_path, hrr_dim=32)
        try:
            row = reopened._conn.execute(
                "SELECT COUNT(*) FROM facts WHERE content = ?", (text,)
            ).fetchone()
            assert row[0] == 0  # tombstone prevented the restore from reviving it
        finally:
            reopened.close()


class TestBoundedInput:
    def test_batch_too_large_is_rejected(self, store, retriever):
        candidates = [_cand(f"Fact {i} about Thing{i}.") for i in range(MAX_BATCH_SIZE + 1)]
        with pytest.raises(ValueError):
            ingest_candidates_batch(
                store, retriever, candidates, namespace="ns-big", source=SourceContext(),
            )
        assert _fact_count(store, "ns-big") == 0

    def test_oversized_item_is_rejected_not_crashed(self, store, retriever):
        candidates = [_cand("x" * (MAX_ITEM_CONTENT_LEN + 1))]
        result = ingest_candidates_batch(
            store, retriever, candidates, namespace="ns-oversize", source=SourceContext(),
        )
        assert result.counts[STATUS_REJECTED] == 1
        assert _fact_count(store, "ns-oversize") == 0


class TestConcurrency:
    def test_concurrent_batches_do_not_corrupt_sqlite(self, store, retriever):
        errors: list[Exception] = []

        def _run(namespace: str, offset: int, n: int) -> None:
            try:
                candidates = [_cand(_distinct(offset + i)) for i in range(n)]
                ingest_candidates_batch(
                    store, retriever, candidates, namespace=namespace, source=SourceContext(),
                )
            except Exception as exc:  # pragma: no cover - surfaced via `errors`
                errors.append(exc)

        threads = [
            threading.Thread(target=_run, args=(f"ns-thread-{t}", t * 10, 10))
            for t in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors
        total = store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        assert total == 40
        for t in range(4):
            assert _fact_count(store, f"ns-thread-{t}") == 10
