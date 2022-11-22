import unittest
from ws_conan_scanner import conan_scanner


class TestConanScanner(unittest.TestCase):

    def test_is_conan_installed(self):
        with self.assertLogs() as captured:
            conan_scanner.is_conan_installed()

        test_l = [
            "conan --version",  # 1
            "Conan version",  # 2
            "Conan identified - Conan version"  # 3
        ]

        for i in range(len(captured.records)):
            self.assertIn(member=test_l[i],
                          container=captured.records[i].getMessage())


if __name__ == '__main__':
    unittest.main()
