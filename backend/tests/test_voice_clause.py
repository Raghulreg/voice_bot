import unittest

from services.streaming_tts import pop_early_clause


class EarlyClauseTests(unittest.TestCase):
    def test_waits_until_there_are_enough_words(self):
        clause, rest = pop_early_clause("The claim is still")
        self.assertEqual(clause, "")
        self.assertIn("claim", rest)

    def test_releases_the_first_clause_before_the_period(self):
        text = "The claim number is one two three four five and it is pending review"
        clause, rest = pop_early_clause(text)
        self.assertTrue(clause)
        self.assertLess(len(clause.split()), len(text.split()))
        self.assertIn("pending", rest)

    def test_prefers_a_comma_when_one_is_already_spoken(self):
        clause, rest = pop_early_clause(
            "The claim is pending, and the reviewer still has the file today"
        )
        self.assertEqual(clause, "The claim is pending,")
        self.assertTrue(rest.strip().startswith("and"))


if __name__ == "__main__":
    unittest.main()
