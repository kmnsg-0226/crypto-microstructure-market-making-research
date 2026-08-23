import hashlib
import json
import unittest

from pyresearch.execution.pipeline import (
    FROZEN_SPEC,
    FROZEN_SPEC_HASH,
    _validate_stage,
    select_and_freeze,
)


class FrozenExecutionPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = json.loads(FROZEN_SPEC.read_text(encoding="utf-8"))

    def test_execution_spec_is_frozen_and_hash_matches(self) -> None:
        self.assertEqual(self.spec["status"], "frozen_after_development")
        self.assertIn("selected_rule", self.spec)
        observed = hashlib.sha256(FROZEN_SPEC.read_bytes()).hexdigest()
        self.assertEqual(FROZEN_SPEC_HASH.read_text(encoding="utf-8").strip(), observed)

    def test_selection_is_idempotent_after_freeze(self) -> None:
        before = FROZEN_SPEC.read_bytes()
        returned = select_and_freeze()
        self.assertEqual(returned["selected_rule"], self.spec["selected_rule"])
        self.assertEqual(FROZEN_SPEC.read_bytes(), before)

    def test_june_and_oos_are_isolated_from_development(self) -> None:
        development = set(self.spec["split"]["development"])
        validation = set(self.spec["split"]["validation"])
        oos = set(self.spec["split"]["oos"])
        self.assertEqual(validation, {"2026-06-01"})
        self.assertEqual(oos, {"2026-07-01", "2026-08-01"})
        self.assertFalse(development & validation)
        self.assertFalse(development & oos)
        self.assertFalse(validation & oos)

    def test_unfrozen_spec_cannot_open_validation_or_oos(self) -> None:
        unfrozen = json.loads(json.dumps(self.spec))
        unfrozen["status"] = "draft_before_execution_results"
        for stage in ("validation", "oos"):
            with self.assertRaises(ValueError):
                _validate_stage(unfrozen, stage)


if __name__ == "__main__":
    unittest.main()
