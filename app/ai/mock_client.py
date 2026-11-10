import json


class MockClient:
    """Stand-in used when no API key exists. Output is NOT real AI content."""
    live = False

    def __init__(self, name="mock"):
        self.name = name
        self.calls = []

    def available(self):
        return True

    def generate(self, prompt, system=None, stage=None, **opts):
        self.calls.append(stage)
        if stage in ("blueprint", "research_blueprint"):
            return json.dumps({"sections": ["[mock section]"], "required_information": [],
                               "claims_to_verify": []})
        if stage == "qa":
            return "PASS"
        return f"[MOCK {stage}] placeholder output"
