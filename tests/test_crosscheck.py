import unittest

from crosscheck import held, parse_ranges


class TestRanges(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_ranges("1-5,8-9"), [(1, 5), (8, 9)])
        self.assertEqual(parse_ranges("7"), [(7, 7)])
        self.assertEqual(parse_ranges("empty"), [])

    def test_held(self):
        r = parse_ranges("10-20,30-31")
        self.assertTrue(held(r, 10))
        self.assertTrue(held(r, 31))
        self.assertFalse(held(r, 25))


if __name__ == "__main__":
    unittest.main()
