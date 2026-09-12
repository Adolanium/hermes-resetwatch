"""DeepSeek pricing follows weekdays and window boundaries in UTC."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import unittest


class DeepSeekPricingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probes = []
        for filename in ('probe.py', 'catalog/desktop/probe.py'):
            spec = importlib.util.spec_from_file_location('pricing_probe', Path(__file__).parent / filename)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cls.probes.append((filename, module))

    def test_weekday_windows_and_all_weekend_hours(self):
        monday = datetime(2026, 9, 7, tzinfo=timezone.utc)
        boundaries = [(0, 0, False), (0, 59, False), (1, 0, True), (3, 59, True),
                      (4, 0, False), (5, 59, False), (6, 0, True), (9, 59, True),
                      (10, 0, False), (23, 59, False)]
        for filename, probe in self.probes:
            for day in range(7):
                for hour, minute, weekday_peak in boundaries:
                    stamp = monday + timedelta(days=day, hours=hour, minutes=minute)
                    with self.subTest(file=filename, stamp=stamp):
                        peak, text = probe._deepseek_peak_status(stamp)
                        self.assertEqual(peak, day < 5 and weekday_peak)
                        self.assertIn('Mon-Fri', text)
                        self.assertTrue(text.startswith('Peak pricing now' if peak else 'Off-peak now'))
                if day >= 5:
                    for minute in range(24 * 60):
                        self.assertFalse(probe._deepseek_peak_status(monday + timedelta(days=day, minutes=minute))[0])

    def test_weekday_is_determined_after_utc_conversion(self):
        cases = [
            # Sunday locally, but Monday 02:00 UTC is peak.
            ('2026-09-06T19:00:00-07:00', True),
            # Friday locally, but Saturday 02:00 UTC is off-peak.
            ('2026-09-11T19:00:00-07:00', False),
            ('2026-09-07T09:00:00+08:00', True),
            ('2026-09-12T02:00:00', False),
        ]
        for filename, probe in self.probes:
            for stamp, expected in cases:
                with self.subTest(file=filename, stamp=stamp):
                    self.assertEqual(probe._deepseek_peak_status(datetime.fromisoformat(stamp))[0], expected)


if __name__ == '__main__':
    unittest.main()
