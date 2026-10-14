"""GLM monitor endpoint selection: a Hermes GLM_BASE_URL override steers region.

The api-key fallback used to hard-code the international monitor base, so a
China Coding Plan key always died with "Could not fetch usage: network error"
(api.z.ai unreachable from the network, and the key would not work there
anyway). The override's region must decide where the monitor call goes.
"""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

QUOTA_PATH = "/api/monitor/usage/quota/limit"


def probes():
    for source in ("probe.py", "catalog/desktop/probe.py"):
        spec = importlib.util.spec_from_file_location("glm_region_probe", Path(__file__).parent / source)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        yield source, probe


class GlmRegionTests(unittest.TestCase):
    def monitor_base(self, probe, base_override):
        seen = []

        def respond(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": {"level": "Lite", "limits": [
                {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 13,
                 "nextResetTime": 1791994881184},
            ]}})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch.object(probe, "_glm_hermes_api_key", return_value="test-key"), \
                patch.object(probe, "_glm_hermes_base_url", return_value=base_override), \
                patch.object(httpx, "Client", return_value=client):
            snapshot = probe._fetch_glm_hermes_api_key_usage()
        self.assertEqual(snapshot["provider"], "glm")
        return seen

    def test_bigmodel_override_hits_china_monitor(self):
        for source, probe in probes():
            with self.subTest(file=source):
                seen = self.monitor_base(probe, "https://open.bigmodel.cn")
                self.assertEqual(seen, ["https://open.bigmodel.cn" + QUOTA_PATH])

    def test_zai_override_hits_international_monitor(self):
        for source, probe in probes():
            with self.subTest(file=source):
                seen = self.monitor_base(probe, "https://api.z.ai")
                self.assertEqual(seen, ["https://api.z.ai" + QUOTA_PATH])

    def test_coding_plan_urls_map_to_their_region_monitor(self):
        for source, probe in probes():
            with self.subTest(file=source):
                seen = self.monitor_base(probe, "https://open.bigmodel.cn/api/coding/paas/v4")
                self.assertEqual(seen, ["https://open.bigmodel.cn" + QUOTA_PATH])

    def test_missing_override_still_defaults_to_international(self):
        for source, probe in probes():
            with self.subTest(file=source):
                seen = self.monitor_base(probe, None)
                self.assertEqual(seen, ["https://api.z.ai" + QUOTA_PATH])


if __name__ == "__main__":
    unittest.main()
