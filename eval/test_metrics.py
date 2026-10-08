import math
import unittest

from run import metrics


class MetricsTests(unittest.TestCase):
    def test_rank_sensitive_binary_metrics(self):
        result = metrics(["wrong", "a"], ["a", "b"])
        self.assertEqual(result["recall_at_8"], 0.5)
        self.assertEqual(result["mrr_at_8"], 0.5)
        self.assertEqual(result["top1_accuracy"], 0)
        self.assertAlmostEqual(result["ndcg_at_8"], (1 / math.log2(3)) / (1 + 1 / math.log2(3)))

    def test_deduplicate_and_cutoff(self):
        self.assertEqual(metrics(["a", "a"], ["a"])["recall_at_8"], 1)
        self.assertEqual(metrics([str(i) for i in range(8)] + ["a"], ["a"])["recall_at_8"], 0)

    def test_unanswerable_is_unscored_not_perfect_abstention(self):
        self.assertIsNone(metrics(["irrelevant"], []))
        self.assertIsNone(metrics([], []))


if __name__ == "__main__":
    unittest.main()
