"""Validate authored fixture integrity, not model quality or semantic truth."""

import hashlib
import json
import unittest
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FROZEN_SHA256 = "9a1e8944d876625213a05d4bb5557c35c8f02a012773e227d45ea446bfee23a6"


class ApplicationFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = (ROOT / "applications-frozen.jsonl").read_bytes()
        cls.cases = [json.loads(line) for line in cls.raw.splitlines()]
        cls.manifest = json.loads((ROOT / "applications-manifest.json").read_text())

    def test_frozen_bytes_match_manifest_and_independent_checksum(self):
        actual = hashlib.sha256(self.raw).hexdigest()
        self.assertEqual(actual, FROZEN_SHA256)
        self.assertEqual(self.manifest["sha256"], FROZEN_SHA256)
        self.assertEqual(self.manifest["dataset"], "applications-frozen.jsonl")

    def test_sixty_cases_are_exactly_balanced_and_stratified_before_tuning(self):
        cases = self.cases
        self.assertEqual(len(cases), 60)
        self.assertEqual(Counter(c["sector"] for c in cases), {"tech": 30, "finance": 30})
        self.assertEqual(Counter(c["split"] for c in cases), {"tune": 30, "heldout": 30})
        self.assertTrue(self.manifest["freeze"]["prepared_before_tuning"])
        for sector in ("tech", "finance"):
            for split in ("tune", "heldout"):
                bucket = [c for c in cases if c["sector"] == sector and c["split"] == split]
                self.assertEqual(len(bucket), 15)
                self.assertEqual(sum(c["category"] == "direct_evidence" for c in bucket), 8)
                self.assertEqual(sum(c["category"] == "unanswerable" for c in bucket), 4)
                self.assertEqual(sum(c["adversarial"] for c in bucket), 3)

    def test_unknown_and_adversarial_counts_match_manifest_without_quality_claims(self):
        cases = self.cases
        counts = self.manifest["counts"]
        unknown = sum(not c["answerable"] for c in cases)
        adversarial = sum(c["adversarial"] for c in cases)
        heldout = [c for c in cases if c["split"] == "heldout"]
        self.assertEqual(unknown, 24)
        self.assertGreaterEqual(unknown, 15)
        self.assertEqual(adversarial, 12)
        self.assertGreaterEqual(adversarial, 10)
        self.assertEqual(counts["unanswerable"], unknown)
        self.assertEqual(counts["adversarial"], adversarial)
        self.assertEqual(counts["heldout_unanswerable"], sum(not c["answerable"] for c in heldout))
        self.assertEqual(counts["heldout_adversarial"], sum(c["adversarial"] for c in heldout))
        self.assertGreaterEqual(counts["heldout_unanswerable"], 8)
        self.assertGreaterEqual(counts["heldout_adversarial"], 5)
        self.assertEqual(counts["sectors"], dict(Counter(c["sector"] for c in cases)))
        self.assertEqual(counts["splits"], dict(Counter(c["split"] for c in cases)))
        self.assertEqual(counts["categories"], dict(Counter(c["category"] for c in cases)))
        self.assertEqual(counts["cases"], len(cases))
        self.assertEqual(self.manifest["quality_gates"]["retrieval_run"], "not_run")
        self.assertEqual(self.manifest["quality_gates"]["human_support_audit"], "required_not_run")
        self.assertEqual(self.manifest["quality_gates"]["paired_editorial_review"], "required_not_run")
        self.assertEqual(self.manifest["quality_gates"]["hiring_quality"], "not_established")

    def test_entity_ids_are_stable_uuids_and_unique_across_profiles_and_splits(self):
        identities = []
        for case in self.cases:
            identities.extend(case[k] for k in ("id", "owner_id", "application_id"))
            identities.extend(s["id"] for s in self.sources(case))
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(len({c["key"] for c in self.cases}), 60)
        for identity in identities:
            parsed = uuid.UUID(identity)
            self.assertEqual(str(parsed), identity)
            self.assertEqual(parsed.version, 5)

    def test_question_and_label_types_are_explicit(self):
        for case in self.cases:
            with self.subTest(case=case["key"]):
                self.assertEqual(case["schema"], 1)
                self.assertIs(type(case["answerable"]), bool)
                self.assertIs(type(case["adversarial"]), bool)
                for key in ("question", "retrieval_query", "category"):
                    self.assertIsInstance(case[key], str)
                    self.assertTrue(case[key].strip())
                self.assertTrue(case["gold"]["label_basis"].strip())
                for key in ("personal_support_ids", "employer_support_ids", "retrieval_relevant_ids"):
                    self.assertIsInstance(case["gold"][key], list)
                    self.assertEqual(len(case["gold"][key]), len(set(case["gold"][key])))

    def test_expected_support_never_crosses_owner_application_or_pending_boundary(self):
        for case in self.cases:
            with self.subTest(case=case["key"]):
                confirmed = {s["id"] for s in case["confirmed_fact_bank"]}
                pending = {s["id"] for s in case["pending_proposals"]}
                employers = {s["id"] for s in case["employer_excerpts"]}
                for source in self.sources(case):
                    self.assertEqual(source["owner_id"], case["owner_id"])
                    self.assertTrue(source["text"].strip())
                for source in case["employer_excerpts"]:
                    self.assertEqual(source["application_id"], case["application_id"])
                    self.assertTrue(source["fictional"])
                    self.assertEqual(source["provenance"], "synthetic_canonical_excerpt")
                for source in case["confirmed_fact_bank"]:
                    self.assertEqual(source["state"], "confirmed")
                for source in case["pending_proposals"]:
                    self.assertEqual(source["state"], "pending")
                gold = case["gold"]
                self.assertLessEqual(set(gold["personal_support_ids"]), confirmed)
                self.assertLessEqual(set(gold["employer_support_ids"]), employers)
                self.assertLessEqual(set(gold["retrieval_relevant_ids"]), confirmed)
                self.assertFalse(set(gold["personal_support_ids"]) & pending)
                if case["answerable"]:
                    self.assertTrue(gold["personal_support_ids"])
                    self.assertTrue(gold["employer_support_ids"])
                    self.assertEqual(gold["decision"], "supported")
                else:
                    self.assertEqual(gold["personal_support_ids"], [])
                    self.assertEqual(gold["employer_support_ids"], [])
                    self.assertEqual(gold["decision"], "needs_confirmation")

    def test_contradictory_relevant_quotes_are_not_answer_support(self):
        conflicts = [c for c in self.cases if c["category"] == "contradiction"]
        self.assertEqual(len(conflicts), 4)
        for case in conflicts:
            self.assertFalse(case["answerable"])
            self.assertEqual(len(case["gold"]["retrieval_relevant_ids"]), 2)
            self.assertEqual(case["gold"]["personal_support_ids"], [])

    def test_v2_snake_software_failure_query_and_fact_texts_are_preserved_unchanged(self):
        legacy = json.loads((ROOT / "cases.json").read_text())
        query = next(q for q in legacy["queries"] if q["key"] == "software_not_snakes")
        preserved = [c for c in self.cases if "regression_fixture_v2" in c]
        self.assertEqual(len(preserved), 1)
        case = preserved[0]
        self.assertEqual(case["regression_fixture_v2"], {
            "schema": 2, "facts": legacy["facts"], "query": query,
        })
        self.assertEqual(case["question"], query["query"])
        self.assertEqual(case["retrieval_query"], query["query"])
        facts = case["confirmed_fact_bank"]
        self.assertEqual([{"key": s["key"], "text": s["text"]} for s in facts], legacy["facts"])
        key_by_id = {s["id"]: s["key"] for s in facts}
        self.assertEqual([key_by_id[i] for i in case["gold"]["personal_support_ids"]], query["relevant"])
        self.assertTrue(case["adversarial"])

    @staticmethod
    def sources(case):
        return case["confirmed_fact_bank"] + case["pending_proposals"] + case["employer_excerpts"]


if __name__ == "__main__":
    unittest.main()
